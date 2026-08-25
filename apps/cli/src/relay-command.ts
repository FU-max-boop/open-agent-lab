import { randomBytes, randomUUID } from "node:crypto";
import { constants } from "node:fs";
import { access, link, mkdir, open, readFile, rm } from "node:fs/promises";
import { dirname, resolve } from "node:path";

import {
  outputBudgetPolicy,
  type OutputBudgetClass,
} from "./responses-output-budget.js";
import { startNativeResponsesRelay, type RelaySealSummary } from "./responses-relay.js";

export interface RelayProfile {
  readonly envKey: string;
  readonly endpoint: string;
  readonly models: readonly string[];
  readonly evidenceProviderId?: string;
}

export type RelayProfiles = Readonly<Record<string, RelayProfile>>;

export interface RelayIdentity {
  readonly provider: string;
  readonly model: string;
  readonly budgetClass: OutputBudgetClass;
}

export interface RelayAuthorization extends RelayIdentity {
  readonly buildId: string;
  readonly readyPath: string;
  readonly capabilityId: string;
}

const PROFILES: RelayProfiles = {
  deepseek: {
    envKey: "DEEPSEEK_API_KEY",
    endpoint: "https://api.deepseek.com/responses",
    models: ["deepseek-v4-flash", "deepseek-v4-pro"],
  },
  zai: {
    envKey: "ZAI_API_KEY",
    endpoint: "https://api.z.ai/api/v1/responses",
    models: ["glm-5.3"],
  },
} as const;

export async function publishFileAtomic(
  path: string,
  content: string,
  mode = 0o600,
): Promise<void> {
  await mkdir(dirname(path), { recursive: true });
  const temporary = `${path}.${randomUUID()}.tmp`;
  let created = false;
  try {
    const handle = await open(temporary, "wx", mode);
    created = true;
    try {
      await handle.writeFile(content, "utf8");
      await handle.sync();
    } finally {
      await handle.close();
    }
    await link(temporary, path);
  } finally {
    if (created) await rm(temporary, { force: true }).catch(() => undefined);
  }
}

function option(args: readonly string[], name: string): string | undefined {
  const index = args.indexOf(name);
  const value = index === -1 ? undefined : args[index + 1];
  if (index !== -1 && (value === undefined || value.startsWith("--"))) {
    throw new Error(`${name} requires a value.`);
  }
  return value;
}

function integer(args: readonly string[], name: string, fallback: number): number {
  const raw = option(args, name);
  if (raw === undefined) return fallback;
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value < 0) throw new Error(`${name} must be an integer.`);
  return value;
}

export async function readVerifiedBuildId(
  args: readonly string[],
  env: NodeJS.ProcessEnv,
): Promise<string> {
  const path = option(args, "--build-id-file");
  if (path === undefined) throw new Error("--build-id-file is required.");
  const buildId = (await readFile(path, "utf8")).trim();
  const expected = env.OAL_EXPECTED_RELAY_BUILD_ID;
  if (expected === undefined || !/^sha256:[a-f0-9]{64}$/u.test(expected) || buildId !== expected) {
    throw new Error("Relay build identity does not match the expected preflight.");
  }
  return buildId;
}

async function authorizationSignal(ready: () => Promise<void>): Promise<void> {
  const keepAlive = setInterval(() => undefined, 60_000);
  let authorize = (): void => undefined;
  let abort = (): void => undefined;
  const cleanup = (): void => {
    process.off("SIGUSR1", authorize);
    process.off("SIGINT", abort);
    process.off("SIGTERM", abort);
    clearInterval(keepAlive);
  };
  const signal = new Promise<void>((resolveSignal, rejectSignal) => {
    authorize = (): void => {
      cleanup();
      resolveSignal();
    };
    abort = (): void => {
      cleanup();
      rejectSignal(new Error("Relay authorization was interrupted."));
    };
    process.once("SIGUSR1", authorize);
    process.once("SIGINT", abort);
    process.once("SIGTERM", abort);
  });
  try {
    await ready();
    await signal;
  } catch (error) {
    cleanup();
    throw error;
  }
}

export async function awaitRelayAuthorization(
  args: readonly string[],
  env: NodeJS.ProcessEnv,
  expected: RelayIdentity,
): Promise<RelayAuthorization> {
  const provider = option(args, "--provider");
  const model = option(args, "--model");
  const budgetClass = option(args, "--budget-class");
  if (
    provider !== expected.provider ||
    model !== expected.model ||
    budgetClass !== expected.budgetClass
  ) {
    throw new Error("Relay identity does not match the expected provider, model, and budget class.");
  }
  const sidecarPath = option(args, "--output");
  if (sidecarPath === undefined) throw new Error("--output is required.");
  const buildId = await readVerifiedBuildId(args, env);
  const readyPath = resolve(`${sidecarPath}.bootstrap-ready`);
  const capabilityId = randomBytes(32).toString("hex");
  await authorizationSignal(async () =>
    publishFileAtomic(
      readyPath,
      `${JSON.stringify({
        schemaVersion: 2,
        buildId,
        provider,
        model,
        budgetClass,
        capabilityId,
      })}\n`,
      0o444,
    ),
  );
  return { buildId, readyPath, provider, model, budgetClass, capabilityId };
}

function normalizeProviderSecret(raw: string, name: string): string {
  const value = raw.replace(/^[\t-\r ]+|[\t-\r ]+$/gu, "");
  if (value.length < 32 || !/^[\x21-\x7e]+$/u.test(value)) {
    throw new Error(`${name} is invalid.`);
  }
  return value;
}

async function providerSecret(
  env: NodeJS.ProcessEnv,
  name: string,
): Promise<{ value: string; path?: string }> {
  const inline = env[name];
  const path = env[`${name}_FILE`];
  if ((inline === undefined) === (path === undefined)) {
    throw new Error(`Set exactly one of ${name} or ${name}_FILE.`);
  }
  if (inline !== undefined) {
    delete env[name];
    return { value: normalizeProviderSecret(inline, name) };
  }
  const file = path as string;
  return { value: normalizeProviderSecret(await readFile(file, "utf8"), name), path: file };
}

async function assertSecretFileUnreadable(path: string | undefined): Promise<void> {
  if (path === undefined) return;
  try {
    await access(path, constants.R_OK);
  } catch {
    return;
  }
  throw new Error("Provider key file remains readable after dropping relay privileges.");
}

function shutdownSignal(): { readonly wait: Promise<void>; readonly cancel: () => void } {
  const keepAlive = setInterval(() => undefined, 60_000);
  let done = (): void => undefined;
  const wait = new Promise<void>((resolveSignal) => {
    done = (): void => {
      process.off("SIGINT", done);
      process.off("SIGTERM", done);
      clearInterval(keepAlive);
      resolveSignal();
    };
    process.once("SIGINT", done);
    process.once("SIGTERM", done);
  });
  return {
    wait,
    cancel: (): void => {
      process.off("SIGINT", done);
      process.off("SIGTERM", done);
      clearInterval(keepAlive);
    },
  };
}

function dropPrivileges(env: NodeJS.ProcessEnv): void {
  const uid = env.OAL_RELAY_UID;
  const gid = env.OAL_RELAY_GID;
  if (uid === undefined && gid === undefined) return;
  if (uid === undefined || gid === undefined || !/^\d+$/u.test(uid) || !/^\d+$/u.test(gid)) {
    throw new Error("OAL_RELAY_UID and OAL_RELAY_GID must be numeric and set together.");
  }
  const { getgid, getuid, setgid, setgroups, setuid } = process;
  if (
    getgid === undefined ||
    getuid === undefined ||
    setgid === undefined ||
    setgroups === undefined ||
    setuid === undefined
  ) {
    throw new Error("Relay privilege dropping requires a POSIX runtime.");
  }
  setgroups([]);
  setgid(Number(gid));
  setuid(Number(uid));
  if (getuid() !== Number(uid) || getgid() !== Number(gid)) {
    throw new Error("Failed to drop relay privileges.");
  }
}

export async function runRelayCommand(
  args: readonly string[],
  env: NodeJS.ProcessEnv = process.env,
  output: (message: string) => void = (message) => process.stdout.write(`${message}\n`),
  profiles: RelayProfiles = PROFILES,
  authorization?: RelayAuthorization,
): Promise<RelaySealSummary> {
  const provider = option(args, "--provider");
  const profile = provider === undefined ? undefined : profiles[provider];
  if (provider === undefined || profile === undefined) {
    throw new Error(`--provider must be one of: ${Object.keys(profiles).join(", ")}.`);
  }
  const model = option(args, "--model");
  if (model === undefined || !profile.models.some((candidate) => candidate === model)) {
    throw new Error(`--model must be one of: ${profile.models.join(", ")}.`);
  }
  const budgetClass = outputBudgetPolicy(option(args, "--budget-class") ?? "").budgetClass;
  const sidecarPath = option(args, "--output");
  if (sidecarPath === undefined) throw new Error("--output is required.");
  const resolvedSidecar = resolve(sidecarPath);
  const clientTokenPath = resolve(
    option(args, "--client-token-output") ?? `${resolvedSidecar}.client-token`,
  );
  const grant =
    authorization ?? (await awaitRelayAuthorization(args, env, { provider, model, budgetClass }));
  const buildId = await readVerifiedBuildId(args, env);
  if (
    grant.buildId !== buildId ||
    grant.readyPath !== resolve(`${resolvedSidecar}.bootstrap-ready`) ||
    grant.provider !== provider ||
    grant.model !== model ||
    grant.budgetClass !== budgetClass ||
    !/^[a-f0-9]{64}$/u.test(grant.capabilityId)
  ) {
    throw new Error("Relay authorization does not match this process.");
  }
  const providerKey = await providerSecret(env, profile.envKey);
  const evidenceProviderId = profile.evidenceProviderId ?? provider;
  dropPrivileges(env);
  await assertSecretFileUnreadable(providerKey.path);
  const clientBearer = randomBytes(32).toString("hex");
  const ttlSeconds = integer(args, "--ttl-seconds", 14_400);
  const expiresAtMs = Date.now() + ttlSeconds * 1_000;
  const relay = await startNativeResponsesRelay({
    runId: option(args, "--run-id") ?? `relay-${randomUUID()}`,
    providerId: evidenceProviderId,
    buildId,
    expectedModel: model,
    budgetClass,
    upstreamResponsesUrl: profile.endpoint,
    upstreamBearer: providerKey.value,
    clientBearer,
    sidecarPath: resolvedSidecar,
    expiresAtMs,
    listenHost: option(args, "--listen") ?? "127.0.0.1",
    port: integer(args, "--port", 8080),
    maxRequests: integer(args, "--max-requests", 256),
    maxRequestBytes: integer(args, "--max-request-bytes", 64 * 1024 * 1024),
    maxResponseBytes: integer(args, "--max-response-bytes", 64 * 1024 * 1024),
    connectTimeoutMs: integer(args, "--connect-timeout-ms", 30_000),
    idleTimeoutMs: integer(args, "--idle-timeout-ms", 300_000),
  });
  const seal = (): void => {
    void relay.seal().catch((error: unknown) => {
      process.stderr.write(
        `Failed to seal relay: ${error instanceof Error ? error.message : String(error)}\n`,
      );
    });
  };
  const shutdown = shutdownSignal();
  let tokenPublished = false;
  process.on("SIGUSR2", seal);
  try {
    await publishFileAtomic(
      clientTokenPath,
      `${JSON.stringify({ schemaVersion: 1, capabilityId: grant.capabilityId, bearer: clientBearer })}\n`,
    );
    tokenPublished = true;
    output(
      JSON.stringify({
        ok: true,
        baseUrl: relay.baseUrl,
        sidecarPath: relay.sidecarPath,
        sealPath: relay.sealPath,
        clientTokenPath,
        provider: evidenceProviderId,
        model,
        budgetClass,
        expiresAt: new Date(expiresAtMs).toISOString(),
      }),
    );
    await shutdown.wait;
    return await relay.close();
  } catch (error) {
    await relay.close().catch(() => undefined);
    if (tokenPublished) {
      await rm(clientTokenPath, { force: true }).catch(() => undefined);
    }
    throw error;
  } finally {
    shutdown.cancel();
    process.off("SIGUSR2", seal);
  }
}
