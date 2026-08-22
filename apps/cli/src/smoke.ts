import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  RUN_EVENT_VERSION,
  RUN_SPEC_VERSION,
  canonicalJson,
  type EvidenceManifestV1,
  type JsonObject,
  type JsonValue,
  type RunEventV1,
  type RunSpecV1,
} from "@open-agent-lab/contracts";
import {
  sha256,
  verifyEvidenceBundle,
  writeEvidenceBundle,
} from "@open-agent-lab/evidence";
import type { KernelEventV1, KernelStateSnapshotV1 } from "@open-agent-lab/kernel";

import { runSmokeKernel, smokeWorkspaceDigest, sortLines } from "./smoke-kernel.js";

interface SmokeTask {
  id: string;
  revision: number;
  title: string;
  instruction: string;
  input: string;
  expectedOutput: string;
}

interface SmokeResult {
  schemaVersion: "smoke-result/v1";
  runId: string;
  taskId: string;
  success: boolean;
  inputSha256: string;
  outputSha256: string;
  expectedOutputSha256: string;
  checks: {
    exactBytes: boolean;
    trailingNewline: boolean;
    sorted: boolean;
  };
}

export interface RunSmokeOptions {
  outputDirectory: string;
  createdAt?: string;
}

export interface SmokeRunSummary {
  outputDirectory: string;
  manifest: EvidenceManifestV1;
  result: SmokeResult;
  kernelState: Readonly<KernelStateSnapshotV1>;
}

export interface SmokeReplaySummary {
  manifestId: string;
  runId: string;
  taskId: string;
  success: true;
  eventCount: number;
  kernelEventCount: number;
}

const taskUrl = new URL("../../../benchmarks/smoke/task.json", import.meta.url);

function assertRecord(value: unknown, label: string): asserts value is Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new TypeError(`${label} must be a JSON object.`);
  }
}

function assertString(value: unknown, label: string): asserts value is string {
  if (typeof value !== "string") {
    throw new TypeError(`${label} must be a string.`);
  }
}

function parseSmokeTask(value: unknown): SmokeTask {
  assertRecord(value, "Smoke task");
  assertString(value.id, "Smoke task id");
  assertString(value.title, "Smoke task title");
  assertString(value.instruction, "Smoke task instruction");
  assertString(value.input, "Smoke task input");
  assertString(value.expectedOutput, "Smoke task expectedOutput");
  if (!Number.isSafeInteger(value.revision) || Number(value.revision) < 1) {
    throw new TypeError("Smoke task revision must be a positive safe integer.");
  }
  return {
    id: value.id,
    revision: Number(value.revision),
    title: value.title,
    instruction: value.instruction,
    input: value.input,
    expectedOutput: value.expectedOutput,
  };
}

async function loadSmokeTask(): Promise<SmokeTask> {
  const raw = await readFile(taskUrl, "utf8");
  return parseSmokeTask(JSON.parse(raw) as unknown);
}

function timestampAt(createdAt: string, sequence: number): string {
  const epoch = Date.parse(createdAt);
  if (!Number.isFinite(epoch)) {
    throw new TypeError("createdAt must be a valid ISO-8601 timestamp.");
  }
  return new Date(epoch + sequence).toISOString();
}

function projectKernelEvent(entry: KernelEventV1): RunEventV1 {
  return {
    schemaVersion: RUN_EVENT_VERSION,
    runId: entry.runId,
    sequence: entry.sequence,
    timestamp: entry.timestamp,
    type: entry.type,
    data: entry.data,
  };
}

function parseJsonObject<T>(raw: string, label: string): T {
  const value = JSON.parse(raw) as unknown;
  assertRecord(value, label);
  return value as T;
}

export async function runSmoke(options: RunSmokeOptions): Promise<SmokeRunSummary> {
  const task = await loadSmokeTask();
  const createdAt = options.createdAt ?? new Date().toISOString();
  const taskIdentity = sha256(canonicalJson(task as unknown as JsonValue));
  const runId = `${task.id}-${taskIdentity.slice("sha256:".length, 19)}`;
  const workspace = await mkdtemp(join(tmpdir(), "open-agent-lab-smoke-"));

  try {
    const inputPath = join(workspace, "input.txt");
    await writeFile(inputPath, task.input, { encoding: "utf8", flag: "wx" });
    let kernelSequence = 0;
    const kernel = await runSmokeKernel({
      workspace,
      runId,
      taskDigest: taskIdentity,
      expectedOutput: task.expectedOutput,
      createdAt,
      clock: () => timestampAt(createdAt, kernelSequence++),
    });
    const observedInput = kernel.input;
    const observedOutput = kernel.output;

    const inputSha256 = sha256(observedInput);
    const outputSha256 = sha256(observedOutput);
    const expectedOutputSha256 = sha256(task.expectedOutput);
    const { exactBytes, trailingNewline, sorted } = kernel.checks;
    const success = exactBytes && trailingNewline && sorted;
    const events = kernel.journal.map(projectKernelEvent);

    const spec: RunSpecV1 = {
      schemaVersion: RUN_SPEC_VERSION,
      runId,
      createdAt,
      task: {
        id: task.id,
        instruction: task.instruction,
        benchmark: {
          name: "deterministic-smoke",
          version: "v1",
          taskId: task.id,
        },
      },
      agent: {
        name: "open-agent-lab-recoverable-kernel",
        version: "0.0.0",
        revision: "sqlite-kernel-sort-lines-v1",
      },
      model: {
        provider: "scripted",
        name: "deterministic-sort-lines-v1",
        parameters: { temperature: 0 },
      },
      limits: {
        maxSteps: events.length,
        wallTimeMs: 10_000,
        maxInputTokens: 0,
        maxOutputTokens: 0,
        maxCostUsd: 0,
      },
      metadata: {
        network: "disabled",
        recoverableKernel: true,
        taskRevision: task.revision,
      },
    };

    const result: SmokeResult = {
      schemaVersion: "smoke-result/v1",
      runId,
      taskId: task.id,
      success,
      inputSha256,
      outputSha256,
      expectedOutputSha256,
      checks: { exactBytes, trailingNewline, sorted },
    };
    if (!success) {
      throw new Error("The deterministic smoke task failed its own verification.");
    }

    const replay = {
      schemaVersion: "smoke-replay/v1",
      algorithm: "sort-nonempty-lines-bytewise-v1",
      taskId: task.id,
      inputSha256,
      expectedOutputSha256,
    };
    const json = (value: JsonValue): string => `${canonicalJson(value)}\n`;
    const manifest = await writeEvidenceBundle(options.outputDirectory, {
      runId,
      createdAt,
      files: [
        {
          path: "run-spec.json",
          content: json(spec as unknown as JsonValue),
          mediaType: "application/json",
          role: "run-spec",
        },
        {
          path: "events.jsonl",
          content: events.map((entry) => canonicalJson(entry as unknown as JsonValue)).join("\n") + "\n",
          mediaType: "application/x-ndjson",
          role: "event-log",
        },
        {
          path: "task.json",
          content: json(task as unknown as JsonValue),
          mediaType: "application/json",
          role: "task",
        },
        {
          path: "result.json",
          content: json(result as unknown as JsonValue),
          mediaType: "application/json",
          role: "result",
        },
        {
          path: "kernel-state.json",
          content: json(kernel.state as unknown as JsonValue),
          mediaType: "application/json",
          role: "kernel-state",
        },
        {
          path: "kernel-events.jsonl",
          content: kernel.journal
            .map((entry) => canonicalJson(entry as unknown as JsonValue))
            .join("\n") + "\n",
          mediaType: "application/x-ndjson",
          role: "kernel-event-log",
        },
        {
          path: "replay.json",
          content: json(replay as unknown as JsonValue),
          mediaType: "application/json",
          role: "replay-contract",
        },
        {
          path: "workspace/input.txt",
          content: observedInput,
          mediaType: "text/plain; charset=utf-8",
          role: "task-input",
        },
        {
          path: "workspace/output.txt",
          content: observedOutput,
          mediaType: "text/plain; charset=utf-8",
          role: "task-output",
        },
      ],
      metadata: {
        profile: "deterministic-smoke/v2",
        network: "disabled",
        success: true,
      },
    });

    return { outputDirectory: options.outputDirectory, manifest, result, kernelState: kernel.state };
  } finally {
    await rm(workspace, { force: true, recursive: true });
  }
}

export async function replaySmokeEvidence(directory: string): Promise<SmokeReplaySummary> {
  const verified = await verifyEvidenceBundle(directory);
  const task = parseSmokeTask(JSON.parse(await readFile(join(directory, "task.json"), "utf8")) as unknown);
  const spec = parseJsonObject<RunSpecV1>(await readFile(join(directory, "run-spec.json"), "utf8"), "Run spec");
  const result = parseJsonObject<SmokeResult>(await readFile(join(directory, "result.json"), "utf8"), "Smoke result");
  const kernelState = parseJsonObject<KernelStateSnapshotV1>(
    await readFile(join(directory, "kernel-state.json"), "utf8"),
    "Kernel state",
  );
  const input = await readFile(join(directory, "workspace/input.txt"), "utf8");
  const output = await readFile(join(directory, "workspace/output.txt"), "utf8");
  const eventLines = (await readFile(join(directory, "events.jsonl"), "utf8"))
    .split("\n")
    .filter((line) => line.length > 0);
  const kernelEventLines = (await readFile(join(directory, "kernel-events.jsonl"), "utf8"))
    .split("\n")
    .filter((line) => line.length > 0);

  if (spec.runId !== verified.manifest.runId || result.runId !== spec.runId) {
    throw new Error("Smoke evidence run IDs do not agree.");
  }
  if (spec.task.id !== task.id || result.taskId !== task.id) {
    throw new Error("Smoke evidence task IDs do not agree.");
  }
  const taskDigest = sha256(canonicalJson(task as unknown as JsonValue));
  if (
    kernelState.runId !== spec.runId ||
    kernelState.taskDigest !== taskDigest ||
    kernelState.lifecycle !== "succeeded" ||
    !Array.isArray(kernelState.completed) ||
    kernelState.completed.length !== 2 ||
    kernelState.completed[0]?.invocation.toolName !== "workspace.read" ||
    kernelState.completed[1]?.invocation.toolName !== "workspace.create" ||
    kernelState.verification?.passed !== true ||
    kernelState.verification.runId !== spec.runId ||
    kernelState.verification.taskDigest !== taskDigest ||
    kernelState.verification.verifierId !== "deterministic-smoke" ||
    kernelState.verification.verifierVersion !== "1" ||
    kernelState.verification.workspaceDigest !== smokeWorkspaceDigest(input, output)
  ) {
    throw new Error("Smoke kernel state is incomplete or not bound to the evidence.");
  }
  if (input !== task.input || output !== sortLines(input) || output !== task.expectedOutput) {
    throw new Error("Smoke replay output does not match the deterministic task contract.");
  }
  if (
    !result.success ||
    result.inputSha256 !== sha256(input) ||
    result.outputSha256 !== sha256(output) ||
    result.expectedOutputSha256 !== sha256(task.expectedOutput)
  ) {
    throw new Error("Smoke result digests or success status are invalid.");
  }

  const events = eventLines.map((line, index) => {
    const parsed = parseJsonObject<RunEventV1>(line, `Run event ${index}`);
    if (parsed.runId !== spec.runId || parsed.sequence !== index) {
      throw new Error(`Run event ${index} has an invalid run ID or sequence.`);
    }
    return parsed;
  });
  if (events.length !== spec.limits.maxSteps) {
    throw new Error("Smoke event log is incomplete.");
  }
  let previousEventHash: string | null = null;
  const kernelEvents = kernelEventLines.map((line, index) => {
    const parsed = parseJsonObject<KernelEventV1>(line, `Kernel event ${index}`);
    const { eventHash, ...body } = parsed;
    if (
      parsed.runId !== spec.runId ||
      parsed.sequence !== index ||
      parsed.previousEventHash !== previousEventHash ||
      eventHash !== sha256(canonicalJson(body as unknown as JsonValue))
    ) {
      throw new Error(`Kernel event ${index} is not a valid hash-chain entry.`);
    }
    previousEventHash = eventHash;
    return parsed;
  });
  if (
    kernelEvents.map((entry) => entry.type).join(",") !==
      "run.created,tool.intent,tool.completed,tool.intent,tool.completed,verification.completed"
  ) {
    throw new Error("Smoke kernel journal is incomplete.");
  }
  if (
    events.length !== kernelEvents.length ||
    events.some((entry, index) =>
      canonicalJson(entry as unknown as JsonValue) !==
      canonicalJson(projectKernelEvent(kernelEvents[index]!) as unknown as JsonValue))
  ) {
    throw new Error("Smoke event log is not a projection of the kernel journal.");
  }

  return {
    manifestId: verified.manifest.manifestId,
    runId: spec.runId,
    taskId: task.id,
    success: true,
    eventCount: events.length,
    kernelEventCount: kernelEvents.length,
  };
}

export const smokeTaskPath = fileURLToPath(taskUrl);
export const smokeTaskDirectory = dirname(smokeTaskPath);
