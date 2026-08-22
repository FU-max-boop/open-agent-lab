import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import test from "node:test";

import { verifyEvidenceBundle } from "@open-agent-lab/evidence";

import { runCli } from "../src/cli.js";
import { replaySmokeEvidence, runSmoke } from "../src/smoke.js";

test("doctor reports a supported deterministic runtime", async () => {
  const output: string[] = [];
  const errors: string[] = [];
  const exitCode = await runCli(["doctor"], {
    stdout: (message) => output.push(message),
    stderr: (message) => errors.push(message),
  });
  assert.equal(exitCode, 0);
  assert.deepEqual(errors, []);
  const report = JSON.parse(output.join("\n")) as { ok: boolean };
  assert.equal(report.ok, true);
});

test("smoke run creates an independently verifiable and replayable bundle", async (t) => {
  const parent = await mkdtemp(join(tmpdir(), "open-agent-lab-cli-test-"));
  t.after(async () => rm(parent, { force: true, recursive: true }));
  const bundle = join(parent, "bundle");
  const repeatedBundle = join(parent, "bundle-repeat");

  const summary = await runSmoke({
    outputDirectory: bundle,
    createdAt: "2026-01-01T00:00:00.000Z",
  });
  const verified = await verifyEvidenceBundle(bundle);
  const replayed = await replaySmokeEvidence(bundle);
  const repeated = await runSmoke({
    outputDirectory: repeatedBundle,
    createdAt: "2026-01-01T00:00:00.000Z",
  });

  assert.equal(summary.result.success, true);
  assert.equal(summary.kernelState.lifecycle, "succeeded");
  assert.equal(summary.kernelState.completed.length, 2);
  assert.equal(verified.manifest.manifestId, summary.manifest.manifestId);
  assert.equal(repeated.manifest.manifestId, summary.manifest.manifestId);
  assert.equal(verified.fileCount, 9);
  assert.equal(replayed.success, true);
  assert.equal(replayed.eventCount, 6);
  assert.equal(replayed.kernelEventCount, 6);
});

test("evidence verification rejects a modified payload", async (t) => {
  const parent = await mkdtemp(join(tmpdir(), "open-agent-lab-tamper-test-"));
  t.after(async () => rm(parent, { force: true, recursive: true }));
  const bundle = join(parent, "bundle");
  await runSmoke({ outputDirectory: bundle, createdAt: "2026-01-01T00:00:00.000Z" });

  const outputPath = join(bundle, "workspace/output.txt");
  const original = await readFile(outputPath, "utf8");
  await writeFile(outputPath, original.replace("alpha", "tampered"), "utf8");

  await assert.rejects(verifyEvidenceBundle(bundle));
});

test("codex-run dry-run exposes exact routing without requiring or printing a key", async () => {
  const output: string[] = [];
  const errors: string[] = [];
  const exitCode = await runCli(
    [
      "codex-run",
      "--provider",
      "zai",
      "--workspace",
      ".",
      "--model",
      "glm-5.3",
      "--prompt",
      "fix the task",
      "--dry-run",
    ],
    {
      stdout: (message) => output.push(message),
      stderr: (message) => errors.push(message),
    },
  );

  assert.equal(exitCode, 0);
  assert.deepEqual(errors, []);
  const report = JSON.parse(output.join("\n")) as {
    command: string;
    args: string[];
    cwd: string;
    provider: string;
    model: string;
    reasoning: string;
    requiredEnv: string;
    apiScope: string;
    promptBytes: number;
  };
  assert.equal(report.command, "codex");
  assert.equal(report.cwd, resolve("."));
  assert.equal(report.provider, "zai");
  assert.equal(report.model, "glm-5.3");
  assert.equal(report.reasoning, "max");
  assert.equal(report.requiredEnv, "ZAI_API_KEY");
  assert.equal(report.apiScope, "coding-plan");
  assert.equal(report.promptBytes, 12);
  assert.ok(report.args.includes("--ignore-user-config"));
  assert.ok(!output.join("\n").includes("fix the task"));
});
