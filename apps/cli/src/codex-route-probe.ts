import { execFile } from "node:child_process";
import { createHash, randomBytes, randomUUID } from "node:crypto";
import { createReadStream } from "node:fs";
import {
  lstat,
  mkdir,
  mkdtemp,
  readFile,
  readdir,
  realpath,
  rm,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { isAbsolute, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import { canonicalJson } from "@open-agent-lab/contracts";
import {
  sha256,
  verifyEvidenceBundle,
  writeEvidenceBundle,
} from "@open-agent-lab/evidence";

import {
  buildCodexRelayInvocation,
  CODEX_RELAY_ENV_KEY,
  nativeProviderProfile,
  runCodexInvocation,
  type OpenModelProvider,
  type ReasoningEffort,
} from "./codex-runner.js";
import {
  startNativeResponsesRelay,
  verifyRelaySeal,
  type NativeResponsesRelay,
  type RelaySealSummary,
} from "./responses-relay.js";

const executeFile = promisify(execFile);
const REPOSITORY_ROOT = resolve(fileURLToPath(new URL("../../..", import.meta.url)));
const EXPERIMENT_MANIFEST = join(
  REPOSITORY_ROOT,
  "benchmarks/terminal_bench/verify-instruction-v1.experiment.json",
);
const EFFECT_NAME = "route-probe-effect.txt";
const EFFECT = "open-agent-lab-route-probe-v1\n";
const COMMAND = `printf 'open-agent-lab-route-probe-v1\\n' > ${EFFECT_NAME}`;
const DISPLAY_COMMANDS = new Set(
  ["/bin/bash", "/usr/bin/bash"].map((shell) => `${shell} -lc ${JSON.stringify(COMMAND)}`),
);
const PROMPT = [
  "Use the shell tool exactly once with this exact command, without changing or wrapping it:",
  COMMAND,
  "Do nothing else. Then report completion.",
].join(" ");
const JOURNAL = "provider-metadata.ndjson";
const SEAL = `${JOURNAL}.sealed`;
const EVENTS = "codex-events.json";

export interface RouteProbeContract {
  readonly model: string;
  readonly reasoning: ReasoningEffort;
  readonly contextWindow: number;
  readonly codexVersion: string;
  readonly codexBytes: number;
  readonly codexSha256: string;
  readonly relayBuildId: string;
}

export interface SyntheticRouteProbeSpec {
  provider: OpenModelProvider;
  providerKey: string;
  outputDirectory: string;
  codexPath: string;
  sourceRevision: string;
  upstreamResponsesUrl: string;
  createdAt?: string;
}

interface SyntheticRouteProbeExecutionSpec extends SyntheticRouteProbeSpec {
  contract: RouteProbeContract;
}

export interface RouteProbeResult {
  ok: true;
  liveProviderConformance: false;
  benchmarkStartAuthorized: false;
  provider: OpenModelProvider;
  model: string;
  manifestId: string;
  outputDirectory: string;
}

function assertSyntheticFixtureUrl(value: string): void {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new Error("Synthetic route probes require a valid loopback fixture URL.");
  }
  const port = Number(url.port);
  if (
    url.protocol !== "http:" ||
    (url.hostname !== "127.0.0.1" && url.hostname !== "[::1]") ||
    url.pathname !== "/responses" ||
    url.username !== "" ||
    url.password !== "" ||
    url.search !== "" ||
    url.hash !== "" ||
    url.port === "" ||
    !Number.isSafeInteger(port) ||
    port < 1 ||
    port > 65_535
  ) {
    throw new Error("Synthetic route probes require an exact HTTP loopback fixture URL.");
  }
}

function object(value: unknown, at: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error(`${at} must be an object.`);
  }
  return value as Record<string, unknown>;
}

function text(value: unknown, at: string): string {
  if (typeof value !== "string" || value.length === 0) {
    throw new Error(`${at} must be a non-empty string.`);
  }
  return value;
}

function integer(value: unknown, at: string): number {
  if (!Number.isSafeInteger(value) || (value as number) < 0) {
    throw new Error(`${at} must be a non-negative integer.`);
  }
  return value as number;
}

export async function loadRouteProbeContract(
  provider: OpenModelProvider,
): Promise<RouteProbeContract> {
  const manifest = object(JSON.parse(await readFile(EXPERIMENT_MANIFEST, "utf8")), "manifest");
  const runtime = object(manifest.runtime, "manifest.runtime");
  const codex = object(runtime.codexRuntime, "manifest.runtime.codexRuntime");
  const files = Array.isArray(codex.files) ? codex.files.map((item) => object(item, "codex file")) : [];
  const executable = files.find((item) => item.path === "vendor/x86_64-unknown-linux-musl/bin/codex");
  const configs = Array.isArray(manifest.pairedConfigs)
    ? manifest.pairedConfigs.map((item) => object(item, "paired config"))
    : [];
  const selected = configs.find((item) => item.provider === provider);
  const builds = object(manifest.relayBuildIds, "manifest.relayBuildIds");
  const profile = nativeProviderProfile(provider);
  if (executable === undefined || selected === undefined) {
    throw new Error(`The frozen ${provider} route-probe contract is missing.`);
  }
  const reasoning = text(selected.reasoningEffort, "reasoningEffort") as ReasoningEffort;
  const contract = {
    model: text(selected.model, "model"),
    reasoning,
    contextWindow: profile.contextWindow,
    codexVersion: text(codex.version, "codex version"),
    codexBytes: integer(executable.bytes, "codex bytes"),
    codexSha256: text(executable.sha256, "codex sha256"),
    relayBuildId: text(builds.production, "production relay build"),
  };
  if (
    contract.model !== profile.defaultModel ||
    contract.reasoning !== profile.defaultReasoning ||
    contract.contextWindow !== profile.contextWindow ||
    !/^sha256:[a-f0-9]{64}$/u.test(contract.codexSha256) ||
    !/^sha256:[a-f0-9]{64}$/u.test(contract.relayBuildId)
  ) {
    throw new Error(`The frozen ${provider} route-probe contract drifted.`);
  }
  return Object.freeze(contract);
}

async function fileSha256(path: string): Promise<string> {
  const digest = createHash("sha256");
  for await (const chunk of createReadStream(path)) digest.update(chunk as Buffer);
  return `sha256:${digest.digest("hex")}`;
}

async function verifyCodex(path: string, contract: RouteProbeContract): Promise<void> {
  if (process.platform !== "linux" || process.arch !== "x64" || !isAbsolute(path)) {
    throw new Error("Route probes require the pinned linux/x64 Codex executable by absolute path.");
  }
  const [actual, info] = await Promise.all([realpath(path), lstat(path)]);
  if (
    actual !== path ||
    !info.isFile() ||
    info.nlink !== 1 ||
    info.size !== contract.codexBytes ||
    (info.mode & 0o111) === 0 ||
    (await fileSha256(path)) !== contract.codexSha256
  ) {
    throw new Error("The Codex executable does not match the frozen runtime bytes.");
  }
  const version = (await executeFile(path, ["--version"], { encoding: "utf8" })).stdout.trim();
  if (version !== `codex-cli ${contract.codexVersion}`) {
    throw new Error("The Codex executable version does not match the frozen runtime.");
  }
}

interface CodexEventSummary {
  lines: number;
  threadStarted: number;
  turnStarted: number;
  turnCompleted: number;
  commandExecutions: number;
}

function projectCodexEvents(stdout: string): string {
  const events = stdout
    .split("\n")
    .filter((line) => line !== "")
    .map((line) => object(JSON.parse(line), "Codex event"));
  const items = events.flatMap((event) => {
    const eventType = text(event.type, "Codex event type");
    if (
      typeof event.item !== "object" ||
      event.item === null ||
      Array.isArray(event.item)
    ) {
      if (eventType.startsWith("item.")) throw new Error("Codex item event is malformed.");
      return [];
    }
    const item = event.item as Record<string, unknown>;
    if (typeof item.id !== "string" || typeof item.type !== "string") {
      throw new Error("Codex item event is malformed.");
    }
    return [
      {
        event: eventType,
        id: item.id,
        type: item.type,
        ...(item.type === "command_execution"
          ? {
              command: typeof item.command === "string" ? item.command : null,
              status: typeof item.status === "string" ? item.status : null,
              exitCode: Number.isSafeInteger(item.exit_code) ? item.exit_code : null,
            }
          : {}),
      },
    ];
  });
  return `${canonicalJson({
    schemaVersion: 1,
    eventTypes: events.map((event) => text(event.type, "Codex event type")),
    items,
  })}\n`;
}

function validateCodexEvents(content: string): CodexEventSummary {
  const projected = object(JSON.parse(content), "Codex event projection");
  if (`${canonicalJson(projected)}\n` !== content || projected.schemaVersion !== 1) {
    throw new Error("Codex event projection is not canonical.");
  }
  if (!Array.isArray(projected.eventTypes) || !Array.isArray(projected.items)) {
    throw new Error("Codex event projection is incomplete.");
  }
  const eventTypes = projected.eventTypes.map((value) => text(value, "Codex event type"));
  const items = projected.items.map((value) => object(value, "Codex item"));
  const allowedEvents = new Set([
    "error",
    "item.completed",
    "item.started",
    "thread.started",
    "turn.completed",
    "turn.failed",
    "turn.started",
  ]);
  const allowedItems = new Set(["agent_message", "command_execution", "reasoning"]);
  if (eventTypes.some((event) => !allowedEvents.has(event))) {
    throw new Error("Codex event projection contains an unknown event.");
  }
  for (const item of items) {
    const expected =
      item.type === "command_execution"
        ? ["command", "event", "exitCode", "id", "status", "type"]
        : ["event", "id", "type"];
    if (
      Object.keys(item).sort().join(",") !== expected.join(",") ||
      !allowedItems.has(text(item.type, "Codex item type")) ||
      !["item.started", "item.completed"].includes(text(item.event, "Codex item event"))
    ) {
      throw new Error(
        `Codex event projection contains an invalid item (${sha256(String(item.event))}/${sha256(String(item.type))}).`,
      );
    }
    text(item.id, "Codex item id");
  }
  const count = (type: string): number => eventTypes.filter((value) => value === type).length;
  const commands = (type: string): Record<string, unknown>[] =>
    items.filter((item) => item.event === type && item.type === "command_execution");
  const started = commands("item.started");
  const completed = commands("item.completed");
  const itemPositions = eventTypes.flatMap((event, index) =>
    event === "item.started" || event === "item.completed" ? [index] : [],
  );
  const threadPosition = eventTypes.indexOf("thread.started");
  const turnStartPosition = eventTypes.indexOf("turn.started");
  const turnCompletePosition = eventTypes.indexOf("turn.completed");
  const startedPosition = itemPositions[items.findIndex((item) => item === started[0])];
  const completedPosition = itemPositions[items.findIndex((item) => item === completed[0])];
  const ordered =
    startedPosition !== undefined &&
    completedPosition !== undefined &&
    threadPosition < turnStartPosition &&
    turnStartPosition < startedPosition &&
    startedPosition < completedPosition &&
    completedPosition < turnCompletePosition &&
    itemPositions.every(
      (position) => turnStartPosition < position && position < turnCompletePosition,
    );
  if (
    count("thread.started") !== 1 ||
    count("turn.started") !== 1 ||
    count("turn.completed") !== 1 ||
    count("turn.failed") > 0 ||
    count("error") > 0 ||
    started.length !== 1 ||
    completed.length !== 1 ||
    typeof started[0]?.id !== "string" ||
    started[0].id.length === 0 ||
    started[0]?.id !== completed[0]?.id ||
    started[0]?.command !== completed[0]?.command ||
    !DISPLAY_COMMANDS.has(String(started[0]?.command)) ||
    started[0]?.status !== "in_progress" ||
    started[0]?.exitCode !== null ||
    completed[0]?.status !== "completed" ||
    completed[0]?.exitCode !== 0 ||
    itemPositions.length !== items.length ||
    items.some((item, index) => item.event !== eventTypes[itemPositions[index] as number]) ||
    !ordered
  ) {
    throw new Error("Codex did not complete the exact single-command route-probe turn.");
  }
  return {
    lines: eventTypes.length,
    threadStarted: 1,
    turnStarted: 1,
    turnCompleted: 1,
    commandExecutions: 1,
  };
}

export function safeCodexEventProjection(stdout: string): {
  content: string;
  summary: CodexEventSummary;
} {
  const content = projectCodexEvents(stdout);
  return { content, summary: validateCodexEvents(content) };
}

function usage(value: unknown): Record<string, number> {
  const observed = object(value, "provider usage");
  const input = integer(observed.input_tokens, "usage.input_tokens");
  const output = integer(observed.output_tokens, "usage.output_tokens");
  const total = integer(observed.total_tokens, "usage.total_tokens");
  if (total !== input + output) throw new Error("Provider usage arithmetic is invalid.");
  return { input_tokens: input, output_tokens: output, total_tokens: total };
}

function validateRelayEvidence(
  journal: string,
  sealText: string,
  expected: { runId: string; providerId: string; model: string; buildId: string },
): RelaySealSummary {
  const seal = verifyRelaySeal(journal, sealText);
  const records = journal
    .trimEnd()
    .split("\n")
    .filter(Boolean)
    .map((line) => object(JSON.parse(line), "relay record"));
  const requestIds = new Set<string>();
  const responseIds = new Set<string>();
  if (
    records.length !== 6 ||
    seal.eventCount !== 6 ||
    seal.runId !== expected.runId ||
    seal.providerId !== expected.providerId ||
    seal.expectedModel !== expected.model ||
    seal.buildId !== expected.buildId ||
    Object.values(seal.rejectedRequests).some((count) => count !== 0)
  ) {
    throw new Error("The relay did not seal exactly two clean provider responses.");
  }
  for (let offset = 0; offset < records.length; offset += 3) {
    const request = records[offset] as Record<string, unknown>;
    const headers = records[offset + 1] as Record<string, unknown>;
    const closed = records[offset + 2] as Record<string, unknown>;
    const providerRequestId = text(headers.providerRequestId, "provider request id");
    const responseId = text(closed.responseId, "provider response id");
    if (
      request.requestedModel !== expected.model ||
      request.stream !== true ||
      integer(headers.status, "provider status") < 200 ||
      integer(headers.status, "provider status") >= 300 ||
      closed.transportState !== "completed" ||
      closed.errorCategory !== null ||
      closed.providerRequestId !== providerRequestId ||
      closed.returnedModel !== expected.model ||
      closed.modelConsistency !== "consistent" ||
      closed.terminalEvent !== "response.completed" ||
      closed.parseErrors !== 0 ||
      !Array.isArray(closed.metadataConflicts) ||
      closed.metadataConflicts.length !== 0 ||
      requestIds.has(providerRequestId) ||
      responseIds.has(responseId)
    ) {
      throw new Error("Provider response identity or completion metadata is invalid.");
    }
    usage(closed.usage);
    requestIds.add(providerRequestId);
    responseIds.add(responseId);
  }
  return seal;
}

async function runCodexRouteProbe(
  spec: SyntheticRouteProbeExecutionSpec,
): Promise<RouteProbeResult> {
  const profile = nativeProviderProfile(spec.provider);
  const runId = `route-probe-${randomUUID()}`;
  const createdAt = spec.createdAt ?? new Date().toISOString();
  const temporary = await mkdtemp(join(tmpdir(), "open-agent-lab-route-probe-"));
  const workspace = join(temporary, "workspace");
  const sidecarPath = join(temporary, JOURNAL);
  await mkdir(workspace);
  const clientBearer = randomBytes(32).toString("hex");
  let relay: NativeResponsesRelay | undefined;
  try {
    relay = await startNativeResponsesRelay({
      runId,
      providerId: "synthetic-fixture",
      buildId: spec.contract.relayBuildId,
      expectedModel: spec.contract.model,
      upstreamResponsesUrl: spec.upstreamResponsesUrl,
      upstreamBearer: spec.providerKey,
      clientBearer,
      sidecarPath,
      expiresAtMs: Date.now() + 10 * 60_000,
      maxRequests: 2,
      maxRequestBytes: 8 * 1024 * 1024,
      maxResponseBytes: 8 * 1024 * 1024,
    });
    const invocation = buildCodexRelayInvocation({
      provider: spec.provider,
      workspace,
      prompt: PROMPT,
      model: spec.contract.model,
      reasoning: spec.contract.reasoning,
      codexPath: spec.codexPath,
      baseUrl: relay.baseUrl,
    });
    let stdout = "";
    const code = await runCodexInvocation(
      invocation,
      { ...process.env, [CODEX_RELAY_ENV_KEY]: clientBearer },
      { stdout: (chunk) => (stdout += chunk), stderr: () => undefined },
      { timeoutMs: 8 * 60_000, maxStdoutBytes: 2 * 1024 * 1024, maxStderrBytes: 1024 * 1024 },
    );
    if (code !== 0) throw new Error(`Codex route probe exited ${code}.`);
    const projected = safeCodexEventProjection(stdout);
    const eventProjection = projected.content;
    const events = projected.summary;
    const effectPath = join(workspace, EFFECT_NAME);
    const [entries, effect, effectInfo] = await Promise.all([
      readdir(workspace, { withFileTypes: true }),
      readFile(effectPath),
      lstat(effectPath),
    ]);
    if (
      entries.length !== 1 ||
      entries[0]?.name !== EFFECT_NAME ||
      !entries[0].isFile() ||
      !effectInfo.isFile() ||
      effectInfo.nlink !== 1 ||
      effectInfo.size !== Buffer.byteLength(EFFECT) ||
      !effect.equals(Buffer.from(EFFECT))
    ) {
      throw new Error("Codex route-probe workspace or effect is invalid.");
    }

    const closed = await relay.close();
    relay = undefined;
    const [journal, sealText] = await Promise.all([
      readFile(sidecarPath, "utf8"),
      readFile(`${sidecarPath}.sealed`, "utf8"),
    ]);
    const seal = validateRelayEvidence(journal, sealText, {
      runId,
      providerId: "synthetic-fixture",
      model: spec.contract.model,
      buildId: spec.contract.relayBuildId,
    });
    if (closed.markerSha256 !== seal.markerSha256) {
      throw new Error("Relay close and retained seal disagree.");
    }
    for (const secret of [spec.providerKey, clientBearer]) {
      if (journal.includes(secret) || sealText.includes(secret) || eventProjection.includes(secret)) {
        throw new Error("Route-probe evidence contains a credential.");
      }
    }
    const manifest = await writeEvidenceBundle(spec.outputDirectory, {
      runId,
      createdAt,
      metadata: {
        routeProbeSchemaVersion: 1,
        proofClass: "synthetic-provider-route",
        liveProviderConformance: false,
        benchmarkStartAuthorized: false,
        provider: spec.provider,
        providerRoute: profile.responsesUrl,
        model: spec.contract.model,
        reasoning: spec.contract.reasoning,
        contextWindow: spec.contract.contextWindow,
        sourceRevision: spec.sourceRevision,
        codexVersion: spec.contract.codexVersion,
        codexSha256: spec.contract.codexSha256,
        relayBuildId: spec.contract.relayBuildId,
        requestCount: seal.eventCount / 3,
        chainHead: seal.chainHead,
        sealMarkerSha256: seal.markerSha256,
        codexEvents: { ...events },
        commandSha256: sha256(COMMAND),
        toolEffectSha256: sha256(effect),
        spendCapConfirmed: false,
      },
      files: [
        { path: JOURNAL, content: journal, mediaType: "application/x-ndjson", role: "relay-journal" },
        { path: SEAL, content: sealText, mediaType: "application/json", role: "relay-seal" },
        { path: EVENTS, content: eventProjection, mediaType: "application/json", role: "codex-event-projection" },
        { path: EFFECT_NAME, content: effect, mediaType: "text/plain", role: "tool-effect" },
      ],
    });
    return {
      ok: true,
      liveProviderConformance: false,
      benchmarkStartAuthorized: false,
      provider: spec.provider,
      model: spec.contract.model,
      manifestId: manifest.manifestId,
      outputDirectory: resolve(spec.outputDirectory),
    };
  } finally {
    if (relay !== undefined) await relay.close().catch(() => undefined);
    await rm(temporary, { force: true, recursive: true });
  }
}

/** Exercise the exact route with a local fixture; this can never prove live conformance. */
export async function runSyntheticCodexRouteProbe(
  spec: SyntheticRouteProbeSpec,
): Promise<RouteProbeResult> {
  assertSyntheticFixtureUrl(spec.upstreamResponsesUrl);
  if (!/^[a-f0-9]{40}$/u.test(spec.sourceRevision)) {
    throw new Error("Synthetic route probes require an exact source revision.");
  }
  const contract = await loadRouteProbeContract(spec.provider);
  await verifyCodex(spec.codexPath, contract);
  return runCodexRouteProbe({ ...spec, contract });
}

export async function verifyCodexRouteProbeBundle(directory: string): Promise<RouteProbeResult> {
  const verified = await verifyEvidenceBundle(directory);
  const metadata = object(verified.manifest.metadata, "route-probe metadata");
  const provider = text(metadata.provider, "provider") as OpenModelProvider;
  if (provider !== "deepseek" && provider !== "zai") throw new Error("Unknown route-probe provider.");
  const contract = await loadRouteProbeContract(provider);
  if (metadata.proofClass !== "synthetic-provider-route") {
    throw new Error("Only provider-free route-probe evidence is accepted here.");
  }
  const [journal, sealText, eventProjection, effect] = await Promise.all([
    readFile(join(directory, JOURNAL), "utf8"),
    readFile(join(directory, SEAL), "utf8"),
    readFile(join(directory, EVENTS), "utf8"),
    readFile(join(directory, EFFECT_NAME)),
  ]);
  const seal = validateRelayEvidence(journal, sealText, {
    runId: verified.manifest.runId,
    providerId: "synthetic-fixture",
    model: contract.model,
    buildId: contract.relayBuildId,
  });
  const liveProviderConformance = false;
  const events = validateCodexEvents(eventProjection);
  const recordedEvents = object(metadata.codexEvents, "codexEvents");
  const files = verified.manifest.files.map((file) => file.path);
  if (
    files.join(",") !== [JOURNAL, SEAL, EVENTS, EFFECT_NAME].sort().join(",") ||
    metadata.routeProbeSchemaVersion !== 1 ||
    metadata.liveProviderConformance !== liveProviderConformance ||
    metadata.benchmarkStartAuthorized !== false ||
    metadata.providerRoute !== nativeProviderProfile(provider).responsesUrl ||
    metadata.model !== contract.model ||
    metadata.reasoning !== contract.reasoning ||
    metadata.contextWindow !== contract.contextWindow ||
    metadata.codexVersion !== contract.codexVersion ||
    metadata.codexSha256 !== contract.codexSha256 ||
    metadata.relayBuildId !== contract.relayBuildId ||
    metadata.requestCount !== 2 ||
    metadata.chainHead !== seal.chainHead ||
    metadata.sealMarkerSha256 !== seal.markerSha256 ||
    metadata.toolEffectSha256 !== sha256(effect) ||
    metadata.commandSha256 !== sha256(COMMAND) ||
    !effect.equals(Buffer.from(EFFECT)) ||
    metadata.spendCapConfirmed !== liveProviderConformance ||
    recordedEvents.lines !== events.lines ||
    recordedEvents.threadStarted !== events.threadStarted ||
    recordedEvents.turnStarted !== events.turnStarted ||
    recordedEvents.turnCompleted !== events.turnCompleted ||
    recordedEvents.commandExecutions !== events.commandExecutions ||
    !/^[a-f0-9]{40}$/u.test(text(metadata.sourceRevision, "sourceRevision"))
  ) {
    throw new Error("Route-probe receipt does not match its retained evidence.");
  }
  return {
    ok: true,
    liveProviderConformance,
    benchmarkStartAuthorized: false,
    provider,
    model: contract.model,
    manifestId: verified.manifest.manifestId,
    outputDirectory: resolve(directory),
  };
}
