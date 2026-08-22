import { spawn } from "node:child_process";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

export type OpenModelProvider = "deepseek" | "zai";
export type CodexProvider = OpenModelProvider | "probe";
export type ReasoningEffort = "low" | "high" | "max";

interface ProviderProfile {
  readonly id: CodexProvider;
  readonly name: string;
  readonly baseUrl: string;
  readonly envKey: string;
  readonly defaultModel: string;
  readonly defaultReasoning: ReasoningEffort;
  readonly reasoning: readonly ReasoningEffort[];
  readonly contextWindow: number;
  readonly scope: "standard-api" | "coding-plan" | "loopback-probe";
  readonly requestRetries: number;
  readonly streamRetries: number;
}

const PROFILES: Readonly<Record<OpenModelProvider, ProviderProfile>> = {
  deepseek: Object.freeze({
    id: "deepseek",
    name: "DeepSeek",
    baseUrl: "https://api.deepseek.com/",
    envKey: "DEEPSEEK_API_KEY",
    defaultModel: "deepseek-v4-pro",
    defaultReasoning: "high",
    reasoning: Object.freeze(["high", "max"] as const),
    contextWindow: 1_048_576,
    scope: "standard-api",
    requestRetries: 4,
    streamRetries: 5,
  }),
  zai: Object.freeze({
    id: "zai",
    name: "Z.AI Coding Plan",
    baseUrl: "https://api.z.ai/api/v1",
    envKey: "ZAI_API_KEY",
    defaultModel: "glm-5.3",
    defaultReasoning: "max",
    reasoning: Object.freeze(["low", "high", "max"] as const),
    contextWindow: 1_000_000,
    scope: "coding-plan",
    requestRetries: 4,
    streamRetries: 5,
  }),
};

export interface CodexRunSpec {
  provider: OpenModelProvider;
  workspace: string;
  prompt: string;
  model?: string;
  reasoning?: ReasoningEffort;
  codexPath?: string;
}

export interface CodexInvocation {
  readonly command: string;
  readonly args: readonly string[];
  readonly cwd: string;
  readonly stdin: string;
  readonly provider: CodexProvider;
  readonly model: string;
  readonly reasoning: ReasoningEffort;
  readonly requiredEnv: string;
  readonly apiScope: ProviderProfile["scope"];
}

export interface CodexRunIo {
  stdout: (chunk: string) => void;
  stderr: (chunk: string) => void;
}

const defaultIo: CodexRunIo = {
  stdout: (chunk) => process.stdout.write(chunk),
  stderr: (chunk) => process.stderr.write(chunk),
};

const PROCESS_ENV = [
  "PATH",
  "USER",
  "LOGNAME",
  "SHELL",
  "TMPDIR",
  "TMP",
  "TEMP",
  "LANG",
  "LC_ALL",
  "TERM",
  "COLORTERM",
  "NO_COLOR",
  "SSL_CERT_FILE",
  "SSL_CERT_DIR",
  "NODE_EXTRA_CA_CERTS",
  "SystemRoot",
  "ComSpec",
  "PATHEXT",
  "USERPROFILE",
] as const;

function profile(provider: OpenModelProvider): ProviderProfile {
  const value: ProviderProfile | undefined = PROFILES[provider];
  if (value === undefined) throw new Error(`Unknown Codex provider: ${provider}`);
  return value;
}

function requireModel(value: string): string {
  const model = value.trim();
  if (!/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/.test(model)) {
    throw new Error("Model must be a 1-128 character provider model ID.");
  }
  return model;
}

function providerToml(value: ProviderProfile): string {
  return `{ name = ${JSON.stringify(value.name)}, base_url = ${JSON.stringify(value.baseUrl)}, env_key = ${JSON.stringify(value.envKey)}, wire_api = "responses", request_max_retries = ${value.requestRetries}, stream_max_retries = ${value.streamRetries}, stream_idle_timeout_ms = 300000, supports_websockets = false }`;
}

function invocation(
  spec: Omit<CodexRunSpec, "provider">,
  selected: ProviderProfile,
): CodexInvocation {
  const model = requireModel(spec.model ?? selected.defaultModel);
  const reasoning = spec.reasoning ?? selected.defaultReasoning;
  if (!selected.reasoning.includes(reasoning)) {
    throw new Error(
      `${selected.name} does not expose reasoning effort '${reasoning}' in this profile.`,
    );
  }
  if (spec.prompt.trim() === "") throw new Error("Codex prompt cannot be empty.");

  const cwd = resolve(spec.workspace);
  const probeNetwork =
    selected.scope === "loopback-probe"
      ? ["--config", "sandbox_workspace_write.network_access=true"]
      : [];
  return Object.freeze({
    command: spec.codexPath ?? "codex",
    args: Object.freeze([
      "exec",
      "--ignore-user-config",
      "--ignore-rules",
      "--strict-config",
      "--ephemeral",
      "--json",
      "--color",
      "never",
      "--sandbox",
      "workspace-write",
      ...probeNetwork,
      "--skip-git-repo-check",
      "--cd",
      cwd,
      "--model",
      model,
      "--config",
      `model_provider=${JSON.stringify(selected.id)}`,
      "--config",
      `model_reasoning_effort=${JSON.stringify(reasoning)}`,
      "--config",
      `model_context_window=${selected.contextWindow}`,
      "--config",
      `model_providers.${selected.id}=${providerToml(selected)}`,
      "--config",
      "shell_environment_policy.ignore_default_excludes=false",
      "--config",
      `shell_environment_policy.set.${selected.envKey}=""`,
      "-",
    ]),
    cwd,
    stdin: spec.prompt,
    provider: selected.id,
    model,
    reasoning,
    requiredEnv: selected.envKey,
    apiScope: selected.scope,
  });
}

/** Build a secret-free, user-config-independent Codex invocation. */
export function buildCodexInvocation(spec: CodexRunSpec): CodexInvocation {
  return invocation(spec, profile(spec.provider));
}

export interface CodexProbeRunSpec {
  workspace: string;
  prompt: string;
  baseUrl: string;
  codexPath?: string;
}

/** Build a deterministic probe invocation; the endpoint must be loopback. */
export function buildCodexProbeInvocation(spec: CodexProbeRunSpec): CodexInvocation {
  const url = new URL(spec.baseUrl);
  if (
    url.protocol !== "http:" ||
    (url.hostname !== "127.0.0.1" && url.hostname !== "[::1]" && url.hostname !== "localhost") ||
    url.username !== "" ||
    url.password !== ""
  ) {
    throw new Error("Codex probe endpoint must be an unauthenticated loopback HTTP URL.");
  }
  return invocation(spec, {
    id: "probe",
    name: "Open Agent Lab loopback probe",
    baseUrl: url.toString().replace(/\/$/, ""),
    envKey: "OPEN_AGENT_LAB_PROBE_KEY",
    defaultModel: "open-agent-lab-probe",
    defaultReasoning: "high",
    reasoning: Object.freeze(["high"]),
    contextWindow: 32_768,
    scope: "loopback-probe",
    requestRetries: 0,
    streamRetries: 0,
  });
}

/** Execute Codex without ever copying the provider secret into argv or config. */
export async function runCodexInvocation(
  invocation: CodexInvocation,
  env: NodeJS.ProcessEnv = process.env,
  io: CodexRunIo = defaultIo,
): Promise<number> {
  const apiKey = env[invocation.requiredEnv];
  if (apiKey === undefined || apiKey.trim() === "") {
    throw new Error(`${invocation.requiredEnv} is required for ${invocation.provider}.`);
  }

  const childEnv: NodeJS.ProcessEnv = { [invocation.requiredEnv]: apiKey };
  for (const name of PROCESS_ENV) {
    if (env[name] !== undefined) childEnv[name] = env[name];
  }

  const codexHome = await mkdtemp(join(tmpdir(), "open-agent-lab-codex-home-"));
  childEnv.HOME = codexHome;
  childEnv.CODEX_HOME = codexHome;

  try {
    await writeFile(join(codexHome, "auth.json"), "{}\n", { mode: 0o600 });
    return await new Promise<number>((resolveExit, reject) => {
      const child = spawn(invocation.command, invocation.args, {
        cwd: invocation.cwd,
        env: childEnv,
        stdio: ["pipe", "pipe", "pipe"],
      });
      child.once("error", reject);
      child.stdin.once("error", reject);
      child.stdout.on("data", (chunk: Buffer) => io.stdout(chunk.toString("utf8")));
      child.stderr.on("data", (chunk: Buffer) => io.stderr(chunk.toString("utf8")));
      child.once("close", (code, signal) => {
        if (signal !== null) {
          reject(new Error(`Codex terminated by signal ${signal}.`));
          return;
        }
        resolveExit(code ?? 1);
      });
      child.stdin.end(invocation.stdin);
    });
  } finally {
    await rm(codexHome, { force: true, recursive: true });
  }
}

export function publicInvocation(invocation: CodexInvocation): object {
  return {
    command: invocation.command,
    args: invocation.args,
    cwd: invocation.cwd,
    provider: invocation.provider,
    model: invocation.model,
    reasoning: invocation.reasoning,
    requiredEnv: invocation.requiredEnv,
    apiScope: invocation.apiScope,
    promptBytes: Buffer.byteLength(invocation.stdin),
  };
}
