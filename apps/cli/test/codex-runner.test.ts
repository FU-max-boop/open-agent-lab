import assert from "node:assert/strict";
import { chmod, mkdtemp, realpath, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  buildCodexInvocation,
  buildCodexProbeInvocation,
  publicInvocation,
  runCodexInvocation,
} from "../src/codex-runner.js";
import { runCodexProbe } from "../src/codex-probe.js";

test("DeepSeek invocation is native Responses, isolated, and secret-free", () => {
  const invocation = buildCodexInvocation({
    provider: "deepseek",
    workspace: "/tmp/workspace",
    prompt: "fix the test",
  });
  const argv = invocation.args.join(" ");

  assert.equal(invocation.model, "deepseek-v4-pro");
  assert.equal(invocation.reasoning, "high");
  assert.equal(invocation.requiredEnv, "DEEPSEEK_API_KEY");
  assert.equal(invocation.apiScope, "standard-api");
  assert.match(argv, /https:\/\/api\.deepseek\.com\//);
  assert.match(argv, /wire_api = "responses"/);
  assert.match(argv, /env_key = "DEEPSEEK_API_KEY"/);
  assert.match(argv, /shell_environment_policy\.ignore_default_excludes=false/);
  assert.match(argv, /shell_environment_policy\.set\.DEEPSEEK_API_KEY=""/);
  assert.ok(invocation.args.includes("--ignore-user-config"));
  assert.ok(invocation.args.includes("--ephemeral"));
  assert.ok(!argv.includes(invocation.stdin));
  assert.ok(!argv.includes("sk-"));
});

test("Z.AI profile is scoped to Coding Plan and pins GLM defaults", () => {
  const invocation = buildCodexInvocation({
    provider: "zai",
    workspace: ".",
    prompt: "inspect the repository",
  });
  const publicView = publicInvocation(invocation) as Record<string, unknown>;

  assert.equal(invocation.model, "glm-5.3");
  assert.equal(invocation.reasoning, "max");
  assert.equal(invocation.requiredEnv, "ZAI_API_KEY");
  assert.equal(invocation.apiScope, "coding-plan");
  assert.match(invocation.args.join(" "), /https:\/\/api\.z\.ai\/api\/v1/);
  assert.equal(publicView.promptBytes, 22);
  assert.ok(!("stdin" in publicView));
});

test("profiles reject unsupported reasoning and unsafe model IDs", () => {
  assert.throws(
    () =>
      buildCodexInvocation({
        provider: "deepseek",
        workspace: ".",
        prompt: "run",
        reasoning: "low",
      }),
    /does not expose reasoning effort 'low'/,
  );
  assert.throws(
    () =>
      buildCodexInvocation({
        provider: "zai",
        workspace: ".",
        prompt: "run",
        model: 'glm" --config secret="leak',
      }),
    /provider model ID/,
  );
});

test("runner sends prompt on stdin and keeps the key out of argv", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "open-agent-lab-codex-test-"));
  t.after(async () => rm(directory, { force: true, recursive: true }));
  const fakeCodex = join(directory, "fake-codex.mjs");
  await writeFile(
    fakeCodex,
    [
      "#!/usr/bin/env node",
      'import { readFileSync } from "node:fs";',
      'let input = "";',
      'process.stdin.setEncoding("utf8");',
      'process.stdin.on("data", (chunk) => { input += chunk; });',
      'process.stdin.on("end", () => {',
      "  process.stdout.write(JSON.stringify({",
      "    argv: process.argv.slice(2),",
      "    cwd: process.cwd(),",
      "    input,",
      '    keyPresent: Boolean(process.env.DEEPSEEK_API_KEY),',
      '    unrelatedSecretPresent: Boolean(process.env.AWS_SECRET_ACCESS_KEY),',
      '    authSecretPresent: readFileSync(`${process.env.CODEX_HOME}/auth.json`, "utf8").includes("test-secret"),',
      "  }));",
      "});",
    ].join("\n"),
    "utf8",
  );
  await chmod(fakeCodex, 0o755);

  const prompt = "make the smallest correct change\nthen test it";
  const invocation = buildCodexInvocation({
    provider: "deepseek",
    workspace: directory,
    prompt,
    codexPath: fakeCodex,
  });
  let output = "";
  const code = await runCodexInvocation(
    invocation,
    {
      ...process.env,
      DEEPSEEK_API_KEY: "test-secret",
      AWS_SECRET_ACCESS_KEY: "must-not-cross",
    },
    { stdout: (chunk) => (output += chunk), stderr: () => undefined },
  );
  const observed = JSON.parse(output) as {
    argv: string[];
    cwd: string;
    input: string;
    keyPresent: boolean;
    unrelatedSecretPresent: boolean;
    authSecretPresent: boolean;
  };

  assert.equal(code, 0);
  assert.equal(observed.cwd, await realpath(directory));
  assert.equal(observed.input, prompt);
  assert.equal(observed.keyPresent, true);
  assert.equal(observed.unrelatedSecretPresent, false);
  assert.equal(observed.authSecretPresent, false);
  assert.ok(!observed.argv.join(" ").includes("test-secret"));
});

test("runner fails before spawning when the provider key is absent", async () => {
  const invocation = buildCodexInvocation({
    provider: "zai",
    workspace: ".",
    prompt: "run",
    codexPath: "/definitely/not/executed",
  });
  await assert.rejects(
    runCodexInvocation(invocation, {}, { stdout: () => undefined, stderr: () => undefined }),
    /ZAI_API_KEY is required/,
  );
});

test("probe configuration is deterministic and restricted to loopback", () => {
  const invocation = buildCodexProbeInvocation({
    workspace: ".",
    prompt: "probe",
    baseUrl: "http://127.0.0.1:43210",
  });
  const args = invocation.args.join(" ");
  assert.equal(invocation.provider, "probe");
  assert.equal(invocation.requiredEnv, "OPEN_AGENT_LAB_PROBE_KEY");
  assert.match(args, /request_max_retries = 0/);
  assert.match(args, /stream_max_retries = 0/);
  assert.throws(
    () =>
      buildCodexProbeInvocation({
        workspace: ".",
        prompt: "probe",
        baseUrl: "https://example.com",
      }),
    /loopback HTTP URL/,
  );
});

test(
  "installed Codex completes a provider-free Responses tool round",
  { skip: process.env.OPEN_AGENT_LAB_CODEX_BIN === undefined },
  async () => {
    const result = await runCodexProbe(process.env.OPEN_AGENT_LAB_CODEX_BIN);
    assert.deepEqual(result, {
      ok: true,
      requests: 2,
      callId: "call_open_agent_lab_probe",
      tool: "exec_command",
      output: "codex-native-responses",
      sawThreadStart: true,
      sawTurnComplete: true,
    });
  },
);

test(
  "installed Codex completes the same tool round through the isolated relay",
  { skip: process.env.OPEN_AGENT_LAB_CODEX_BIN === undefined },
  async () => {
    const result = await runCodexProbe(process.env.OPEN_AGENT_LAB_CODEX_BIN, true);
    assert.equal(result.ok, true);
    assert.equal(result.requests, 2);
    assert.equal(result.output, "codex-native-responses");
  },
);
