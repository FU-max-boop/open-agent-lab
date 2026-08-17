import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
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

  const summary = await runSmoke({
    outputDirectory: bundle,
    createdAt: "2026-01-01T00:00:00.000Z",
  });
  const verified = await verifyEvidenceBundle(bundle);
  const replayed = await replaySmokeEvidence(bundle);

  assert.equal(summary.result.success, true);
  assert.equal(verified.manifest.manifestId, summary.manifest.manifestId);
  assert.equal(verified.fileCount, 7);
  assert.equal(replayed.success, true);
  assert.equal(replayed.eventCount, 9);
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
