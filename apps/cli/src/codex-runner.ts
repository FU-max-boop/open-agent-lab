import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

export type OpenModelProvider = "deepseek" | "zai";
export type CodexProvider = OpenModelProvider | "probe";
export type ReasoningEffort = "low" | "high" | "max";

export const CODEX_RELAY_ENV_KEY = "OPEN_AGENT_LAB_RELAY_TOKEN";

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

export interface CodexRelayRunSpec extends CodexRunSpec {
  baseUrl: string;
}

export interface NativeProviderProfile {
  readonly responsesUrl: string;
  readonly defaultModel: string;
  readonly defaultReasoning: ReasoningEffort;
  readonly contextWindow: number;
}

export interface CodexInvocation {
  readonly command: string;
  readonly args: readonly string[];
  readonly cwd: string;
  readonly stdin: string;
  readonly provider: CodexProvider;
  readonly model: string;
  readonly reasoning: ReasoningEffort;
  readonly contextWindow: number;
  readonly requiredEnv: string;
  readonly apiScope: ProviderProfile["scope"];
}

export interface CodexRunIo {
  stdout: (chunk: string) => void;
  stderr: (chunk: string) => void;
}

export interface CodexRunLimits {
  timeoutMs: number;
  maxStdoutBytes: number;
  maxStderrBytes: number;
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
const BASE_INSTRUCTIONS = new URL(
  "../../../benchmarks/terminal_bench/codex-0.149.0-base-instructions.md",
  import.meta.url,
);
const BASE_INSTRUCTIONS_SHA256 =
  "ac8ae107a0d72fe3476b430afb161ea4e67da2e446d778aefc44828160559807";

function profile(provider: OpenModelProvider): ProviderProfile {
  const value: ProviderProfile | undefined = PROFILES[provider];
  if (value === undefined) throw new Error(`Unknown Codex provider: ${provider}`);
  return value;
}

export function nativeProviderProfile(provider: OpenModelProvider): NativeProviderProfile {
  const selected = profile(provider);
  return Object.freeze({
    responsesUrl: new URL("responses", `${selected.baseUrl.replace(/\/$/u, "")}/`).toString(),
    defaultModel: selected.defaultModel,
    defaultReasoning: selected.defaultReasoning,
    contextWindow: selected.contextWindow,
  });
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

function modelCatalog(
  invocation: CodexInvocation,
  instructions: string,
): string {
  if (!Number.isSafeInteger(invocation.contextWindow) || invocation.contextWindow <= 0) {
    throw new Error("Codex invocation context window is invalid.");
  }
  const model = invocation.model;
  const reasoning = invocation.reasoning;
  return `${JSON.stringify({
    models: [
      {
        slug: model,
        display_name: model,
        default_reasoning_level: reasoning,
        supported_reasoning_levels: [
          { effort: reasoning, description: "Frozen Open Agent Lab effort" },
        ],
        shell_type: "shell_command",
        visibility: "none",
        supported_in_api: true,
        priority: 0,
        model_messages: {
          instructions_template: instructions,
        },
        include_skills_usage_instructions: false,
        include_plugin_usage_instructions: false,
        include_apps_usage_instructions: false,
        supports_reasoning_summary_parameter: false,
        default_reasoning_summary: "none",
        support_verbosity: false,
        apply_patch_tool_type: "freeform",
        truncation_policy: {
          mode: invocation.provider === "deepseek" ? "tokens" : "bytes",
          limit: 10_000,
        },
        context_window: invocation.contextWindow,
        max_context_window: invocation.contextWindow,
        effective_context_window_percent: 95,
        experimental_supported_tools: [],
        input_modalities: ["text"],
      },
    ],
  })}\n`;
}

async function baseInstructions(): Promise<string> {
  const content = await readFile(BASE_INSTRUCTIONS);
  if (
    createHash("sha256").update(content).digest("hex") !== BASE_INSTRUCTIONS_SHA256 ||
    content.at(-1) !== 0x0a
  ) {
    throw new Error("The pinned Codex base instructions drifted.");
  }
  return content.toString("utf8");
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
  const network =
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
      ...network,
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
    contextWindow: selected.contextWindow,
    requiredEnv: selected.envKey,
    apiScope: selected.scope,
  });
}

/** Build a secret-free, user-config-independent Codex invocation. */
export function buildCodexInvocation(spec: CodexRunSpec): CodexInvocation {
  const selected = profile(spec.provider);
  return invocation(spec, selected);
}

function loopbackBaseUrl(value: string): string {
  const url = new URL(value);
  if (
    url.protocol !== "http:" ||
    (url.hostname !== "127.0.0.1" && url.hostname !== "[::1]" && url.hostname !== "localhost") ||
    url.username !== "" ||
    url.password !== "" ||
    url.search !== "" ||
    url.hash !== ""
  ) {
    throw new Error("Codex relay endpoint must be an unauthenticated loopback HTTP URL.");
  }
  return url.toString().replace(/\/$/u, "");
}

/** Build a frozen synthetic route invocation that receives only a relay capability. */
export function buildCodexRelayInvocation(spec: CodexRelayRunSpec): CodexInvocation {
  const selected = profile(spec.provider);
  const relayProfile = {
    ...selected,
    baseUrl: loopbackBaseUrl(spec.baseUrl),
    envKey: CODEX_RELAY_ENV_KEY,
    requestRetries: 0,
    streamRetries: 0,
  };
  const built = invocation(spec, relayProfile);
  if (
    built.model !== selected.defaultModel ||
    built.reasoning !== selected.defaultReasoning
  ) {
    throw new Error(`${selected.name} relay probes require the frozen provider profile.`);
  }
  return built;
}

export interface CodexProbeRunSpec {
  workspace: string;
  prompt: string;
  baseUrl: string;
  codexPath?: string;
  developerInstruction?: string;
}

function withDeveloperInstruction(
  invocation: CodexInvocation,
  instruction: string | undefined,
): CodexInvocation {
  if (instruction === undefined) return invocation;
  if (
    instruction.trim() === "" ||
    instruction.includes("\0") ||
    Buffer.byteLength(instruction) > 16 * 1024
  ) {
    throw new Error("Codex probe developer instruction is invalid.");
  }
  if (invocation.args.at(-1) !== "-") {
    throw new Error("Codex probe invocation no longer reads its prompt from stdin.");
  }
  return Object.freeze({
    ...invocation,
    args: Object.freeze([
      ...invocation.args.slice(0, -1),
      "--config",
      `developer_instructions=${JSON.stringify(instruction)}`,
      "-",
    ]),
  });
}

/** Build a deterministic probe invocation; the endpoint must be loopback. */
export function buildCodexProbeInvocation(spec: CodexProbeRunSpec): CodexInvocation {
  const selected: ProviderProfile = {
    id: "probe",
    name: "Open Agent Lab loopback probe",
    baseUrl: loopbackBaseUrl(spec.baseUrl),
    envKey: "OPEN_AGENT_LAB_PROBE_KEY",
    defaultModel: "open-agent-lab-probe",
    defaultReasoning: "high",
    reasoning: Object.freeze(["high"]),
    contextWindow: 32_768,
    scope: "loopback-probe",
    requestRetries: 0,
    streamRetries: 0,
  };
  return withDeveloperInstruction(invocation(spec, selected), spec.developerInstruction);
}

function runtimeArgs(invocation: CodexInvocation, modelCatalogPath: string): string[] {
  const args = [...invocation.args];
  if (args.some((value) => /^(?:--config=|-c=?|\s*)model_catalog_json\s*=/u.test(value))) {
    throw new Error("Codex model catalog paths are managed internally.");
  }
  if (args.at(-1) !== "-") {
    throw new Error("Codex invocation no longer reads its prompt from stdin.");
  }
  args.splice(-1, 0, "--config", `model_catalog_json=${JSON.stringify(modelCatalogPath)}`);
  return args;
}

/** Execute Codex without ever copying the provider secret into argv or config. */
export async function runCodexInvocation(
  invocation: CodexInvocation,
  env: NodeJS.ProcessEnv = process.env,
  io: CodexRunIo = defaultIo,
  limits?: CodexRunLimits,
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
    const modelCatalogPath = join(codexHome, "model-catalog.json");
    const catalog = modelCatalog(invocation, await baseInstructions());
    const args = runtimeArgs(invocation, modelCatalogPath);
    await writeFile(modelCatalogPath, catalog, { flag: "wx", mode: 0o600 });
    if (
      limits !== undefined &&
      Object.entries(limits).some(([, value]) => !Number.isSafeInteger(value) || value <= 0)
    ) {
      throw new Error("Codex execution limits must be positive integers.");
    }
    return await new Promise<number>((resolveExit, reject) => {
      const detached = limits !== undefined && process.platform !== "win32";
      const child = spawn(invocation.command, args, {
        cwd: invocation.cwd,
        env: childEnv,
        detached,
        stdio: ["pipe", "pipe", "pipe"],
      });
      let stdoutBytes = 0;
      let stderrBytes = 0;
      let failure: Error | undefined;
      const stop = (error: Error): void => {
        if (failure !== undefined) return;
        failure = error;
        try {
          if (detached && child.pid !== undefined) process.kill(-child.pid, "SIGKILL");
          else child.kill("SIGKILL");
        } catch {
          child.kill("SIGKILL");
        }
      };
      const interrupted = (): void => stop(new Error("Codex execution was interrupted."));
      if (limits !== undefined) {
        process.once("SIGINT", interrupted);
        process.once("SIGTERM", interrupted);
      }
      const removeSignalHandlers = (): void => {
        process.off("SIGINT", interrupted);
        process.off("SIGTERM", interrupted);
      };
      const timer =
        limits === undefined
          ? undefined
          : setTimeout(
              () => stop(new Error(`Codex exceeded its ${limits.timeoutMs}ms execution limit.`)),
              limits.timeoutMs,
            );
      child.once("error", (error) => stop(error));
      child.stdin.once("error", (error) => stop(error));
      child.stdout.on("data", (chunk: Buffer) => {
        stdoutBytes += chunk.length;
        if (limits !== undefined && stdoutBytes > limits.maxStdoutBytes) {
          stop(new Error("Codex exceeded its stdout limit."));
        } else {
          io.stdout(chunk.toString("utf8"));
        }
      });
      child.stderr.on("data", (chunk: Buffer) => {
        stderrBytes += chunk.length;
        if (limits !== undefined && stderrBytes > limits.maxStderrBytes) {
          stop(new Error("Codex exceeded its stderr limit."));
        } else {
          io.stderr(chunk.toString("utf8"));
        }
      });
      child.once("close", (code, signal) => {
        if (timer !== undefined) clearTimeout(timer);
        removeSignalHandlers();
        if (failure !== undefined) {
          reject(failure);
          return;
        }
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
    contextWindow: invocation.contextWindow,
    requiredEnv: invocation.requiredEnv,
    apiScope: invocation.apiScope,
    promptBytes: Buffer.byteLength(invocation.stdin),
  };
}
