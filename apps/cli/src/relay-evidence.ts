import { randomUUID } from "node:crypto";
import { mkdir, open, rename, rm, type FileHandle } from "node:fs/promises";
import { dirname } from "node:path";

import { canonicalJson } from "@open-agent-lab/contracts";
import { sha256 } from "@open-agent-lab/evidence";

export const RELAY_VERSION = "native-responses-relay-v1";

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
    if (!isObject(parsed) || typeof parsed.eventSha256 !== "string") {
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
  const marker = JSON.parse(markerContent) as unknown;
  if (!isObject(marker) || typeof marker.markerSha256 !== "string") {
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
    !/^(?:sha256:[a-f0-9]{64}|development)$/u.test(body.buildId) ||
    typeof body.expectedModel !== "string" ||
    typeof body.sealedAt !== "string" ||
    !isObject(body.rejectedRequests) ||
    !Object.values(body.rejectedRequests).every(
      (value) => typeof value === "number" && Number.isSafeInteger(value) && value >= 0,
    ) ||
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
