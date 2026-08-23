import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { canonicalJson, type EvidenceManifestV1 } from "@open-agent-lab/contracts";
import {
  manifestBodyOf,
  manifestIdFor,
  verifyEvidenceBundle,
} from "@open-agent-lab/evidence";

import {
  cleanRepositoryRevision,
  loadRouteProbeContract,
  runSyntheticCodexRouteProbe,
  safeCodexEventProjection,
  verifyCodexRouteProbeBundle,
} from "../src/codex-route-probe.js";
import { startResponsesFixture } from "../src/responses-fixture.js";

test("frozen route-probe contracts match the benchmark provider variants", async () => {
  const [deepseek, zai] = await Promise.all([
    loadRouteProbeContract("deepseek"),
    loadRouteProbeContract("zai"),
  ]);

  assert.equal(deepseek.model, "deepseek-v4-pro");
  assert.equal(deepseek.reasoning, "high");
  assert.equal(deepseek.contextWindow, 1_048_576);
  assert.equal(zai.model, "glm-5.3");
  assert.equal(zai.reasoning, "max");
  assert.equal(zai.contextWindow, 1_000_000);
  assert.equal(deepseek.codexVersion, "0.149.0");
  assert.match(deepseek.codexSha256, /^sha256:[a-f0-9]{64}$/u);
  assert.match(deepseek.relayBuildId, /^sha256:[a-f0-9]{64}$/u);
});

test("Codex event projection retains only the exact safe command lifecycle", () => {
  const command = "printf 'open-agent-lab-route-probe-v1\\n' > route-probe-effect.txt";
  const displayedCommand = `/bin/bash -lc ${JSON.stringify(command)}`;
  const stdout = [
    { type: "thread.started", thread_id: "thread-secret" },
    { type: "turn.started" },
    {
      type: "item.started",
      item: {
        id: "item-1",
        type: "command_execution",
        command: displayedCommand,
        status: "in_progress",
      },
    },
    {
      type: "item.completed",
      item: {
        id: "item-1",
        type: "command_execution",
        command: displayedCommand,
        status: "completed",
        exit_code: 0,
        aggregated_output: "must-not-be-retained",
      },
    },
    { type: "item.completed", item: { id: "item-2", type: "agent_message", text: "private" } },
    { type: "turn.completed", usage: { input_tokens: 1, output_tokens: 1 } },
  ].map((event) => JSON.stringify(event)).join("\n");

  const projected = safeCodexEventProjection(stdout);
  assert.equal(projected.summary.turnStarted, 1);
  assert.equal(projected.summary.commandExecutions, 1);
  assert.ok(!projected.content.includes("must-not-be-retained"));
  assert.ok(!projected.content.includes("private"));
  assert.ok(!projected.content.includes("thread-secret"));
  assert.throws(
    () =>
      safeCodexEventProjection(
        `${JSON.stringify({ type: "item.completed", item: { id: "x", type: "file_change" } })}\n`,
      ),
    /invalid item/u,
  );
  assert.throws(
    () =>
      safeCodexEventProjection(
        [
          { type: "turn.completed" },
          {
            type: "item.started",
            item: {
              id: "item-1",
              type: "command_execution",
              command: "cat /etc/passwd",
              status: "completed",
              exit_code: 7,
            },
          },
          { type: "thread.started" },
          {
            type: "item.completed",
            item: {
              id: "item-1",
              type: "command_execution",
              command: displayedCommand,
              status: "completed",
              exit_code: 0,
            },
          },
          { type: "turn.started" },
        ].map((event) => JSON.stringify(event)).join("\n"),
      ),
    /exact single-command/u,
  );
  assert.throws(
    () => safeCodexEventProjection(`${stdout}\n${JSON.stringify({ type: "thread.metadata" })}`),
    /unknown event/u,
  );
});

test("synthetic route probes reject live provider URLs before inspecting Codex", async () => {
  await assert.rejects(
    runSyntheticCodexRouteProbe({
      provider: "deepseek",
      providerKey: "must-not-be-used",
      outputDirectory: "/must-not-be-created",
      codexPath: "/must-not-be-inspected",
      sourceRevision: "a".repeat(40),
      upstreamResponsesUrl: "https://api.deepseek.com/responses",
    }),
    /exact HTTP loopback fixture URL/u,
  );
});

test("synthetic route probes reject an unbound source revision before inspecting Codex", async () => {
  await assert.rejects(
    runSyntheticCodexRouteProbe({
      provider: "deepseek",
      providerKey: "must-not-be-used",
      outputDirectory: "/must-not-be-created",
      codexPath: "/must-not-be-inspected",
      sourceRevision: "0".repeat(40),
      upstreamResponsesUrl: "http://127.0.0.1:1/responses",
    }),
    /current clean repository revision/u,
  );
});

test("repository revision rejects inherited Git metadata and dirty roots", async (t) => {
  const parent = await mkdtemp(join(tmpdir(), "open-agent-lab-parent-repository-"));
  const submodule = await mkdtemp(join(tmpdir(), "open-agent-lab-submodule-"));
  t.after(async () => rm(parent, { force: true, recursive: true }));
  t.after(async () => rm(submodule, { force: true, recursive: true }));
  execFileSync("git", ["init", "--quiet"], { cwd: parent });
  await assert.rejects(cleanRepositoryRevision(parent), /current clean repository/u);
  await writeFile(join(parent, ".gitignore"), "ignored/\n", "utf8");
  execFileSync("git", ["add", ".gitignore"], { cwd: parent });
  execFileSync(
    "git",
    [
      "-c",
      "user.name=Open Agent Lab",
      "-c",
      "user.email=open-agent-lab@example.invalid",
      "commit",
      "--quiet",
      "--message=fixture",
    ],
    { cwd: parent },
  );
  const revision = execFileSync("git", ["rev-parse", "HEAD"], {
    cwd: parent,
    encoding: "utf8",
  }).trim();
  const inherited = join(parent, "ignored", "source");
  await mkdir(inherited, { recursive: true });

  await assert.rejects(cleanRepositoryRevision(inherited), /current clean repository/u);
  assert.equal(await cleanRepositoryRevision(parent), revision);
  execFileSync("git", ["checkout", "--detach", "--quiet"], { cwd: parent });
  assert.equal(await cleanRepositoryRevision(parent), revision);
  await writeFile(join(parent, ".gitignore"), "ignored/\nchanged\n", "utf8");
  await assert.rejects(cleanRepositoryRevision(parent), /current clean repository/u);
  await writeFile(join(parent, ".gitignore"), "ignored/\n", "utf8");
  assert.equal(await cleanRepositoryRevision(parent), revision);

  await writeFile(join(submodule, "tracked"), "clean\n", "utf8");
  execFileSync("git", ["init", "--quiet"], { cwd: submodule });
  execFileSync("git", ["add", "tracked"], { cwd: submodule });
  execFileSync(
    "git",
    [
      "-c",
      "user.name=Open Agent Lab",
      "-c",
      "user.email=open-agent-lab@example.invalid",
      "commit",
      "--quiet",
      "--message=fixture",
    ],
    { cwd: submodule },
  );
  execFileSync(
    "git",
    ["-c", "protocol.file.allow=always", "submodule", "add", "--quiet", submodule, "dependency"],
    { cwd: parent },
  );
  execFileSync(
    "git",
    [
      "-c",
      "user.name=Open Agent Lab",
      "-c",
      "user.email=open-agent-lab@example.invalid",
      "commit",
      "--quiet",
      "--message=submodule",
    ],
    { cwd: parent },
  );
  execFileSync("git", ["config", "submodule.dependency.ignore", "all"], { cwd: parent });
  await writeFile(join(parent, "dependency", "tracked"), "dirty\n", "utf8");
  await assert.rejects(cleanRepositoryRevision(parent), /current clean repository/u);
  await writeFile(join(parent, "dependency", "tracked"), "clean\n", "utf8");
  assert.match(await cleanRepositoryRevision(parent), /^[a-f0-9]{40}$/u);
  await writeFile(join(parent, "untracked"), "dirty\n", "utf8");
  await assert.rejects(cleanRepositoryRevision(parent), /current clean repository/u);
});

test(
  "installed Codex executes both frozen provider profiles through the synthetic route",
  { skip: process.env.OPEN_AGENT_LAB_CODEX_BIN === undefined },
  async (t) => {
    const sourceRevision = await cleanRepositoryRevision();
    const parent = await mkdtemp(join(tmpdir(), "open-agent-lab-route-probe-test-"));
    t.after(async () => rm(parent, { force: true, recursive: true }));
    for (const provider of ["deepseek", "zai"] as const) {
      const output = join(parent, `bundle-${provider}`);
      const upstreamKey = `synthetic-${provider}-key-not-for-a-provider`;
      const contract = await loadRouteProbeContract(provider);
      const fixture = await startResponsesFixture({
        bearer: upstreamKey,
        model: contract.model,
        command: "printf 'open-agent-lab-route-probe-v1\\n' > route-probe-effect.txt",
        finalMessage: "Route probe complete.",
      });
      try {
        const result = await runSyntheticCodexRouteProbe({
          provider,
          providerKey: upstreamKey,
          outputDirectory: output,
          codexPath: process.env.OPEN_AGENT_LAB_CODEX_BIN as string,
          sourceRevision,
          createdAt: "2026-08-23T00:00:00.000Z",
          upstreamResponsesUrl: `${fixture.baseUrl}/responses`,
        });

        assert.equal(result.ok, true);
        assert.equal(result.liveProviderConformance, false);
        assert.equal(result.benchmarkStartAuthorized, false);
        assert.equal(fixture.snapshot().requests.length, 2);
        const verified = await verifyCodexRouteProbeBundle(output);
        assert.equal(verified.manifestId, result.manifestId);
        assert.equal(verified.liveProviderConformance, false);
        assert.equal(verified.benchmarkStartAuthorized, false);
        await verifyEvidenceBundle(output);

        for (const name of [
          "manifest.json",
          "codex-events.json",
          "provider-metadata.ndjson",
          "provider-metadata.ndjson.sealed",
          "route-probe-effect.txt",
        ]) {
          assert.ok(!(await readFile(join(output, name), "utf8")).includes(upstreamKey));
        }

        const manifestPath = join(output, "manifest.json");
        const originalManifest = await readFile(manifestPath, "utf8");
        const wrongSource = JSON.parse(originalManifest) as EvidenceManifestV1;
        assert.ok(wrongSource.metadata);
        wrongSource.metadata.sourceRevision = "0".repeat(40);
        wrongSource.manifestId = manifestIdFor(manifestBodyOf(wrongSource));
        await writeFile(manifestPath, canonicalJson(wrongSource), "utf8");
        await assert.rejects(verifyCodexRouteProbeBundle(output), /retained evidence/u);
        await writeFile(manifestPath, originalManifest, "utf8");

        const mixed = JSON.parse(originalManifest) as EvidenceManifestV1;
        mixed.runId = "route-probe-mixed-run";
        mixed.manifestId = manifestIdFor(manifestBodyOf(mixed));
        await writeFile(manifestPath, canonicalJson(mixed), "utf8");
        await assert.rejects(verifyCodexRouteProbeBundle(output), /two clean provider responses/u);
        await writeFile(manifestPath, originalManifest, "utf8");

        await writeFile(join(output, "route-probe-effect.txt"), "tampered\n", "utf8");
        await assert.rejects(verifyCodexRouteProbeBundle(output));
      } finally {
        await fixture.close();
      }
    }
  },
);
