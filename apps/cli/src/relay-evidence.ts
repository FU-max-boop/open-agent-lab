import { randomUUID } from "node:crypto";
import { mkdir, open, rename, rm, type FileHandle } from "node:fs/promises";
import { dirname } from "node:path";

import { canonicalJson } from "@open-agent-lab/contracts";
import { sha256 } from "@open-agent-lab/evidence";

import {
  outputBudgetTerminalKind,
  reportedOutputTokens,
} from "./responses-output-budget.js";

export const RELAY_VERSION = "native-responses-relay-v2";

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
    "requestedMaxOutputTokens",
    "effectiveMaxOutputTokens",
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
    "terminalStatus",
    "incompleteReason",
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
  "budgetClass",
  "accountingMode",
  "slotOutputTokenLimit",
  "outputTokenAccounting",
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
  "invalid_max_output_tokens",
  "invalid_turn_state",
  "model_mismatch",
  "not_found",
  "relay_sealed",
  "request_quota_exceeded",
  "request_too_large",
  "slot_output_budget_exhausted",
  "unsupported_content_type",
  "unsupported_response_mode",
  "upstream_failure",
  "upstream_secret_echo",
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
const TERMINAL_STATUSES = new Set(["completed", "failed", "incomplete"]);
const SCORED_ACCOUNTING_STATES = new Set(
  ["complete", "budget_terminal", "exact_exhaustion", "poisoned"],
);
const ZAI_PROBE_ACCOUNTING_STATES = new Set(["complete", "probe_conformant", "poisoned"]);
const OUTPUT_TOKEN_ACCOUNTING_FIELDS = new Set([
  "state",
  "reportedOutputTokens",
  "conservativeOutputTokenUpperBound",
  "unusedOutputTokensBurned",
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
  schemaVersion: 2;
  state: "sealed";
  relayVersion: typeof RELAY_VERSION;
  runId: string;
  relayInstanceId: string;
  providerId: string;
  buildId: string;
  expectedModel: string;
  sealedAt: string;
  rejectedRequests: Record<string, number>;
  budgetClass: "scored_slot" | "zai_route_probe" | "unmetered_route_probe";
  accountingMode: "sealed_usage_debit" | "fixed_round_allocations" | "none";
  slotOutputTokenLimit: number | null;
  outputTokenAccounting: {
    state: "complete" | "budget_terminal" | "exact_exhaustion" | "probe_conformant" |
      "poisoned" | "unmetered";
    reportedOutputTokens: number | null;
    conservativeOutputTokenUpperBound: number | null;
    unusedOutputTokensBurned: number;
  };
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

function integerBetween(
  value: unknown,
  minimum: number,
  maximum = Number.MAX_SAFE_INTEGER,
): value is number {
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
    record.schemaVersion === 2 &&
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
  const requestedMax = record.requestedMaxOutputTokens;
  const effectiveMax = record.effectiveMaxOutputTokens;
  return (
    typeof record.requestedModel === "string" &&
    MODEL_ID.test(record.requestedModel) &&
    integerBetween(record.requestBytes, 0) &&
    typeof record.requestSha256 === "string" &&
    SHA256.test(record.requestSha256) &&
    optionalText(record.clientRequestId) &&
    record.stream === true &&
    (requestedMax === null || integerBetween(requestedMax, 1)) &&
    (effectiveMax === null || integerBetween(effectiveMax, 1))
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
  const terminalEvent = record.terminalEvent;
  const terminalStatus = record.terminalStatus;
  const incompleteReason = record.incompleteReason;
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
    (terminalEvent === null ||
      (typeof terminalEvent === "string" && TERMINAL_EVENTS.has(terminalEvent))) &&
    (terminalStatus === null ||
      (typeof terminalStatus === "string" && TERMINAL_STATUSES.has(terminalStatus))) &&
    optionalText(incompleteReason) &&
    ((terminalEvent === null && terminalStatus === null && incompleteReason === null) ||
      (terminalEvent === "response.completed" &&
        (terminalStatus === null || terminalStatus === "completed") &&
        incompleteReason === null) ||
      (terminalEvent === "response.failed" &&
        (terminalStatus === null || terminalStatus === "failed") &&
        incompleteReason === null) ||
      (terminalEvent === "response.incomplete" &&
        (terminalStatus === null || terminalStatus === "incomplete") &&
        boundedText(incompleteReason))) &&
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
    ["client_disconnected_after_close", "upstream_secret_echo"].every(
      (code) => Number(value[code] ?? 0) <= lifecycles,
    )
  );
}

function validOutputTokenAccounting(
  value: unknown,
  budgetClass: unknown,
  slotOutputTokenLimit: unknown,
): boolean {
  if (!isObject(value) || !exactFields(value, OUTPUT_TOKEN_ACCOUNTING_FIELDS)) return false;
  const state = value.state;
  const reported = value.reportedOutputTokens;
  const upper = value.conservativeOutputTokenUpperBound;
  const burned = value.unusedOutputTokensBurned;
  if (
    typeof state !== "string" ||
    (reported !== null && !integerBetween(reported, 0)) ||
    (upper !== null && !integerBetween(upper, 0)) ||
    !integerBetween(burned, 0)
  ) {
    return false;
  }
  if (budgetClass === "unmetered_route_probe") {
    return (
      burned === 0 &&
      ((state === "unmetered" && integerBetween(reported, 0) && upper === reported) ||
        (state === "poisoned" && reported === null && upper === null))
    );
  }
  if (!integerBetween(slotOutputTokenLimit, 1)) return false;
  const limit = slotOutputTokenLimit as number;
  if (
    (budgetClass === "scored_slot" && !SCORED_ACCOUNTING_STATES.has(state)) ||
    (budgetClass === "zai_route_probe" && !ZAI_PROBE_ACCOUNTING_STATES.has(state))
  ) {
    return false;
  }
  if (state === "poisoned") {
    return (
      reported === null &&
      integerBetween(upper, 0, limit) &&
      integerBetween(burned, 0, limit) &&
      upper === limit - burned
    );
  }
  if (!integerBetween(reported, 0, limit) || upper !== reported) return false;
  if (state === "complete" && budgetClass === "zai_route_probe") {
    return reported <= 8_192 && integerBetween(burned, 0, 8_192) && reported === 8_192 - burned;
  }
  if (state === "exact_exhaustion") return reported === limit && burned === 0;
  return integerBetween(burned, 0, limit) && reported === limit - burned;
}

function validBudgetPolicy(body: Record<string, unknown>): boolean {
  const budgetClass = body.budgetClass;
  const accountingMode = body.accountingMode;
  const limit = body.slotOutputTokenLimit;
  const policyMatches =
    (budgetClass === "scored_slot" &&
      accountingMode === "sealed_usage_debit" &&
      limit === 50_000) ||
    (budgetClass === "zai_route_probe" &&
      accountingMode === "fixed_round_allocations" &&
      limit === 8_448) ||
    (budgetClass === "unmetered_route_probe" && accountingMode === "none" && limit === null);
  return policyMatches &&
    validOutputTokenAccounting(body.outputTokenAccounting, budgetClass, limit);
}

function settledBudgetLifecycle(
  closed: Record<string, unknown> | undefined,
  effectiveMaxOutputTokens: number | null,
  expectedTerminal?: "completed" | "max_output_tokens",
): { outputTokens: number; terminal: "completed" | "max_output_tokens" } | null {
  if (closed?.transportState !== "completed") return null;
  const terminal = outputBudgetTerminalKind({
    terminalEvent: closed.terminalEvent,
    terminalStatus: closed.terminalStatus,
    incompleteReason: closed.incompleteReason,
    usage: closed.usage,
    metadataConflicts: closed.metadataConflicts,
  });
  const outputTokens = reportedOutputTokens(closed.usage);
  const usageConflict =
    Array.isArray(closed.metadataConflicts) && closed.metadataConflicts.includes("usage");
  if (
    terminal === null ||
    (expectedTerminal !== undefined && terminal !== expectedTerminal) ||
    outputTokens === null ||
    usageConflict ||
    (effectiveMaxOutputTokens !== null && outputTokens > effectiveMaxOutputTokens)
  ) {
    return null;
  }
  return { outputTokens, terminal };
}

function validBudgetedRecords(
  records: Record<string, unknown>[],
  body: Record<string, unknown>,
): boolean {
  const groups = Array.from({ length: records.length / 3 }, (_, index) =>
    records.slice(index * 3, index * 3 + 3),
  );
  const accounting = body.outputTokenAccounting;
  if (!isObject(accounting)) return false;
  if (body.budgetClass === "unmetered_route_probe") {
    if (
      groups.some(([request]) =>
        request?.requestedMaxOutputTokens === null
          ? request.effectiveMaxOutputTokens !== null
          : request?.effectiveMaxOutputTokens !== request?.requestedMaxOutputTokens,
      )
    ) {
      return false;
    }
    const outputs = groups.map(([request, , closed]) =>
      settledBudgetLifecycle(
        closed,
        request?.effectiveMaxOutputTokens as number | null,
        "completed",
      )?.outputTokens ?? null
    );
    if (accounting.state === "poisoned") {
      return groups.length > 0 && outputs.slice(0, -1).every((output) => output !== null);
    }
    if (outputs.includes(null)) return false;
    const total = outputs.reduce<number>((sum, output) => sum + (output ?? 0), 0);
    return accounting.state === "unmetered" &&
      Number.isSafeInteger(total) && total === accounting.reportedOutputTokens;
  }
  const limit = body.slotOutputTokenLimit;
  if (!integerBetween(limit, 1)) return false;
  if (body.budgetClass === "zai_route_probe") {
    const expectedAllocations = [8_192, 256];
    if (
      groups.length > 2 ||
      groups.some(([request], index) =>
        request?.effectiveMaxOutputTokens !== expectedAllocations[index],
      )
    ) return false;
  }
  let reportedTotal = 0;
  let remaining = limit as number;
  const poisoned = accounting.state === "poisoned";
  for (const [index, [request, , closed]] of groups.entries()) {
    const requested = request?.requestedMaxOutputTokens;
    const effective = request?.effectiveMaxOutputTokens;
    if (!integerBetween(effective, 1, remaining)) return false;
    if (body.budgetClass === "scored_slot") {
      const expectedEffective = Math.min(
        requested === null ? remaining : (requested as number),
        remaining,
      );
      if (effective !== expectedEffective) return false;
    }
    const isPoisonLifecycle = poisoned && index === groups.length - 1;
    const expectedTerminal = body.budgetClass === "zai_route_probe"
      ? index === 0 ? "completed" : "max_output_tokens"
      : index < groups.length - 1 ? "completed" : undefined;
    const lifecycle = settledBudgetLifecycle(
      closed,
      effective as number,
      expectedTerminal,
    );
    if (isPoisonLifecycle && lifecycle === null) {
      reportedTotal += effective;
      remaining -= effective;
      continue;
    }
    if (lifecycle === null) return false;
    reportedTotal += lifecycle.outputTokens;
    remaining -= lifecycle.outputTokens;
    if (!Number.isSafeInteger(reportedTotal)) return false;
  }
  if (poisoned) {
    if (groups.length === 0) reportedTotal = 0;
    return (
      accounting.conservativeOutputTokenUpperBound === reportedTotal &&
      accounting.unusedOutputTokensBurned === (limit as number) - reportedTotal
    );
  }
  const reported = accounting.reportedOutputTokens;
  if (reported !== reportedTotal) return false;
  const lastClosed = groups.at(-1)?.[2];
  return accounting.state === "complete"
    ? lastClosed?.terminalEvent === "response.completed" &&
        (body.budgetClass !== "zai_route_probe" || groups.length === 1)
    : accounting.state === "budget_terminal"
      ? lastClosed?.terminalEvent === "response.incomplete" &&
        lastClosed.incompleteReason === "max_output_tokens"
      : accounting.state === "exact_exhaustion"
        ? lastClosed?.terminalEvent === "response.completed" &&
          (body.rejectedRequests as Record<string, unknown>).slot_output_budget_exhausted === 1
        : accounting.state === "probe_conformant"
          ? (
      groups.length === 2 &&
      groups[0]?.[2]?.terminalEvent === "response.completed" &&
      groups[1]?.[2]?.terminalEvent === "response.incomplete" &&
      groups[1]?.[2]?.incompleteReason === "max_output_tokens"
            )
          : true;
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
        record?.schemaVersion !== 2 ||
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
    body.schemaVersion !== 2 ||
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
    !validBudgetPolicy(body) ||
    !validBudgetedRecords(records, body) ||
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
