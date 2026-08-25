import { createHash, randomUUID, timingSafeEqual } from "node:crypto";
import {
  createServer,
  type IncomingMessage,
  type ServerResponse,
} from "node:http";
import type {
  ReadableStreamDefaultReader,
  ReadableStreamReadResult,
} from "node:stream/web";

import { canonicalJson } from "@open-agent-lab/contracts";
import { sha256 } from "@open-agent-lab/evidence";

import {
  SseMetadataObserver,
  type ResponseMetadata,
  type ToolOutputContinuationBinding,
} from "./responses-metadata.js";
import {
  Codex149SecretGuard,
  ResponsesSseParser,
  inspectNonSuccessBody,
  type ParsedSseFrame,
} from "./responses-sse.js";
import {
  RELAY_VERSION,
  RelayJournal,
  writeRelaySeal,
  type RelaySealSummary,
} from "./relay-evidence.js";
import {
  OutputBudgetInputError,
  ResponsesOutputBudgetLedger,
  type OutputBudgetAdmission,
  type OutputBudgetClass,
} from "./responses-output-budget.js";

export { verifyRelayJournal, verifyRelaySeal } from "./relay-evidence.js";
export type { RelaySealSummary } from "./relay-evidence.js";

const RESPONSES_PATH = "/v1/responses";
const TURN_STATE_HEADER = "x-codex-turn-state";
const FORWARDED_HEADERS = [
  "cache-control",
  "content-type",
  "openai-model",
  TURN_STATE_HEADER,
  "x-models-etag",
  "x-reasoning-included",
  "x-request-id",
] as const;

export interface NativeResponsesRelayOptions {
  runId: string;
  providerId: string;
  buildId: string;
  expectedModel: string;
  budgetClass: OutputBudgetClass;
  upstreamResponsesUrl: string;
  upstreamBearer: string;
  clientBearer: string;
  sidecarPath: string;
  expiresAtMs: number;
  listenHost?: string;
  port?: number;
  maxRequests?: number;
  maxRequestBytes?: number;
  maxResponseBytes?: number;
  connectTimeoutMs?: number;
  idleTimeoutMs?: number;
  fetchImpl?: typeof fetch;
  clock?: () => number;
}

export interface NativeResponsesRelay {
  baseUrl: string;
  sidecarPath: string;
  sealPath: string;
  seal: () => Promise<RelaySealSummary>;
  close: () => Promise<RelaySealSummary>;
}

interface RelayLimits {
  maxRequests: number;
  maxRequestBytes: number;
  maxResponseBytes: number;
  connectTimeoutMs: number;
  idleTimeoutMs: number;
}

interface QueuedFlight {
  grant: () => void;
  cancel: (error: RelayHttpError) => void;
}

class RelayHttpError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
  ) {
    super(code);
  }
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function safeString(value: unknown): string | null {
  return typeof value === "string" &&
    value.length > 0 &&
    value.length <= 512 &&
    !/[\u0000-\u001f]/u.test(value)
    ? value
    : null;
}

function codexTurnState(value: unknown): string | null {
  return typeof value === "string" &&
    !value.includes(",") &&
    /^[\x20-\x7e]{1,512}$/u.test(value)
    ? value
    : null;
}

function containsSecret(values: Iterable<string | null>, secret: string): boolean {
  for (const value of values) {
    if (value?.includes(secret) === true) return true;
  }
  return false;
}

function redactedResponseMetadata(parseErrors: number): ResponseMetadata {
  return {
    responseId: null,
    returnedModel: null,
    modelConsistency: "missing",
    modelSources: {},
    systemFingerprint: null,
    terminalEvent: null,
    terminalStatus: null,
    incompleteReason: null,
    usage: null,
    metadataConflicts: [],
    parseErrors,
  };
}

function validateSecret(value: string, name: string): void {
  if (Buffer.byteLength(value) < 32 || !/^[\x21-\x7e]+$/u.test(value)) {
    throw new Error(`${name} is invalid.`);
  }
}

function positiveInteger(value: number, name: string): void {
  if (!Number.isSafeInteger(value) || value <= 0) throw new Error(`${name} must be positive.`);
}

function authorized(header: string | undefined, expectedToken: string): boolean {
  if (header === undefined || !header.startsWith("Bearer ")) return false;
  const actual = Buffer.from(header.slice("Bearer ".length));
  const expected = Buffer.from(expectedToken);
  return actual.length === expected.length && timingSafeEqual(actual, expected);
}

async function requestBody(request: IncomingMessage, limit: number): Promise<Buffer> {
  const chunks: Buffer[] = [];
  let bytes = 0;
  for await (const value of request) {
    const chunk = Buffer.isBuffer(value) ? value : Buffer.from(value);
    bytes += chunk.length;
    if (bytes > limit) throw new RelayHttpError(413, "request_too_large");
    chunks.push(chunk);
  }
  return Buffer.concat(chunks, bytes);
}

function normalizedRequestBody(
  body: Buffer,
  expectedModel: string,
): { request: Record<string, unknown>; requestedMaxOutputTokens: unknown } {
  let parsed: unknown;
  try {
    parsed = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(body)) as unknown;
  } catch {
    throw new RelayHttpError(400, "invalid_json");
  }
  if (!isObject(parsed) || parsed.model !== expectedModel) {
    throw new RelayHttpError(400, "model_mismatch");
  }
  if (parsed.stream !== true || parsed.store !== false) {
    throw new RelayHttpError(400, "unsupported_response_mode");
  }
  try {
    canonicalJson(parsed);
  } catch {
    throw new RelayHttpError(400, "invalid_json");
  }
  return {
    request: parsed,
    requestedMaxOutputTokens:
      Object.hasOwn(parsed, "max_output_tokens") ? parsed.max_output_tokens : null,
  };
}

function isToolOutputContinuation(
  request: Record<string, unknown>,
  expected: ToolOutputContinuationBinding | null,
): boolean {
  if (expected === null || !Array.isArray(request.input) || request.input.length !== 1) {
    return false;
  }
  const item = request.input[0];
  return isObject(item) &&
    item.type === expected.type &&
    item.call_id === expected.callId &&
    typeof item.output === "string";
}

function writeError(response: ServerResponse, error: RelayHttpError): void {
  if (response.headersSent) {
    response.destroy();
    return;
  }
  const body = `${canonicalJson({ error: { code: error.code } })}\n`;
  response.writeHead(error.status, {
    "content-length": Buffer.byteLength(body),
    "content-type": "application/json",
  });
  response.end(body);
}

function isoTime(milliseconds: number): string {
  return new Date(milliseconds).toISOString();
}

async function readWithIdleTimeout(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  timeoutMs: number,
  controller: AbortController,
): Promise<ReadableStreamReadResult<Uint8Array>> {
  let timer: NodeJS.Timeout | undefined;
  let timedOut = false;
  try {
    return await Promise.race([
      reader.read(),
      new Promise<never>((_resolve, reject) => {
        timer = setTimeout(() => {
          timedOut = true;
          reject(new RelayHttpError(504, "upstream_idle_timeout"));
          controller.abort();
        }, timeoutMs);
      }),
    ]);
  } catch (error) {
    if (timedOut) throw new RelayHttpError(504, "upstream_idle_timeout");
    throw error;
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
}

async function writeWithBackpressure(response: ServerResponse, chunk: Uint8Array): Promise<void> {
  if (response.write(chunk)) return;
  await new Promise<void>((resolve, reject) => {
    const cleanup = (): void => {
      response.off("drain", drained);
      response.off("close", closed);
    };
    const drained = (): void => {
      cleanup();
      resolve();
    };
    const closed = (): void => {
      cleanup();
      reject(new RelayHttpError(499, "client_disconnected"));
    };
    response.once("drain", drained);
    response.once("close", closed);
  });
}

async function endResponse(response: ServerResponse): Promise<boolean> {
  if (response.writableFinished) return true;
  if (response.destroyed) return false;
  return new Promise<boolean>((resolve) => {
    const cleanup = (): void => {
      response.off("finish", finished);
      response.off("close", closed);
    };
    const finished = (): void => {
      cleanup();
      resolve(true);
    };
    const closed = (): void => {
      cleanup();
      resolve(response.writableFinished);
    };
    response.once("finish", finished);
    response.once("close", closed);
    if (!response.writableEnded) response.end();
  });
}

function normalizedOptions(options: NativeResponsesRelayOptions): {
  listenHost: string;
  port: number;
  limits: RelayLimits;
  upstream: URL;
} {
  validateSecret(options.upstreamBearer, "upstreamBearer");
  validateSecret(options.clientBearer, "clientBearer");
  if (options.upstreamBearer === options.clientBearer) {
    throw new Error("Provider and relay credentials must differ.");
  }
  if (!/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/u.test(options.expectedModel)) {
    throw new Error("expectedModel is invalid.");
  }
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/u.test(options.runId)) {
    throw new Error("runId is invalid.");
  }
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/u.test(options.providerId)) {
    throw new Error("providerId is invalid.");
  }
  if (!/^(?:sha256:[a-f0-9]{64}|development)$/u.test(options.buildId)) {
    throw new Error("buildId is invalid.");
  }
  const upstream = new URL(options.upstreamResponsesUrl);
  if (
    !["http:", "https:"].includes(upstream.protocol) ||
    upstream.username !== "" ||
    upstream.password !== "" ||
    upstream.search !== "" ||
    upstream.hash !== "" ||
    !upstream.pathname.endsWith("/responses")
  ) {
    throw new Error("upstreamResponsesUrl must be one fixed Responses endpoint.");
  }
  if (
    upstream.protocol === "http:" &&
    !["127.0.0.1", "[::1]", "localhost"].includes(upstream.hostname)
  ) {
    throw new Error("Plain HTTP upstreams are restricted to loopback tests.");
  }
  const limits: RelayLimits = {
    maxRequests: options.maxRequests ?? 256,
    maxRequestBytes: options.maxRequestBytes ?? 64 * 1024 * 1024,
    maxResponseBytes: options.maxResponseBytes ?? 64 * 1024 * 1024,
    connectTimeoutMs: options.connectTimeoutMs ?? 30_000,
    idleTimeoutMs: options.idleTimeoutMs ?? 300_000,
  };
  for (const [name, value] of Object.entries(limits)) positiveInteger(value, name);
  const port = options.port ?? 0;
  if (!Number.isSafeInteger(port) || port < 0 || port > 65_535) {
    throw new Error("port is invalid.");
  }
  return { listenHost: options.listenHost ?? "127.0.0.1", port, limits, upstream };
}

export async function startNativeResponsesRelay(
  options: NativeResponsesRelayOptions,
): Promise<NativeResponsesRelay> {
  const { listenHost, port, limits, upstream } = normalizedOptions(options);
  const clock = options.clock ?? Date.now;
  const now = clock();
  if (
    !Number.isFinite(options.expiresAtMs) ||
    options.expiresAtMs <= now ||
    options.expiresAtMs > now + 24 * 60 * 60 * 1_000
  ) {
    throw new Error("expiresAtMs must be within the next 24 hours.");
  }
  const fetchImpl = options.fetchImpl ?? fetch;
  const journal = await RelayJournal.create(options.sidecarPath);
  const outputBudget = new ResponsesOutputBudgetLedger(options.budgetClass);
  const relayInstanceId = randomUUID();
  const sealPath = `${options.sidecarPath}.sealed`;
  const identity = {
    schemaVersion: 2,
    relayVersion: RELAY_VERSION,
    runId: options.runId,
    relayInstanceId,
    providerId: options.providerId,
    buildId: options.buildId,
  } as const;
  const controllers = new Set<AbortController>();
  const requests = new Set<IncomingMessage>();
  const inFlight = new Set<Promise<void>>();
  const rejectedRequests: Record<string, number> = {};
  let accepted = 0;
  let expectedToolContinuation: ToolOutputContinuationBinding | null = null;
  let active = 0;
  let queuedFlight: QueuedFlight | undefined;
  let closing = false;
  const countRejection = (code: string): void => {
    rejectedRequests[code] = (rejectedRequests[code] ?? 0) + 1;
  };
  const acquireFlight = async (
    request: IncomingMessage,
    response: ServerResponse,
  ): Promise<void> => {
    if (active === 0) {
      active = 1;
      return;
    }
    if (queuedFlight !== undefined) {
      throw new RelayHttpError(429, "concurrency_exceeded");
    }
    await new Promise<void>((resolve, reject) => {
      let waiter!: QueuedFlight;
      const cleanup = (): void => {
        clearTimeout(timer);
        request.off("aborted", disconnected);
        response.off("close", disconnected);
      };
      const cancel = (error: RelayHttpError): void => {
        if (queuedFlight === waiter) queuedFlight = undefined;
        cleanup();
        reject(error);
      };
      const disconnected = (): void => cancel(new RelayHttpError(499, "client_disconnected"));
      const timer = setTimeout(
        () => cancel(new RelayHttpError(401, "expired")),
        Math.max(1, options.expiresAtMs - clock()),
      );
      waiter = {
        grant: () => {
          cleanup();
          resolve();
        },
        cancel,
      };
      request.once("aborted", disconnected);
      response.once("close", disconnected);
      queuedFlight = waiter;
    });
  };
  const releaseFlight = (): void => {
    const successor = queuedFlight;
    queuedFlight = undefined;
    if (successor === undefined) {
      active = 0;
    } else if (closing || clock() >= options.expiresAtMs) {
      active = 0;
      successor.cancel(new RelayHttpError(closing ? 404 : 401, closing ? "relay_sealed" : "expired"));
    } else {
      successor.grant();
    }
  };

  const server = createServer(
    {
      headersTimeout: 10_000,
      keepAliveTimeout: 5_000,
      maxHeaderSize: 16 * 1024,
      requestTimeout: Math.max(10_000, limits.idleTimeoutMs),
    },
    (request, response) => {
      const task = (async () => {
        if (closing || request.url !== RESPONSES_PATH || request.method !== "POST") {
          throw new RelayHttpError(404, "not_found");
        }
        if (!authorized(request.headers.authorization, options.clientBearer)) {
          throw new RelayHttpError(401, "unauthorized");
        }
        if (clock() >= options.expiresAtMs) throw new RelayHttpError(401, "expired");
        const contentType = request.headers["content-type"]?.trim();
        if (!contentType || !/^application\/json(?:\s*;.*)?$/iu.test(contentType)) {
          throw new RelayHttpError(415, "unsupported_content_type");
        }
        const declaredLength = request.headers["content-length"];
        if (
          declaredLength !== undefined &&
          (!/^\d+$/u.test(declaredLength) || Number(declaredLength) > limits.maxRequestBytes)
        ) {
          throw new RelayHttpError(413, "request_too_large");
        }

        await acquireFlight(request, response);
        requests.add(request);
        const startedAt = clock();
        const relayRequestId = randomUUID();
        let ordinal = 0;
        let headersRecorded = false;
        let upstreamStatus: number | null = null;
        let providerRequestId: string | null = null;
        let responseBytes = 0;
        let responseHash = createHash("sha256");
        let firstByteAt: number | null = null;
        let transportState: "completed" | "failed" | "aborted" = "failed";
        let errorCategory: string | null = null;
        let terminalError: RelayHttpError | null = null;
        let observer = new SseMetadataObserver(null);
        const controller = new AbortController();
        let secretEchoConfirmed = false;
        const secretFailure = (): RelayHttpError => {
          if (!secretEchoConfirmed) countRejection("upstream_secret_echo");
          secretEchoConfirmed = true;
          controller.abort();
          if (response.headersSent) response.destroy();
          return new RelayHttpError(502, "upstream_failure");
        };
        controllers.add(controller);
        let clientDisconnected = false;
        let expiredInFlight = false;
        const deadline = setTimeout(() => {
          expiredInFlight = true;
          controller.abort();
          request.destroy(new Error("relay_expired"));
        }, Math.max(1, options.expiresAtMs - clock()));
        const clientClosed = (): void => {
          if (!response.writableEnded) {
            clientDisconnected = true;
            controller.abort();
          }
        };
        response.once("close", clientClosed);

        try {
          const normalized = normalizedRequestBody(
            await requestBody(request, limits.maxRequestBytes),
            options.expectedModel,
          );
          const rawClientRequestId = request.headers["x-client-request-id"];
          const clientRequestId = safeString(rawClientRequestId);
          const turnStateValues = request.headersDistinct[TURN_STATE_HEADER];
          const turnState =
            turnStateValues?.length === 1 ? codexTurnState(turnStateValues[0]) : null;
          const clientRequestIdEcho =
            typeof rawClientRequestId === "string" &&
            rawClientRequestId.includes(options.upstreamBearer);
          const turnStateEcho =
            turnStateValues?.some((value) => value.includes(options.upstreamBearer)) === true;
          if (
            turnStateValues !== undefined &&
            turnState === null &&
            !(clientRequestIdEcho || turnStateEcho)
          ) {
            throw new RelayHttpError(400, "invalid_turn_state");
          }
          if (Buffer.byteLength(canonicalJson(normalized.request)) > limits.maxRequestBytes) {
            throw new RelayHttpError(413, "request_too_large");
          }
          let admission: OutputBudgetAdmission;
          try {
            admission = outputBudget.admit(
              accepted + 1,
              normalized.requestedMaxOutputTokens,
            );
          } catch (error) {
            if (!(error instanceof OutputBudgetInputError)) throw error;
            if (options.budgetClass !== "unmetered_route_probe") {
              outputBudget.poison(error.code);
            }
            throw new RelayHttpError(400, error.code);
          }
          if (admission.kind === "rejected") {
            if (
              admission.code !== "slot_output_budget_exhausted" ||
              !isToolOutputContinuation(normalized.request, expectedToolContinuation)
            ) {
              outputBudget.poison("request_quota_exceeded");
              throw new RelayHttpError(429, "request_quota_exceeded");
            }
            throw new RelayHttpError(429, admission.code);
          }
          if (accepted >= limits.maxRequests) {
            outputBudget.poisonBeforeUpstream("request_quota_exceeded");
            throw new RelayHttpError(429, "request_quota_exceeded");
          }
          expectedToolContinuation = null;
          if (admission.effectiveMaxOutputTokens === null) {
            delete normalized.request.max_output_tokens;
          } else {
            normalized.request.max_output_tokens = admission.effectiveMaxOutputTokens;
          }
          const body = Buffer.from(canonicalJson(normalized.request));
          if (body.length > limits.maxRequestBytes) {
            outputBudget.poisonBeforeUpstream("request_too_large");
            throw new RelayHttpError(413, "request_too_large");
          }
          accepted += 1;
          ordinal = accepted;
          await journal.append({
            ...identity,
            event: "transport.responses.request",
            ordinal,
            relayRequestId,
            at: isoTime(startedAt),
            requestedModel: options.expectedModel,
            requestBytes: body.length,
            requestSha256: sha256(body),
            clientRequestId: clientRequestIdEcho ? null : clientRequestId,
            stream: true,
            requestedMaxOutputTokens: admission.requestedMaxOutputTokens,
            effectiveMaxOutputTokens: admission.effectiveMaxOutputTokens,
          });
          if (clientRequestIdEcho || turnStateEcho) {
            throw secretFailure();
          }

          let connectTimer: NodeJS.Timeout | undefined;
          let connectTimedOut = false;
          const upstreamHeaders: Record<string, string> = {
            accept: "text/event-stream",
            "accept-encoding": "identity",
            authorization: `Bearer ${options.upstreamBearer}`,
            "content-type": "application/json",
          };
          if (clientRequestId !== null) {
            upstreamHeaders["x-client-request-id"] = clientRequestId;
          }
          if (turnState !== null) {
            upstreamHeaders[TURN_STATE_HEADER] = turnState;
          }
          const upstreamResponse = await Promise.race([
            fetchImpl(upstream, {
              method: "POST",
              headers: upstreamHeaders,
              body: new Uint8Array(body),
              redirect: "manual",
              signal: controller.signal,
            }),
            new Promise<never>((_resolve, reject) => {
              connectTimer = setTimeout(() => {
                connectTimedOut = true;
                reject(new RelayHttpError(504, "upstream_connect_timeout"));
                controller.abort();
              }, limits.connectTimeoutMs);
            }),
          ])
            .catch((error: unknown) => {
              if (connectTimedOut) {
                throw new RelayHttpError(504, "upstream_connect_timeout");
              }
              throw error;
            })
            .finally(() => {
              if (connectTimer !== undefined) clearTimeout(connectTimer);
            });

          const headersAt = clock();
          upstreamStatus = upstreamResponse.status;
          const upstreamProviderRequestId =
            upstreamResponse.headers.get("x-request-id") ??
            upstreamResponse.headers.get("request-id");
          const upstreamModelHeader = upstreamResponse.headers.get("openai-model");
          const forwarded: Record<string, string> = {};
          for (const name of FORWARDED_HEADERS) {
            const value = upstreamResponse.headers.get(name);
            if (value !== null) forwarded[name] = value;
          }
          if (
            containsSecret(
              [upstreamProviderRequestId, ...Object.values(forwarded)],
              options.upstreamBearer,
            )
          ) {
            await upstreamResponse.body?.cancel().catch(() => undefined);
            throw secretFailure();
          }
          const upstreamTurnState = forwarded[TURN_STATE_HEADER];
          if (upstreamTurnState !== undefined && codexTurnState(upstreamTurnState) === null) {
            countRejection("invalid_turn_state");
            await upstreamResponse.body?.cancel().catch(() => undefined);
            throw new RelayHttpError(502, "upstream_failure");
          }
          providerRequestId = safeString(upstreamProviderRequestId);
          const modelHeader = safeString(upstreamModelHeader);
          observer = new SseMetadataObserver(modelHeader);
          await journal.append({
            ...identity,
            event: "transport.responses.headers",
            ordinal,
            relayRequestId,
            at: isoTime(headersAt),
            status: upstreamStatus,
            providerRequestId,
            modelHeader,
            headersMs: headersAt - startedAt,
          });
          headersRecorded = true;

          if (upstreamStatus >= 300 && upstreamStatus < 400) {
            await upstreamResponse.body?.cancel();
            throw new RelayHttpError(502, "upstream_redirect");
          }
          const contentEncoding = upstreamResponse.headers.get("content-encoding");
          if (contentEncoding !== null && contentEncoding !== "identity") {
            await upstreamResponse.body?.cancel();
            throw new RelayHttpError(502, "upstream_compressed");
          }
          const reader = upstreamResponse.body?.getReader();
          if (reader === undefined) throw new RelayHttpError(502, "upstream_body_missing");
          const success = upstreamStatus >= 200 && upstreamStatus < 300;
          const parser = success ? new ResponsesSseParser() : null;
          const guard = success ? new Codex149SecretGuard(options.upstreamBearer) : null;
          const failureChunks: Uint8Array[] = [];
          const forwardFrames = async (frames: ParsedSseFrame[]): Promise<void> => {
            for (const frame of frames) {
              const stage = guard!.stage(frame);
              if (stage.secret) throw secretFailure();
              if (frame.error !== null || stage.invalid) {
                observer.recordParseError();
                controller.abort();
                if (response.headersSent) response.destroy();
                throw new RelayHttpError(502, "upstream_failure");
              }
              if (frame.event !== null) observer.observe(frame.event);
              if (!response.headersSent) response.writeHead(upstreamResponse.status, forwarded);
              await writeWithBackpressure(response, frame.raw);
              stage.commit();
            }
          };
          while (true) {
            const result = await readWithIdleTimeout(reader, limits.idleTimeoutMs, controller);
            if (result.done) break;
            const chunk = result.value;
            if (chunk.byteLength > 0) firstByteAt ??= clock();
            responseBytes += chunk.byteLength;
            responseHash.update(chunk);
            if (responseBytes > limits.maxResponseBytes) {
              controller.abort();
              throw new RelayHttpError(502, "response_too_large");
            }
            if (parser === null) failureChunks.push(chunk);
            else await forwardFrames(parser.feed(chunk));
          }
          if (parser === null) {
            const failureBody = Buffer.concat(failureChunks, responseBytes);
            const failureInspection = inspectNonSuccessBody(failureBody, options.upstreamBearer);
            if (failureInspection === "secret") throw secretFailure();
            if (failureInspection === "invalid") {
              throw new RelayHttpError(502, "upstream_failure");
            }
            response.writeHead(upstreamResponse.status, forwarded);
            if (failureBody.byteLength > 0) await writeWithBackpressure(response, failureBody);
          } else {
            await forwardFrames(parser.finish());
            if (!response.headersSent) response.writeHead(upstreamResponse.status, forwarded);
          }
          transportState = "completed";
        } catch (error) {
          const relayError =
            error instanceof RelayHttpError
              ? error
              : new RelayHttpError(
                  expiredInFlight ? 401 : clientDisconnected ? 499 : 502,
                  expiredInFlight
                    ? "expired"
                    : clientDisconnected
                      ? "client_disconnected"
                      : controller.signal.aborted
                      ? "upstream_aborted"
                      : "upstream_failure",
                );
          terminalError = relayError;
          if (ordinal === 0) countRejection(relayError.code);
          errorCategory = relayError.code;
          transportState = relayError.code === "client_disconnected" ? "aborted" : "failed";
          if (!headersRecorded && ordinal > 0) {
            const failedHeadersAt = clock();
            await journal.append({
              ...identity,
              event: "transport.responses.headers",
              ordinal,
              relayRequestId,
              at: isoTime(failedHeadersAt),
              status: upstreamStatus,
              providerRequestId: null,
              modelHeader: null,
              headersMs: upstreamStatus === null ? null : failedHeadersAt - startedAt,
            });
            headersRecorded = true;
          }
        } finally {
          clearTimeout(deadline);
          response.off("close", clientClosed);
          controllers.delete(controller);
          requests.delete(request);
          try {
            if (ordinal > 0) {
              const endedAt = clock();
              let metadata = observer.finish();
              if (secretEchoConfirmed) metadata = redactedResponseMetadata(metadata.parseErrors);
              if (transportState === "completed") {
                const settlement = outputBudget.settle(metadata);
                expectedToolContinuation = settlement.kind === "settled"
                  ? observer.toolOutputContinuation()
                  : null;
              } else {
                outputBudget.poison(errorCategory ?? "transport_failed");
                expectedToolContinuation = null;
              }
              await journal.append({
                ...identity,
                event: "transport.responses.closed",
                ordinal,
                relayRequestId,
                at: isoTime(endedAt),
                transportState,
                errorCategory,
                status: upstreamStatus,
                providerRequestId,
                responseBytes,
                responseSha256: `sha256:${responseHash.digest("hex")}`,
                durationMs: endedAt - startedAt,
                firstByteMs: firstByteAt === null ? null : firstByteAt - startedAt,
                ...metadata,
              });
            }
            releaseFlight();
          } catch (error) {
            outputBudget.poison("journal_write_failure");
            closing = true;
            const successor = queuedFlight;
            queuedFlight = undefined;
            active = 0;
            successor?.cancel(new RelayHttpError(500, "relay_failure"));
            throw error;
          }
        }
        if (terminalError !== null) {
          writeError(response, terminalError);
        } else if (!(await endResponse(response))) {
          countRejection("client_disconnected_after_close");
        }
      })().catch((error: unknown) => {
        if (error instanceof RelayHttpError) {
          if (
            request.url === RESPONSES_PATH &&
            request.method === "POST" &&
            authorized(request.headers.authorization, options.clientBearer)
          ) {
            countRejection(error.code);
          }
          writeError(response, error);
          return;
        }
        closing = true;
        for (const controller of controllers) controller.abort();
        writeError(response, new RelayHttpError(500, "relay_failure"));
      });
      inFlight.add(task);
      void task.then(
        () => inFlight.delete(task),
        () => inFlight.delete(task),
      );
    },
  );
  server.on("clientError", (_error, socket) => socket.end("HTTP/1.1 400 Bad Request\r\n\r\n"));
  try {
    await new Promise<void>((resolve, reject) => {
      server.once("error", reject);
      server.listen(port, listenHost, () => {
        server.off("error", reject);
        resolve();
      });
    });
  } catch (error) {
    await journal.close().catch(() => undefined);
    throw error;
  }
  const address = server.address();
  if (address === null || typeof address === "string") {
    await journal.close();
    throw new Error("Relay did not bind a TCP address.");
  }
  let sealPromise: Promise<RelaySealSummary> | undefined;
  const seal = (): Promise<RelaySealSummary> => {
    sealPromise ??= (async () => {
      closing = true;
      queuedFlight?.cancel(new RelayHttpError(404, "relay_sealed"));
      const serverClosed = new Promise<void>((resolve, reject) => {
        server.close((error) => (error === undefined ? resolve() : reject(error)));
      });
      for (const controller of controllers) controller.abort();
      for (const request of requests) request.destroy(new Error("relay_sealed"));
      server.closeAllConnections();
      await serverClosed;
      while (inFlight.size > 0) await Promise.allSettled([...inFlight]);
      const summary = await journal.close();
      const body = {
        ...identity,
        state: "sealed",
        expectedModel: options.expectedModel,
        sealedAt: isoTime(clock()),
        rejectedRequests: { ...rejectedRequests },
        ...outputBudget.seal(),
        ...summary,
      } as const;
      return await writeRelaySeal(sealPath, body);
    })();
    return sealPromise;
  };
  return {
    baseUrl: `http://${listenHost}:${address.port}/v1`,
    sidecarPath: options.sidecarPath,
    sealPath,
    seal,
    close: seal,
  };
}
