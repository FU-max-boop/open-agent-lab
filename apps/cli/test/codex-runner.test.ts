import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { chmod, mkdtemp, readFile, realpath, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  buildCodexInvocation,
  buildCodexProbeInvocation,
  buildCodexRelayInvocation,
  CODEX_RELAY_ENV_KEY,
  nativeProviderProfile,
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
  assert.match(argv, /--sandbox workspace-write/);
  assert.doesNotMatch(argv, /sandbox_workspace_write\.network_access=true/);
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
      "const setting = process.argv.find((value) => value.startsWith('model_catalog_json='));",
      "if (setting === undefined) process.exit(2);",
      "const catalogPath = JSON.parse(setting.slice(setting.indexOf('=') + 1));",
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
      "    catalog: JSON.parse(readFileSync(catalogPath, 'utf8')),",
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
    model: "deepseek-model_catalog_json-alias",
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
    catalog: { models: Record<string, unknown>[] };
  };

  assert.equal(code, 0);
  assert.equal(observed.cwd, await realpath(directory));
  assert.equal(observed.input, prompt);
  assert.equal(observed.keyPresent, true);
  assert.equal(observed.unrelatedSecretPresent, false);
  assert.equal(observed.authSecretPresent, false);
  const catalogArg = observed.argv.findIndex((value) => value.startsWith("model_catalog_json="));
  assert.ok(catalogArg > 0);
  const publicArgs = [...observed.argv];
  publicArgs.splice(catalogArg - 1, 2);
  assert.deepEqual(publicArgs, invocation.args);
  assert.equal(observed.catalog.models[0]?.slug, "deepseek-model_catalog_json-alias");
  assert.equal(observed.catalog.models[0]?.apply_patch_tool_type, "freeform");
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

test("runner rejects caller-managed model catalog paths", async () => {
  const base = buildCodexInvocation({
    provider: "deepseek",
    workspace: ".",
    prompt: "run",
    codexPath: "/definitely/not/executed",
  });
  const invocation = Object.freeze({
    ...base,
    args: Object.freeze([
      ...base.args.slice(0, -1),
      "--config",
      'model_catalog_json = "/tmp/caller.json"',
      "-",
    ]),
  });
  await assert.rejects(
    runCodexInvocation(
      invocation,
      { DEEPSEEK_API_KEY: "fixture-key" },
      { stdout: () => undefined, stderr: () => undefined },
    ),
    /managed internally/u,
  );
});

test("runner kills a bounded execution that exceeds its output budget", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "open-agent-lab-codex-limit-test-"));
  t.after(async () => rm(directory, { force: true, recursive: true }));
  const fakeCodex = join(directory, "noisy-codex.mjs");
  await writeFile(
    fakeCodex,
    "#!/usr/bin/env node\nprocess.stdin.resume();\nprocess.stdout.write('x'.repeat(1024));\nsetInterval(() => {}, 1000);\n",
    "utf8",
  );
  await chmod(fakeCodex, 0o755);
  const invocation = buildCodexInvocation({
    provider: "deepseek",
    workspace: directory,
    prompt: "run",
    codexPath: fakeCodex,
  });
  const signalListeners = [process.listenerCount("SIGINT"), process.listenerCount("SIGTERM")];

  await assert.rejects(
    runCodexInvocation(
      invocation,
      { ...process.env, DEEPSEEK_API_KEY: "test-key" },
      { stdout: () => undefined, stderr: () => undefined },
      { timeoutMs: 5_000, maxStdoutBytes: 64, maxStderrBytes: 64 },
    ),
    /stdout limit/u,
  );
  assert.deepEqual(
    [process.listenerCount("SIGINT"), process.listenerCount("SIGTERM")],
    signalListeners,
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
  assert.match(args, /--sandbox workspace-write/);
  assert.match(args, /sandbox_workspace_write\.network_access=true/);
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

test("route-probe invocation keeps exact provider identity behind a zero-retry relay", () => {
  const deepseek = nativeProviderProfile("deepseek");
  const zai = nativeProviderProfile("zai");
  const invocation = buildCodexRelayInvocation({
    provider: "deepseek",
    workspace: ".",
    prompt: "perform one tool round",
    baseUrl: "http://127.0.0.1:43123/v1",
  });
  const args = invocation.args.join(" ");

  assert.equal(deepseek.responsesUrl, "https://api.deepseek.com/responses");
  assert.equal(zai.responsesUrl, "https://api.z.ai/api/v1/responses");
  assert.equal(invocation.provider, "deepseek");
  assert.equal(invocation.model, "deepseek-v4-pro");
  assert.equal(invocation.reasoning, "high");
  assert.equal(invocation.requiredEnv, CODEX_RELAY_ENV_KEY);
  assert.equal(invocation.apiScope, "standard-api");
  assert.match(args, /base_url = "http:\/\/127\.0\.0\.1:43123\/v1"/u);
  assert.match(args, /request_max_retries = 0/u);
  assert.match(args, /stream_max_retries = 0/u);
  assert.doesNotMatch(args, /sandbox_workspace_write\.network_access=true/u);
  assert.match(args, /shell_environment_policy\.set\.OPEN_AGENT_LAB_RELAY_TOKEN=""/u);
  assert.doesNotMatch(args, /model_catalog_json/u);
  assert.throws(
    () =>
      buildCodexRelayInvocation({
        provider: "deepseek",
        workspace: ".",
        prompt: "probe",
        baseUrl: "https://example.com/v1",
      }),
    /loopback HTTP URL/u,
  );
  assert.throws(
    () =>
      buildCodexRelayInvocation({
        provider: "zai",
        workspace: ".",
        prompt: "probe",
        baseUrl: "http://user:secret@127.0.0.1:43123/v1",
      }),
    /loopback HTTP URL/u,
  );
  assert.throws(
    () =>
      buildCodexRelayInvocation({
        provider: "deepseek",
        workspace: ".",
        prompt: "probe",
        model: "deepseek-v4-flash",
        baseUrl: "http://127.0.0.1:43123/v1",
      }),
    /frozen provider profile/u,
  );
});

test("runner materializes the model catalog only inside its private home", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "open-agent-lab-codex-catalog-test-"));
  t.after(async () => rm(directory, { force: true, recursive: true }));
  const fakeCodex = join(directory, "catalog-codex.mjs");
  await writeFile(
    fakeCodex,
    [
      "#!/usr/bin/env node",
      'import { readFileSync, statSync } from "node:fs";',
      "const setting = process.argv.find((value) => value.startsWith('model_catalog_json='));",
      "if (setting === undefined) process.exit(2);",
      "const path = JSON.parse(setting.slice(setting.indexOf('=') + 1));",
      "process.stdin.resume();",
      "process.stdin.on('end', () => process.stdout.write(JSON.stringify({",
      "  path,",
      "  privatePath: path === `${process.env.CODEX_HOME}/model-catalog.json`,",
      "  mode: statSync(path).mode & 0o777,",
      "  catalog: JSON.parse(readFileSync(path, 'utf8'))",
      "})));",
    ].join("\n"),
    "utf8",
  );
  await chmod(fakeCodex, 0o755);
  for (const expected of [
    { provider: "deepseek" as const, model: "deepseek-v4-pro", reasoning: "high", context: 1_048_576 },
    { provider: "zai" as const, model: "glm-5.3", reasoning: "max", context: 1_000_000 },
  ]) {
    const invocation = buildCodexRelayInvocation({
      provider: expected.provider,
      workspace: directory,
      prompt: "perform one tool round",
      baseUrl: "http://127.0.0.1:43123/v1",
      codexPath: fakeCodex,
    });
    let output = "";
    const code = await runCodexInvocation(
      invocation,
      {
        ...process.env,
        HOME: "/caller/home/must-not-be-used",
        CODEX_HOME: "/caller/codex-home/must-not-be-used",
        [CODEX_RELAY_ENV_KEY]: "fixture-capability",
      },
      { stdout: (chunk) => (output += chunk), stderr: () => undefined },
    );
    const observed = JSON.parse(output) as {
      path: string;
      privatePath: boolean;
      mode: number;
      catalog: { models: Record<string, unknown>[] };
    };
    const model = observed.catalog.models[0];

    assert.equal(code, 0);
    assert.equal(observed.privatePath, true);
    assert.equal(observed.mode, 0o600);
    assert.equal(observed.catalog.models.length, 1);
    assert.deepEqual(Object.keys(model as object).sort(), [
      "apply_patch_tool_type",
      "context_window",
      "default_reasoning_level",
      "default_reasoning_summary",
      "display_name",
      "effective_context_window_percent",
      "experimental_supported_tools",
      "include_apps_usage_instructions",
      "include_plugin_usage_instructions",
      "include_skills_usage_instructions",
      "input_modalities",
      "max_context_window",
      "model_messages",
      "priority",
      "shell_type",
      "slug",
      "support_verbosity",
      "supported_in_api",
      "supported_reasoning_levels",
      "supports_reasoning_summary_parameter",
      "truncation_policy",
      "visibility",
    ]);
    assert.equal(model?.slug, expected.model);
    assert.equal(model?.default_reasoning_level, expected.reasoning);
    assert.equal(model?.apply_patch_tool_type, "freeform");
    assert.equal(model?.context_window, expected.context);
    assert.equal(model?.max_context_window, expected.context);
    assert.equal(model?.effective_context_window_percent, 95);
    assert.equal(model?.supports_reasoning_summary_parameter, false);
    assert.deepEqual(model?.input_modalities, ["text"]);
    const messages = model?.model_messages as Record<string, unknown>;
    const instructions = messages.instructions_template;
    assert.equal(typeof instructions, "string");
    assert.equal(
      createHash("sha256").update(instructions as string).digest("hex"),
      "ac8ae107a0d72fe3476b430afb161ea4e67da2e446d778aefc44828160559807",
    );
    await assert.rejects(readFile(observed.path, "utf8"), /ENOENT/u);
  }
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
      sawDeveloperInstruction: false,
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

test(
  "installed Codex forwards the frozen verification instruction",
  { skip: process.env.OPEN_AGENT_LAB_CODEX_BIN === undefined },
  async () => {
    const instruction = await readFile(
      new URL("../../../benchmarks/terminal_bench/verify-instruction-v1.txt", import.meta.url),
      "utf8",
    );
    const result = await runCodexProbe(
      process.env.OPEN_AGENT_LAB_CODEX_BIN,
      true,
      instruction,
    );
    assert.equal(result.ok, true);
    assert.equal(result.sawDeveloperInstruction, true);
  },
);
