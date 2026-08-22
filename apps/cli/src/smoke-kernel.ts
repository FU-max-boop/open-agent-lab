import { readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";

import {
  canonicalJson,
  type JsonObject,
  type JsonValue,
  type Sha256Digest,
} from "@open-agent-lab/contracts";
import { sha256 } from "@open-agent-lab/evidence";
import {
  TaskKernel,
  type KernelEventV1,
  type KernelStateSnapshotV1,
} from "@open-agent-lab/kernel";
import {
  ToolBroker,
  ToolBrokerError,
  type PersistedToolInvocation,
  type ToolDefinition,
} from "@open-agent-lab/tool-broker";

export interface SmokeKernelResult {
  input: string;
  output: string;
  checks: {
    exactBytes: boolean;
    trailingNewline: boolean;
    sorted: boolean;
  };
  journal: readonly KernelEventV1[];
  state: Readonly<KernelStateSnapshotV1>;
}

interface SmokeKernelOptions {
  workspace: string;
  runId: string;
  taskDigest: Sha256Digest;
  expectedOutput: string;
  createdAt: string;
  clock: () => string;
}

const ABSENT_FILE = sha256(canonicalJson({ exists: false }));

function object(value: JsonValue, label: string): JsonObject {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new ToolBrokerError("invalid_invocation", `${label} must be an object.`);
  }
  return value;
}

function stringField(value: JsonValue, field: string): string {
  const candidate = object(value, "Tool arguments")[field];
  if (typeof candidate !== "string") {
    throw new ToolBrokerError("invalid_invocation", `${field} must be a string.`);
  }
  return candidate;
}

function fixedPath(value: JsonValue, expected: string): void {
  if (stringField(value, "path") !== expected) {
    throw new ToolBrokerError("invalid_invocation", `Only ${expected} is allowed.`);
  }
}

function isFileMissing(error: unknown): boolean {
  return typeof error === "object" && error !== null && "code" in error && error.code === "ENOENT";
}

async function fileState(path: string): Promise<Sha256Digest> {
  try {
    return sha256(canonicalJson({ exists: true, sha256: sha256(await readFile(path)) }));
  } catch (error) {
    if (isFileMissing(error)) return ABSENT_FILE;
    throw error;
  }
}

export function sortLines(input: string): string {
  const lines = input.split("\n").filter((line) => line.length > 0);
  lines.sort((left, right) => Buffer.from(left).compare(Buffer.from(right)));
  return `${lines.join("\n")}\n`;
}

export function smokeWorkspaceDigest(input: string, output: string): Sha256Digest {
  return sha256(canonicalJson({ inputSha256: sha256(input), outputSha256: sha256(output) }));
}

function smokeBroker(workspace: string): ToolBroker {
  const inputPath = join(workspace, "input.txt");
  const outputPath = join(workspace, "output.txt");
  const definitions: ToolDefinition[] = [
    {
      name: "workspace.read",
      contractDigest: sha256("workspace.read/input.txt/read-only/v1"),
      effect: "read_only",
      stateFingerprint: async (argumentsValue) => {
        fixedPath(argumentsValue, "input.txt");
        return sha256(await readFile(inputPath));
      },
      execute: async (invocation) => {
        fixedPath(invocation.arguments, "input.txt");
        const input = await readFile(inputPath, "utf8");
        if (sha256(input) !== invocation.stateFingerprint) {
          throw new ToolBrokerError("precondition_changed", "input.txt changed before reading.");
        }
        return { output: input };
      },
    },
    {
      name: "workspace.create",
      contractDigest: sha256("workspace.create/output.txt/create-if-absent/v1"),
      effect: "workspace_mutation",
      stateFingerprint: async (argumentsValue) => {
        fixedPath(argumentsValue, "output.txt");
        stringField(argumentsValue, "content");
        return fileState(outputPath);
      },
      execute: async (invocation: Readonly<PersistedToolInvocation>) => {
        fixedPath(invocation.arguments, "output.txt");
        const content = stringField(invocation.arguments, "content");
        if (invocation.stateFingerprint !== ABSENT_FILE) {
          throw new ToolBrokerError("precondition_changed", "output.txt already exists.");
        }
        try {
          await writeFile(outputPath, content, { encoding: "utf8", flag: "wx" });
        } catch (error) {
          if (typeof error === "object" && error !== null && "code" in error && error.code === "EEXIST") {
            throw new ToolBrokerError("precondition_changed", "output.txt appeared before creation.");
          }
          throw error;
        }
        return { output: { sha256: sha256(content) } };
      },
      reconcile: async (invocation) => {
        const content = stringField(invocation.arguments, "content");
        try {
          return await readFile(outputPath, "utf8") === content
            ? { status: "applied", result: { output: { sha256: sha256(content) } } }
            : { status: "unknown", reason: "output.txt exists with unexpected content." };
        } catch (error) {
          if (isFileMissing(error)) return { status: "not_applied" };
          throw error;
        }
      },
    },
  ];
  return new ToolBroker(definitions);
}

async function inspectWorkspace(workspace: string, expectedOutput: string) {
  const input = await readFile(join(workspace, "input.txt"), "utf8");
  const output = await readFile(join(workspace, "output.txt"), "utf8");
  const checks = {
    exactBytes: output === expectedOutput,
    trailingNewline: output.endsWith("\n"),
    sorted: output === sortLines(output),
  };
  return { input, output, checks };
}

export async function runSmokeKernel(options: SmokeKernelOptions): Promise<SmokeKernelResult> {
  const kernel = await TaskKernel.create({
    directory: join(options.workspace, "kernel"),
    runId: options.runId,
    taskDigest: options.taskDigest,
    broker: smokeBroker(options.workspace),
    createdAt: options.createdAt,
    clock: options.clock,
  });
  try {
    const read = await kernel.invoke({
      invocationId: "read-input",
      toolName: "workspace.read",
      arguments: { path: "input.txt" },
    });
    if (typeof read.output !== "string") throw new Error("Smoke read returned invalid output.");
    const output = sortLines(read.output);
    await kernel.invoke({
      invocationId: "create-output",
      toolName: "workspace.create",
      arguments: { path: "output.txt", content: output },
    });
    let inspected: Awaited<ReturnType<typeof inspectWorkspace>> | undefined;
    await kernel.verify({
      id: "deterministic-smoke",
      version: "1",
      verify: async () => {
        inspected = await inspectWorkspace(options.workspace, options.expectedOutput);
        return {
          workspaceDigest: smokeWorkspaceDigest(inspected.input, inspected.output),
          passed: inspected.checks.exactBytes &&
            inspected.checks.trailingNewline &&
            inspected.checks.sorted,
          details: inspected.checks,
        };
      },
    });
    const state = kernel.state;
    if (state.lifecycle !== "succeeded" || inspected === undefined) {
      throw new Error("The recoverable kernel failed smoke verification.");
    }
    return { ...inspected, journal: kernel.journal, state };
  } finally {
    await kernel.close();
  }
}
