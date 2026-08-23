import { randomUUID } from "node:crypto";
import { mkdir, open, rename, rm, type FileHandle } from "node:fs/promises";
import { dirname } from "node:path";

import { canonicalJson } from "@open-agent-lab/contracts";
import { sha256 } from "@open-agent-lab/evidence";

export const RELAY_VERSION = "native-responses-relay-v1";

const COMMON_RECORD_FIELDS = [
  "schemaVersion",
  "relayVersion",
  "runId",
  "relayInstanceId",
  "providerId",
  "buildId",
  "event",
  "ordinal",
  "relayRequestId",
  "at",
  "previousEventSha256",
  "eventSha256",
] as const;
const recordFields = (...specific: string[]): ReadonlySet<string> =>
  new Set([...COMMON_RECORD_FIELDS, ...specific]);
const RECORD_FIELDS: Readonly<Record<string, ReadonlySet<string>>> = {
  "transport.responses.request": recordFields(
    "requestedModel",
    "requestBytes",
    "requestSha256",
    "clientRequestId",
    "stream",
  ),
  "transport.responses.headers": recordFields(
    "status",
    "providerRequestId",
    "modelHeader",
    "headersMs",
  ),
  "transport.responses.closed": recordFields(
    "transportState",
    "errorCategory",
    "status",
    "providerRequestId",
    "responseBytes",
    "responseSha256",
    "durationMs",
    "firstByteMs",
    "responseId",
    "returnedModel",
    "modelConsistency",
    "modelSources",
    "systemFingerprint",
    "terminalEvent",
    "usage",
    "metadataConflicts",
    "parseErrors",
  ),
};
const SEAL_FIELDS = new Set([
  "schemaVersion",
  "state",
  "relayVersion",
  "runId",
  "relayInstanceId",
  "providerId",
  "buildId",
  "expectedModel",
  "sealedAt",
  "rejectedRequests",
  "eventCount",
  "chainHead",
  "markerSha256",
]);
const USAGE_FIELDS = new Set([
  "input_tokens",
  "output_tokens",
  "total_tokens",
  "cached_input_tokens",
  "reasoning_output_tokens",
]);
const METADATA_CONFLICTS = new Set([
  "model",
  "response_id",
  "system_fingerprint",
  "terminal_event",
  "usage",
]);
const REJECTION_CODES = new Set([
  "client_disconnected",
  "client_disconnected_after_close",
  "concurrency_exceeded",
  "expired",
  "invalid_json",
  "model_mismatch",
  "not_found",
  "relay_sealed",
  "request_quota_exceeded",
  "request_too_large",
  "unsupported_content_type",
  "unsupported_response_mode",
  "upstream_failure",
]);
const MODEL_SOURCE = /^event\.(.+)\.response\.(?:model|headers\.openai-model)\.([1-9][0-9]*)$/su;
const BUILD_ID = /^(?:sha256:[a-f0-9]{64}|development)$/u;
const SHA256 = /^sha256:[a-f0-9]{64}$/u;
const UUID4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u;
const RUN_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/u;
const PROVIDER_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/u;
const MODEL_ID = /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/u;
const TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/u;
const TERMINAL_EVENTS = new Set([
  "response.completed",
  "response.failed",
  "response.incomplete",
]);
const MODEL_CONSISTENCIES = new Set(["consistent", "conflict", "missing"]);
const FAILED_TRANSPORT_ERRORS = new Set([
  "expired",
  "response_too_large",
  "upstream_aborted",
  "upstream_body_missing",
  "upstream_compressed",
  "upstream_connect_timeout",
  "upstream_failure",
  "upstream_idle_timeout",
  "upstream_redirect",
]);

export interface RelayJournalSummary {
  eventCount: number;
  chainHead: string | null;
}

export interface RelaySealSummary extends RelayJournalSummary {
  schemaVersion: 1;
  state: "sealed";
  relayVersion: typeof RELAY_VERSION;
  runId: string;
  relayInstanceId: string;
  providerId: string;
  buildId: string;
  expectedModel: string;
  sealedAt: string;
  rejectedRequests: Record<string, number>;
  markerSha256: string;
}

export class RelayJournal {
  private previous: string | null = null;
  private count = 0;
  private failure: unknown;
  private tail: Promise<void> = Promise.resolve();

  private constructor(private readonly handle: FileHandle) {}

  static async create(path: string): Promise<RelayJournal> {
    await mkdir(dirname(path), { recursive: true });
    return new RelayJournal(await open(path, "ax", 0o600));
  }

  append(event: Record<string, unknown>): Promise<void> {
    const operation = this.tail.then(async () => {
      if (this.failure !== undefined) throw this.failure;
      const body = { ...event, previousEventSha256: this.previous };
      const eventSha256 = sha256(canonicalJson(body));
      const line = Buffer.from(`${canonicalJson({ ...body, eventSha256 })}\n`);
      for (let offset = 0; offset < line.length; ) {
        const { bytesWritten } = await this.handle.write(
          line,
          offset,
          line.length - offset,
          null,
        );
        if (bytesWritten === 0) throw new Error("Relay journal write made no progress.");
        offset += bytesWritten;
      }
      await this.handle.sync();
      this.previous = eventSha256;
      this.count += 1;
    });
    this.tail = operation.then(
      () => undefined,
      (error: unknown) => {
        this.failure ??= error;
      },
    );
    return operation;
  }

  summary(): RelayJournalSummary {
    return { eventCount: this.count, chainHead: this.previous };
  }

  async close(): Promise<RelayJournalSummary> {
    await this.tail;
    await this.handle.close();
    if (this.failure !== undefined) throw this.failure;
    return this.summary();
  }
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function exactFields(value: Record<string, unknown>, expected: ReadonlySet<string>): boolean {
  const keys = Object.keys(value);
  return keys.length === expected.size && keys.every((key) => expected.has(key));
}

function boundedText(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    value.length <= 512 &&
    !/[\u0000-\u001f]/u.test(value)
  );
}

function optionalText(value: unknown): boolean {
  return value === null || boundedText(value);
}

function integerBetween(value: unknown, minimum: number, maximum = Number.MAX_SAFE_INTEGER): boolean {
  return (
    typeof value === "number" &&
    Number.isSafeInteger(value) &&
    value >= minimum &&
    value <= maximum
  );
}

function validTimestamp(value: unknown): boolean {
  if (typeof value !== "string" || !TIMESTAMP.test(value)) return false;
  const milliseconds = Date.parse(value);
  return Number.isFinite(milliseconds) && new Date(milliseconds).toISOString() === value;
}

function validStatus(value: unknown): boolean {
  return value === null || integerBetween(value, 200, 599);
}

function validCommonShapes(record: Record<string, unknown>): boolean {
  return (
    record.schemaVersion === 1 &&
    record.relayVersion === RELAY_VERSION &&
    typeof record.runId === "string" &&
    RUN_ID.test(record.runId) &&
    typeof record.relayInstanceId === "string" &&
    UUID4.test(record.relayInstanceId) &&
    typeof record.providerId === "string" &&
    PROVIDER_ID.test(record.providerId) &&
    typeof record.buildId === "string" &&
    BUILD_ID.test(record.buildId) &&
    boundedText(record.event) &&
    integerBetween(record.ordinal, 1) &&
    typeof record.relayRequestId === "string" &&
    UUID4.test(record.relayRequestId) &&
    validTimestamp(record.at) &&
    (record.previousEventSha256 === null ||
      (typeof record.previousEventSha256 === "string" &&
        SHA256.test(record.previousEventSha256))) &&
    typeof record.eventSha256 === "string" &&
    SHA256.test(record.eventSha256)
  );
}

function validModelSources(value: unknown, responseBytes: unknown): boolean {
  if (!integerBetween(responseBytes, 0) || !isObject(value) || Object.keys(value).length > 16) {
    return false;
  }
  const indexed = new Map<string, string>();
  for (const [key, model] of Object.entries(value)) {
    if (!boundedText(model)) return false;
    if (key === "http.openai-model.0") continue;
    const match = MODEL_SOURCE.exec(key);
    if (match === null || !boundedText(match[1])) return false;
    const [event, index] = [match[1], match[2]] as [string, string];
    const numericIndex = Number(index);
    if (!integerBetween(numericIndex, 1, responseBytes as number)) return false;
    const observed = indexed.get(index);
    if (observed !== undefined && observed !== event) return false;
    indexed.set(index, event);
  }
  return true;
}

function validRequestShapes(record: Record<string, unknown>): boolean {
  return (
    typeof record.requestedModel === "string" &&
    MODEL_ID.test(record.requestedModel) &&
    integerBetween(record.requestBytes, 0) &&
    typeof record.requestSha256 === "string" &&
    SHA256.test(record.requestSha256) &&
    optionalText(record.clientRequestId) &&
    record.stream === true
  );
}

function validHeadersShapes(record: Record<string, unknown>): boolean {
  return (
    validStatus(record.status) &&
    optionalText(record.providerRequestId) &&
    optionalText(record.modelHeader) &&
    (record.headersMs === null || integerBetween(record.headersMs, 0))
  );
}

function validClosedShapes(record: Record<string, unknown>): boolean {
  const usage = record.usage;
  const conflicts = record.metadataConflicts;
  const state = record.transportState;
  const error = record.errorCategory;
  const stateMatchesError =
    (state === "completed" && error === null) ||
    (state === "aborted" && error === "client_disconnected") ||
    (state === "failed" && typeof error === "string" && FAILED_TRANSPORT_ERRORS.has(error));
  return (
    stateMatchesError &&
    validStatus(record.status) &&
    optionalText(record.providerRequestId) &&
    integerBetween(record.responseBytes, 0) &&
    typeof record.responseSha256 === "string" &&
    SHA256.test(record.responseSha256) &&
    integerBetween(record.durationMs, 0) &&
    (record.firstByteMs === null || integerBetween(record.firstByteMs, 0)) &&
    optionalText(record.responseId) &&
    optionalText(record.returnedModel) &&
    typeof record.modelConsistency === "string" &&
    MODEL_CONSISTENCIES.has(record.modelConsistency) &&
    validModelSources(record.modelSources, record.responseBytes) &&
    optionalText(record.systemFingerprint) &&
    (record.terminalEvent === null ||
      (typeof record.terminalEvent === "string" && TERMINAL_EVENTS.has(record.terminalEvent))) &&
    (usage === null ||
      (isObject(usage) &&
        Object.keys(usage).every((key) => USAGE_FIELDS.has(key)) &&
        Object.values(usage).every(
          (value) => Number.isSafeInteger(value) && (value as number) >= 0,
        ))) &&
    Array.isArray(conflicts) &&
    new Set(conflicts).size === conflicts.length &&
    conflicts.every((value) => typeof value === "string" && METADATA_CONFLICTS.has(value)) &&
    integerBetween(record.parseErrors, 0)
  );
}

const RECORD_SHAPES: Readonly<
  Record<string, (value: Record<string, unknown>) => boolean>
> = {
  "transport.responses.request": validRequestShapes,
  "transport.responses.headers": validHeadersShapes,
  "transport.responses.closed": validClosedShapes,
};

function validRecordShape(record: Record<string, unknown>): boolean {
  const fields = typeof record.event === "string" ? RECORD_FIELDS[record.event] : undefined;
  const validator = typeof record.event === "string" ? RECORD_SHAPES[record.event] : undefined;
  return (
    fields !== undefined &&
    validator !== undefined &&
    exactFields(record, fields) &&
    validCommonShapes(record) &&
    validator(record)
  );
}

function validRejections(value: unknown, lifecycles: number): boolean {
  return (
    isObject(value) &&
    Object.entries(value).every(
      ([code, count]) =>
        REJECTION_CODES.has(code) && Number.isSafeInteger(count) && (count as number) > 0,
    ) &&
    Number(value.client_disconnected_after_close ?? 0) <= lifecycles
  );
}

async function atomicFile(path: string, content: string): Promise<void> {
  const temporary = `${path}.${randomUUID()}.tmp`;
  const handle = await open(temporary, "wx", 0o600);
  try {
    const bytes = Buffer.from(content);
    for (let offset = 0; offset < bytes.length; ) {
      const result = await handle.write(bytes, offset, bytes.length - offset, null);
      if (result.bytesWritten === 0) throw new Error("Atomic file write made no progress.");
      offset += result.bytesWritten;
    }
    await handle.sync();
  } catch (error) {
    await handle.close().catch(() => undefined);
    await rm(temporary, { force: true }).catch(() => undefined);
    throw error;
  }
  await handle.close();
  await rename(temporary, path);
  const directory = await open(dirname(path), "r");
  try {
    await directory.sync();
  } finally {
    await directory.close();
  }
}

function relayRecords(content: string): Record<string, unknown>[] {
  if (content !== "" && (!content.endsWith("\n") || content.endsWith("\n\n"))) {
    throw new Error("Relay journal must end with exactly one newline.");
  }
  const lines = content.split("\n");
  if (lines.at(-1) === "") lines.pop();
  if (lines.length === 0) return [];
  let previous: string | null = null;
  const records: Record<string, unknown>[] = [];
  for (const [index, line] of lines.entries()) {
    let parsed: unknown;
    try {
      parsed = JSON.parse(line) as unknown;
    } catch {
      throw new Error(`Invalid relay journal JSON at line ${index + 1}.`);
    }
    if (
      !isObject(parsed) ||
      line !== canonicalJson(parsed) ||
      typeof parsed.eventSha256 !== "string" ||
      !validRecordShape(parsed)
    ) {
      throw new Error(`Invalid relay journal record at line ${index + 1}.`);
    }
    const { eventSha256, ...body } = parsed;
    if (body.previousEventSha256 !== previous || sha256(canonicalJson(body)) !== eventSha256) {
      throw new Error(`Relay journal chain mismatch at line ${index + 1}.`);
    }
    previous = eventSha256;
    records.push(parsed);
  }
  return records;
}

export function verifyRelayJournal(content: string): RelayJournalSummary {
  const records = relayRecords(content);
  if (records.length === 0) return { eventCount: 0, chainHead: null };
  if (records.length % 3 !== 0) throw new Error("Relay journal has an incomplete lifecycle.");
  let runId: unknown;
  let relayInstanceId: unknown;
  let providerId: unknown;
  let buildId: unknown;
  let requestedModel: unknown;
  const requestIds = new Set<string>();
  for (let index = 0; index < records.length; index += 3) {
    const group = records.slice(index, index + 3);
    const ordinal = index / 3 + 1;
    const expectedEvents = [
      "transport.responses.request",
      "transport.responses.headers",
      "transport.responses.closed",
    ];
    const requestId = group[0]?.relayRequestId;
    if (typeof requestId !== "string" || requestIds.has(requestId)) {
      throw new Error(`Relay journal request identity mismatch at ordinal ${ordinal}.`);
    }
    requestIds.add(requestId);
    runId ??= group[0]?.runId;
    relayInstanceId ??= group[0]?.relayInstanceId;
    providerId ??= group[0]?.providerId;
    buildId ??= group[0]?.buildId;
    requestedModel ??= group[0]?.requestedModel;
    if (
      typeof runId !== "string" ||
      typeof relayInstanceId !== "string" ||
      typeof providerId !== "string" ||
      typeof buildId !== "string" ||
      typeof requestedModel !== "string" ||
      group[0]?.requestedModel !== requestedModel ||
      group[1]?.status !== group[2]?.status ||
      group[1]?.providerRequestId !== group[2]?.providerRequestId
    ) {
      throw new Error(`Relay journal identity is missing at ordinal ${ordinal}.`);
    }
    for (const [offset, record] of group.entries()) {
      if (
        record?.schemaVersion !== 1 ||
        record.relayVersion !== RELAY_VERSION ||
        record.event !== expectedEvents[offset] ||
        record.ordinal !== ordinal ||
        record.runId !== runId ||
        record.relayInstanceId !== relayInstanceId ||
        record.providerId !== providerId ||
        record.buildId !== buildId ||
        record.relayRequestId !== requestId
      ) {
        throw new Error(`Relay journal lifecycle mismatch at ordinal ${ordinal}.`);
      }
    }
  }
  return {
    eventCount: records.length,
    chainHead: records.at(-1)?.eventSha256 as string,
  };
}

export function verifyRelaySeal(
  journalContent: string,
  markerContent: string,
): RelaySealSummary {
  const journal = verifyRelayJournal(journalContent);
  let marker: unknown;
  try {
    marker = JSON.parse(markerContent) as unknown;
  } catch {
    throw new Error("Invalid relay seal marker.");
  }
  if (!isObject(marker)) throw new Error("Invalid relay seal marker.");
  if (markerContent !== `${canonicalJson(marker)}\n`) {
    throw new Error("Relay seal marker is not canonical JSON.");
  }
  if (!exactFields(marker, SEAL_FIELDS) || typeof marker.markerSha256 !== "string") {
    throw new Error("Invalid relay seal marker.");
  }
  const { markerSha256, ...body } = marker;
  const records = relayRecords(journalContent);
  const first = records[0];
  if (
    sha256(canonicalJson(body)) !== markerSha256 ||
    body.schemaVersion !== 1 ||
    body.state !== "sealed" ||
    body.relayVersion !== RELAY_VERSION ||
    typeof body.runId !== "string" ||
    typeof body.relayInstanceId !== "string" ||
    typeof body.providerId !== "string" ||
    typeof body.buildId !== "string" ||
    !BUILD_ID.test(body.buildId) ||
    !RUN_ID.test(body.runId) ||
    !UUID4.test(body.relayInstanceId) ||
    !PROVIDER_ID.test(body.providerId) ||
    typeof body.expectedModel !== "string" ||
    !MODEL_ID.test(body.expectedModel) ||
    !validTimestamp(body.sealedAt) ||
    !validRejections(body.rejectedRequests, journal.eventCount / 3) ||
    body.eventCount !== journal.eventCount ||
    body.chainHead !== journal.chainHead ||
    (first !== undefined &&
      (first.runId !== body.runId ||
        first.relayInstanceId !== body.relayInstanceId ||
        first.providerId !== body.providerId ||
        first.buildId !== body.buildId ||
        first.requestedModel !== body.expectedModel))
  ) {
    throw new Error("Relay seal marker mismatch.");
  }
  return marker as unknown as RelaySealSummary;
}

export async function writeRelaySeal(
  path: string,
  body: Omit<RelaySealSummary, "markerSha256">,
): Promise<RelaySealSummary> {
  const marker = { ...body, markerSha256: sha256(canonicalJson(body)) };
  await atomicFile(path, `${canonicalJson(marker)}\n`);
  return marker;
}
