import { randomBytes, randomUUID } from "node:crypto";
import { constants } from "node:fs";
import { access, mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";

import { startNativeResponsesRelay, type RelaySealSummary } from "./responses-relay.js";

type RelayProvider = "deepseek" | "zai";

const PROFILES = {
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
    return { value: inline };
  }
  const file = path as string;
  return { value: (await readFile(file, "utf8")).trim(), path: file };
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

async function shutdownSignal(): Promise<void> {
  await new Promise<void>((resolveSignal) => {
    const done = (): void => {
      process.off("SIGINT", done);
      process.off("SIGTERM", done);
      resolveSignal();
    };
    process.once("SIGINT", done);
    process.once("SIGTERM", done);
  });
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
): Promise<RelaySealSummary> {
  const provider = option(args, "--provider") as RelayProvider | undefined;
  if (provider === undefined || !Object.hasOwn(PROFILES, provider)) {
    throw new Error("--provider must be one of: deepseek, zai.");
  }
  const profile = PROFILES[provider];
  const model = option(args, "--model");
  if (model === undefined || !profile.models.some((candidate) => candidate === model)) {
    throw new Error(`--model must be one of: ${profile.models.join(", ")}.`);
  }
  const sidecarPath = option(args, "--output");
  if (sidecarPath === undefined) throw new Error("--output is required.");
  const resolvedSidecar = resolve(sidecarPath);
  const clientTokenPath = resolve(
    option(args, "--client-token-output") ?? `${resolvedSidecar}.client-token`,
  );
  const providerKey = await providerSecret(env, profile.envKey);
  const buildIdPath = option(args, "--build-id-file");
  const buildId =
    buildIdPath === undefined
      ? (env.OAL_RELAY_BUILD_ID ?? "development")
      : (await readFile(buildIdPath, "utf8")).trim();
  dropPrivileges(env);
  await assertSecretFileUnreadable(providerKey.path);
  const clientBearer = randomBytes(32).toString("hex");
  await mkdir(dirname(clientTokenPath), { recursive: true });
  await writeFile(clientTokenPath, `${clientBearer}\n`, { flag: "wx", mode: 0o600 });
  const ttlSeconds = integer(args, "--ttl-seconds", 14_400);
  const expiresAtMs = Date.now() + ttlSeconds * 1_000;
  const relay = await startNativeResponsesRelay({
    runId: option(args, "--run-id") ?? `relay-${randomUUID()}`,
    providerId: provider,
    buildId,
    expectedModel: model,
    upstreamResponsesUrl: profile.endpoint,
    upstreamBearer: providerKey.value,
    clientBearer,
    sidecarPath: resolvedSidecar,
    expiresAtMs,
    listenHost: option(args, "--listen") ?? "127.0.0.1",
    port: integer(args, "--port", 8080),
    maxRequests: integer(args, "--max-requests", 256),
  });
  output(
    JSON.stringify({
      ok: true,
      baseUrl: relay.baseUrl,
      sidecarPath: relay.sidecarPath,
      sealPath: relay.sealPath,
      clientTokenPath,
      provider,
      model,
      expiresAt: new Date(expiresAtMs).toISOString(),
    }),
  );
  const seal = (): void => {
    void relay.seal().catch((error: unknown) => {
      process.stderr.write(
        `Failed to seal relay: ${error instanceof Error ? error.message : String(error)}\n`,
      );
    });
  };
  process.on("SIGUSR2", seal);
  try {
    await shutdownSignal();
    return await relay.close();
  } finally {
    process.off("SIGUSR2", seal);
  }
}
