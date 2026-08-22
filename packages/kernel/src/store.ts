import { randomUUID } from "node:crypto";
import {
  chmodSync,
  linkSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  rmdirSync,
  rmSync,
} from "node:fs";
import { basename, dirname, join, resolve } from "node:path";

import { canonicalJson, type Sha256Digest } from "@open-agent-lab/contracts";
import Database from "better-sqlite3";

import { DIGEST_PATTERN, kernelDigest } from "./digest.js";
import { KernelError } from "./errors.js";
import {
  KERNEL_CHECKPOINT_VERSION,
  KERNEL_EVENT_VERSION,
  KERNEL_STATE_VERSION,
  KERNEL_STORE_VERSION,
  type EventHead,
  type KernelCheckpointV1,
  type KernelEventV1,
  type KernelStateSnapshotV1,
  type KernelStoreMetadataV1,
} from "./types.js";

const DB_FILE = "run.sqlite3";
const DEFAULT_LEASE_MS = 30_000;
const CHECKPOINT_SCHEMA = `CREATE TABLE checkpoint(
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  sequence INTEGER NOT NULL,
  event_hash TEXT NOT NULL,
  checkpoint_hash TEXT NOT NULL,
  checkpoint_json TEXT NOT NULL
) STRICT`;
const SCHEMA = `
CREATE TABLE metadata(
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  schema_version TEXT NOT NULL,
  run_id TEXT NOT NULL,
  task_digest TEXT NOT NULL,
  created_at TEXT NOT NULL,
  initialized INTEGER NOT NULL CHECK(initialized IN (0,1))
) STRICT;
CREATE TABLE events(
  sequence INTEGER PRIMARY KEY CHECK(sequence>=0),
  event_hash TEXT NOT NULL UNIQUE,
  event_json TEXT NOT NULL
) STRICT;
${CHECKPOINT_SCHEMA};
CREATE TABLE lease(
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  owner TEXT NOT NULL,
  expires_at_ms INTEGER NOT NULL
) STRICT;`;
const UPSERT_CHECKPOINT = `INSERT INTO checkpoint VALUES(1,?,?,?,?)
  ON CONFLICT(singleton) DO UPDATE SET
    sequence=excluded.sequence,
    event_hash=excluded.event_hash,
    checkpoint_hash=excluded.checkpoint_hash,
    checkpoint_json=excluded.checkpoint_json`;

export interface SqliteKernelStoreOptions {
  clock?: () => string;
  now?: () => number;
  leaseMs?: number;
}

export interface SqliteKernelStoreCreateOptions extends SqliteKernelStoreOptions {
  createdAt?: string;
}

interface MetadataRow {
  schema_version: string;
  run_id: string;
  task_digest: string;
  created_at: string;
  initialized: number;
}

interface EventRow {
  sequence: number;
  event_hash: string;
  event_json: string;
}

interface CommittedHead {
  sequence: number;
  eventHash: Sha256Digest;
}

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function canonicalTimestamp(value: unknown): value is string {
  if (typeof value !== "string") return false;
  const parsed = new Date(value);
  return Number.isFinite(parsed.valueOf()) && parsed.toISOString() === value;
}

function safeRunId(value: string): string {
  if (
    value.trim() === "" ||
    value.length > 256 ||
    /[\u0000-\u001f\u007f]/u.test(value)
  ) {
    throw new KernelError("invalid_store", "runId must be a non-empty safe string.");
  }
  return value;
}

function strictLeaseMs(value: number | undefined): number {
  const leaseMs = value ?? DEFAULT_LEASE_MS;
  if (!Number.isSafeInteger(leaseMs) || leaseMs <= 0) {
    throw new KernelError("invalid_store", "leaseMs must be a positive safe integer.");
  }
  return leaseMs;
}

function strictTime(value: number): number {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new KernelError("invalid_store", "Clock is invalid.");
  }
  return value;
}

function leaseExpiry(now: number, leaseMs: number): number {
  const expiry = now + leaseMs;
  if (!Number.isSafeInteger(expiry)) {
    throw new KernelError("invalid_store", "Lease expiry overflowed.");
  }
  return expiry;
}

function parseCanonical(raw: string): Record<string, unknown> | undefined {
  try {
    const value: unknown = JSON.parse(raw);
    return record(value) && canonicalJson(value) === raw ? value : undefined;
  } catch {
    return undefined;
  }
}

function encode(value: unknown, label: string): string {
  try {
    return canonicalJson(value);
  } catch (error) {
    throw new KernelError("invalid_state", `${label} must be strict JSON.`, { cause: error });
  }
}

function validEvent(
  value: Record<string, unknown>,
  runId: string,
  sequence: number,
  previousEventHash: Sha256Digest | null,
): boolean {
  const { eventHash, ...body } = value;
  return (
    Object.keys(value).length === 8 &&
    value.schemaVersion === KERNEL_EVENT_VERSION &&
    value.runId === runId &&
    value.sequence === sequence &&
    canonicalTimestamp(value.timestamp) &&
    typeof value.type === "string" &&
    value.type.trim() !== "" &&
    record(value.data) &&
    value.previousEventHash === previousEventHash &&
    typeof eventHash === "string" &&
    DIGEST_PATTERN.test(eventHash) &&
    kernelDigest(body) === eventHash
  );
}

function connect(path: string, fileMustExist: boolean): Database.Database {
  let database: Database.Database | undefined;
  try {
    database = new Database(path, { fileMustExist });
    database.pragma("busy_timeout = 5000");
    database.pragma("journal_mode = WAL");
    database.pragma("synchronous = FULL");
    database.pragma("foreign_keys = ON");
    database.pragma("trusted_schema = OFF");
    return database;
  } catch (error) {
    database?.close();
    throw new KernelError("invalid_store", `Cannot open kernel store: ${path}`, { cause: error });
  }
}

function readMetadata(database: Database.Database, runId: string) {
  let row: MetadataRow | undefined;
  try {
    row = database.prepare("SELECT * FROM metadata WHERE singleton=1").get() as
      | MetadataRow
      | undefined;
  } catch (error) {
    throw new KernelError("invalid_store", "Kernel metadata is unavailable.", { cause: error });
  }
  if (row?.run_id !== runId) {
    throw new KernelError("run_id_mismatch", "Kernel store runId does not match.");
  }
  if (
    row.schema_version !== KERNEL_STORE_VERSION ||
    !DIGEST_PATTERN.test(row.task_digest) ||
    !canonicalTimestamp(row.created_at) ||
    (row.initialized !== 0 && row.initialized !== 1)
  ) {
    throw new KernelError("invalid_store", "Kernel store metadata is invalid.");
  }
  return {
    metadata: {
      schemaVersion: KERNEL_STORE_VERSION,
      runId,
      taskDigest: row.task_digest as Sha256Digest,
      createdAt: row.created_at,
    } satisfies KernelStoreMetadataV1,
    initialized: row.initialized === 1,
  };
}

function checkpointFor(
  metadata: KernelStoreMetadataV1,
  head: CommittedHead,
  state: KernelStateSnapshotV1,
): KernelCheckpointV1 {
  const body = {
    schemaVersion: KERNEL_CHECKPOINT_VERSION,
    runId: metadata.runId,
    lastSequence: head.sequence,
    lastEventHash: head.eventHash,
    state,
  };
  return { ...body, checkpointHash: kernelDigest(body) };
}

export class SqliteKernelStore {
  readonly metadata: Readonly<KernelStoreMetadataV1>;
  readonly heartbeatIntervalMs: number;
  readonly #database: Database.Database;
  readonly #leaseMs: number;
  readonly #now: () => number;
  readonly #owner = randomUUID();
  #closed = false;
  #initialized: boolean;
  #unresolvedTakeover = false;

  private constructor(
    database: Database.Database,
    metadata: KernelStoreMetadataV1,
    initialized: boolean,
    leaseMs: number,
    now: () => number,
  ) {
    this.#database = database;
    this.metadata = Object.freeze(metadata);
    this.#initialized = initialized;
    this.#leaseMs = leaseMs;
    this.#now = now;
    this.heartbeatIntervalMs = Math.max(1, Math.floor(leaseMs / 3));
  }

  static create(
    directory: string,
    runId: string,
    taskDigest: Sha256Digest,
    options: SqliteKernelStoreCreateOptions = {},
  ): SqliteKernelStore {
    const safeId = safeRunId(runId);
    const leaseMs = strictLeaseMs(options.leaseMs);
    const now = options.now ?? Date.now;
    const initialNow = strictTime(now());
    leaseExpiry(initialNow, leaseMs);
    const createdAt = options.createdAt ?? (options.clock ?? (() => new Date().toISOString()))();
    if (!DIGEST_PATTERN.test(taskDigest) || !canonicalTimestamp(createdAt)) {
      throw new KernelError("invalid_store", "Store metadata is invalid.");
    }

    const target = resolve(directory);
    const parent = dirname(target);
    mkdirSync(parent, { recursive: true });
    const staging = mkdtempSync(join(parent, `.${basename(target)}.init-`));
    let claimed = false;
    let published = false;
    try {
      const stagingDatabase = connect(join(staging, DB_FILE), false);
      try {
        stagingDatabase.transaction(() => {
          stagingDatabase.exec(SCHEMA);
          stagingDatabase.prepare("INSERT INTO metadata VALUES(1,?,?,?,?,0)").run(
            KERNEL_STORE_VERSION,
            safeId,
            taskDigest,
            createdAt,
          );
        }).immediate();
        chmodSync(join(staging, DB_FILE), 0o600);
      } finally {
        stagingDatabase.close();
      }
      if (readdirSync(staging).some((entry) => entry !== DB_FILE)) {
        throw new KernelError("invalid_store", "Staged kernel store has unexpected sidecar files.");
      }
      try {
        mkdirSync(target, { mode: 0o700 });
        claimed = true;
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code === "EEXIST") {
          throw new KernelError("target_exists", `Kernel store target exists: ${target}`);
        }
        throw error;
      }
      try {
        linkSync(join(staging, DB_FILE), join(target, DB_FILE));
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code === "EEXIST") {
          throw new KernelError("target_exists", `Kernel store target was occupied: ${target}`);
        }
        throw error;
      }
      published = true;
      const database = connect(join(target, DB_FILE), true);
      try {
        const store = new SqliteKernelStore(
          database,
          { schemaVersion: KERNEL_STORE_VERSION, runId: safeId, taskDigest, createdAt },
          false,
          leaseMs,
          now,
        );
        store.#acquireLease(initialNow);
        return store;
      } catch (error) {
        database.close();
        throw error;
      }
    } finally {
      rmSync(staging, { recursive: true, force: true });
      if (claimed && !published) {
        try {
          rmdirSync(target);
        } catch {
          // Preserve a non-empty target: another process may have written into our claim.
        }
      }
    }
  }

  static open(
    directory: string,
    runId: string,
    options: SqliteKernelStoreOptions = {},
  ): SqliteKernelStore {
    const leaseMs = strictLeaseMs(options.leaseMs);
    const now = options.now ?? Date.now;
    const initialNow = strictTime(now());
    leaseExpiry(initialNow, leaseMs);
    const database = connect(join(resolve(directory), DB_FILE), true);
    try {
      const { metadata, initialized } = readMetadata(database, safeRunId(runId));
      const store = new SqliteKernelStore(database, metadata, initialized, leaseMs, now);
      store.#acquireLease(initialNow);
      return store;
    } catch (error) {
      database.close();
      throw error;
    }
  }

  get initialized(): boolean {
    return this.#initialized;
  }

  get tookOverExpiredLease(): boolean {
    return this.#unresolvedTakeover;
  }

  loadEvents(): KernelEventV1[] {
    this.#assertOpen();
    let rows: EventRow[];
    try {
      rows = this.#database.prepare("SELECT * FROM events ORDER BY sequence").all() as EventRow[];
    } catch (error) {
      throw new KernelError("corrupt_journal", "Kernel journal is unavailable.", { cause: error });
    }
    const events: KernelEventV1[] = [];
    let previous: Sha256Digest | null = null;
    for (let index = 0; index < rows.length; index += 1) {
      const row = rows[index]!;
      const value = parseCanonical(row.event_json);
      if (
        row.sequence !== index ||
        value === undefined ||
        row.event_hash !== value.eventHash ||
        !validEvent(value, this.metadata.runId, index, previous)
      ) {
        throw new KernelError("corrupt_journal", `Journal event ${index} is invalid.`);
      }
      const event = value as unknown as KernelEventV1;
      events.push(event);
      previous = event.eventHash;
    }
    return events;
  }

  commit(
    expected: EventHead,
    event: KernelEventV1,
    nextState: KernelStateSnapshotV1,
  ): EventHead {
    this.#assertOpen();
    const eventJson = encode(event, "Event");
    const value = parseCanonical(eventJson);
    if (
      value === undefined ||
      !this.#validHead(expected) ||
      !validEvent(value, this.metadata.runId, expected.sequence + 1, expected.eventHash) ||
      nextState.schemaVersion !== KERNEL_STATE_VERSION ||
      nextState.runId !== this.metadata.runId ||
      nextState.taskDigest !== this.metadata.taskDigest
    ) {
      throw new KernelError("invalid_state", "Event, expected head, or next state is invalid.");
    }
    const head = { sequence: event.sequence, eventHash: event.eventHash } as const;
    const checkpoint = checkpointFor(this.metadata, head, nextState);
    const checkpointJson = canonicalJson(checkpoint);
    this.#database.transaction(() => {
      this.#renew();
      this.#assertHead(expected);
      this.#database.prepare("INSERT INTO events VALUES(?,?,?)").run(
        event.sequence,
        event.eventHash,
        eventJson,
      );
      this.#database.prepare(UPSERT_CHECKPOINT).run(
        event.sequence,
        event.eventHash,
        checkpoint.checkpointHash,
        checkpointJson,
      );
      if (event.sequence === 0) {
        this.#database.prepare("UPDATE metadata SET initialized=1 WHERE singleton=1").run();
      }
    }).immediate();
    if (event.sequence === 0) this.#initialized = true;
    return head;
  }

  syncCheckpoint(expected: EventHead, state: KernelStateSnapshotV1): void {
    this.#assertOpen();
    if (expected.sequence < 0 || expected.eventHash === null || !this.#validHead(expected)) {
      throw new KernelError("invalid_state", "Cannot checkpoint an empty journal.");
    }
    const checkpoint = checkpointFor(this.metadata, expected as CommittedHead, state);
    const checkpointJson = canonicalJson(checkpoint);
    const schema = this.#database.prepare(
      "SELECT type,sql FROM sqlite_master WHERE name='checkpoint'",
    ).get() as { type: string; sql: string | null } | undefined;
    if (schema?.type === "table" && schema.sql === CHECKPOINT_SCHEMA) {
      const current = this.#database.prepare(
        "SELECT sequence,event_hash,checkpoint_hash,checkpoint_json FROM checkpoint WHERE singleton=1",
      ).get() as {
        sequence: number;
        event_hash: string;
        checkpoint_hash: string;
        checkpoint_json: string;
      } | undefined;
      if (
        current?.sequence === expected.sequence &&
        current.event_hash === expected.eventHash &&
        current.checkpoint_hash === checkpoint.checkpointHash &&
        current.checkpoint_json === checkpointJson
      ) {
        return;
      }
    }
    this.#database.transaction(() => {
      this.#renew();
      this.#assertHead(expected);
      if (schema?.type !== "table" || schema.sql !== CHECKPOINT_SCHEMA) {
        if (schema !== undefined) {
          if (!["index", "table", "trigger", "view"].includes(schema.type)) {
            throw new KernelError("invalid_store", "Checkpoint schema object is invalid.");
          }
          this.#database.exec(`DROP ${schema.type.toUpperCase()} checkpoint`);
        }
        this.#database.exec(CHECKPOINT_SCHEMA);
      }
      this.#database.prepare(UPSERT_CHECKPOINT).run(
        expected.sequence,
        expected.eventHash,
        checkpoint.checkpointHash,
        checkpointJson,
      );
    }).immediate();
  }

  renewLease(): void {
    this.#assertOpen();
    this.#renew();
  }

  resolveTakeover(): void {
    this.#unresolvedTakeover = false;
  }

  close(): void {
    if (this.#closed) return;
    try {
      if (this.#unresolvedTakeover) {
        this.#database.prepare(
          "UPDATE lease SET expires_at_ms=0 WHERE singleton=1 AND owner=?",
        ).run(this.#owner);
      } else {
        this.#database.prepare("DELETE FROM lease WHERE singleton=1 AND owner=?").run(this.#owner);
      }
    } finally {
      this.#closed = true;
      this.#database.close();
    }
  }

  #acquireLease(now: number): void {
    this.#database.transaction(() => {
      const row = this.#database.prepare(
        "SELECT owner,expires_at_ms FROM lease WHERE singleton=1",
      ).get() as { owner: string; expires_at_ms: number } | undefined;
      if (row !== undefined && row.expires_at_ms > now) {
        throw new KernelError("lease_held", "Another kernel writer holds the lease.");
      }
      this.#unresolvedTakeover = row !== undefined;
      this.#database.prepare(
        "INSERT INTO lease VALUES(1,?,?) ON CONFLICT(singleton) DO UPDATE SET owner=excluded.owner,expires_at_ms=excluded.expires_at_ms",
      ).run(this.#owner, this.#expiry(now));
    }).immediate();
  }

  #renew(): void {
    const now = strictTime(this.#now());
    const result = this.#database.prepare(
      "UPDATE lease SET expires_at_ms=? WHERE singleton=1 AND owner=?",
    ).run(this.#expiry(now), this.#owner);
    if (result.changes !== 1) {
      throw new KernelError("lease_lost", "Kernel writer lease was lost.");
    }
  }

  #assertHead(expected: EventHead): void {
    const actual = this.#databaseHead();
    if (actual.sequence !== expected.sequence || actual.eventHash !== expected.eventHash) {
      throw new KernelError("stale_head", "Kernel journal head changed.");
    }
  }

  #databaseHead(): EventHead {
    const row = this.#database.prepare(
      "SELECT sequence,event_hash FROM events ORDER BY sequence DESC LIMIT 1",
    ).get() as { sequence: number; event_hash: Sha256Digest } | undefined;
    return row === undefined
      ? { sequence: -1, eventHash: null }
      : { sequence: row.sequence, eventHash: row.event_hash };
  }

  #validHead(head: EventHead): boolean {
    return (
      Number.isSafeInteger(head.sequence) &&
      head.sequence >= -1 &&
      (head.sequence === -1
        ? head.eventHash === null
        : typeof head.eventHash === "string" && DIGEST_PATTERN.test(head.eventHash))
    );
  }

  #expiry(now: number): number {
    return leaseExpiry(now, this.#leaseMs);
  }

  #assertOpen(): void {
    if (this.#closed) throw new KernelError("invalid_store", "Kernel store is closed.");
  }
}
