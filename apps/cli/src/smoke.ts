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
}

export interface SmokeReplaySummary {
  manifestId: string;
  runId: string;
  taskId: string;
  success: true;
  eventCount: number;
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

function sortLines(input: string): string {
  const lines = input.split("\n").filter((line) => line.length > 0);
  lines.sort((left, right) => Buffer.from(left).compare(Buffer.from(right)));
  return `${lines.join("\n")}\n`;
}

function timestampAt(createdAt: string, sequence: number): string {
  const epoch = Date.parse(createdAt);
  if (!Number.isFinite(epoch)) {
    throw new TypeError("createdAt must be a valid ISO-8601 timestamp.");
  }
  return new Date(epoch + sequence).toISOString();
}

function event(
  runId: string,
  createdAt: string,
  sequence: number,
  type: string,
  data: JsonObject,
): RunEventV1 {
  return {
    schemaVersion: RUN_EVENT_VERSION,
    runId,
    sequence,
    timestamp: timestampAt(createdAt, sequence),
    type,
    data,
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
    const outputPath = join(workspace, "output.txt");
    await writeFile(inputPath, task.input, { encoding: "utf8", flag: "wx" });
    const observedInput = await readFile(inputPath, "utf8");
    const output = sortLines(observedInput);
    await writeFile(outputPath, output, { encoding: "utf8", flag: "wx" });
    const observedOutput = await readFile(outputPath, "utf8");

    const inputSha256 = sha256(observedInput);
    const outputSha256 = sha256(observedOutput);
    const expectedOutputSha256 = sha256(task.expectedOutput);
    const exactBytes = observedOutput === task.expectedOutput;
    const trailingNewline = observedOutput.endsWith("\n");
    const sorted = observedOutput === sortLines(observedOutput);
    const success = exactBytes && trailingNewline && sorted;

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
        name: "open-agent-lab-scripted-driver",
        version: "0.0.0",
        revision: "sort-lines-bytewise-v1",
      },
      model: {
        provider: "scripted",
        name: "deterministic-sort-lines-v1",
        parameters: { temperature: 0 },
      },
      limits: {
        maxSteps: 9,
        wallTimeMs: 10_000,
        maxInputTokens: 0,
        maxOutputTokens: 0,
        maxCostUsd: 0,
      },
      metadata: {
        network: "disabled",
        taskRevision: task.revision,
      },
    };

    const events: RunEventV1[] = [
      event(runId, createdAt, 0, "run.started", { taskId: task.id }),
      event(runId, createdAt, 1, "model.requested", { instruction: task.instruction }),
      event(runId, createdAt, 2, "model.responded", {
        plan: ["read input.txt", "sort non-empty lines", "write output.txt", "verify exact bytes"],
      }),
      event(runId, createdAt, 3, "tool.started", { tool: "workspace.read", path: "input.txt" }),
      event(runId, createdAt, 4, "tool.completed", {
        tool: "workspace.read",
        path: "input.txt",
        sha256: inputSha256,
      }),
      event(runId, createdAt, 5, "tool.started", { tool: "workspace.write", path: "output.txt" }),
      event(runId, createdAt, 6, "tool.completed", {
        tool: "workspace.write",
        path: "output.txt",
        sha256: outputSha256,
      }),
      event(runId, createdAt, 7, "verification.completed", {
        exactBytes,
        trailingNewline,
        sorted,
      }),
      event(runId, createdAt, 8, "run.completed", { status: success ? "succeeded" : "failed" }),
    ];

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
        profile: "deterministic-smoke/v1",
        network: "disabled",
        success: true,
      },
    });

    return { outputDirectory: options.outputDirectory, manifest, result };
  } finally {
    await rm(workspace, { force: true, recursive: true });
  }
}

export async function replaySmokeEvidence(directory: string): Promise<SmokeReplaySummary> {
  const verified = await verifyEvidenceBundle(directory);
  const task = parseSmokeTask(JSON.parse(await readFile(join(directory, "task.json"), "utf8")) as unknown);
  const spec = parseJsonObject<RunSpecV1>(await readFile(join(directory, "run-spec.json"), "utf8"), "Run spec");
  const result = parseJsonObject<SmokeResult>(await readFile(join(directory, "result.json"), "utf8"), "Smoke result");
  const input = await readFile(join(directory, "workspace/input.txt"), "utf8");
  const output = await readFile(join(directory, "workspace/output.txt"), "utf8");
  const eventLines = (await readFile(join(directory, "events.jsonl"), "utf8"))
    .split("\n")
    .filter((line) => line.length > 0);

  if (spec.runId !== verified.manifest.runId || result.runId !== spec.runId) {
    throw new Error("Smoke evidence run IDs do not agree.");
  }
  if (spec.task.id !== task.id || result.taskId !== task.id) {
    throw new Error("Smoke evidence task IDs do not agree.");
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
  if (events.length !== spec.limits.maxSteps || events.at(-1)?.type !== "run.completed") {
    throw new Error("Smoke event log is incomplete.");
  }

  return {
    manifestId: verified.manifest.manifestId,
    runId: spec.runId,
    taskId: task.id,
    success: true,
    eventCount: events.length,
  };
}

export const smokeTaskPath = fileURLToPath(taskUrl);
export const smokeTaskDirectory = dirname(smokeTaskPath);
