/** Values which have a lossless representation in JSON. */
export type JsonPrimitive = null | boolean | number | string;

export type JsonValue = JsonPrimitive | JsonObject | JsonValue[];

export interface JsonObject {
  [key: string]: JsonValue;
}

/**
 * Schema identifiers are deliberately part of every persisted record. A reader
 * must never guess the version from the shape of an object.
 */
export const RUN_SPEC_VERSION = "open-agent-lab.run-spec.v1" as const;
export const RUN_EVENT_VERSION = "open-agent-lab.run-event.v1" as const;
export const EVIDENCE_MANIFEST_VERSION =
  "open-agent-lab.evidence-manifest.v1" as const;

export type Sha256Digest = `sha256:${string}`;

export interface BenchmarkRefV1 {
  name: string;
  version?: string;
  split?: string;
  taskId?: string;
}

export interface RunTaskV1 {
  id: string;
  instruction: string;
  benchmark?: BenchmarkRefV1;
}

export interface AgentRefV1 {
  name: string;
  version: string;
  revision?: string;
}

export interface ModelRefV1 {
  provider: string;
  name: string;
  /** An endpoint label, never an API key or other secret. */
  endpoint?: string;
  parameters?: JsonObject;
}

export interface RunLimitsV1 {
  maxSteps: number;
  wallTimeMs: number;
  maxInputTokens?: number;
  maxOutputTokens?: number;
  maxCostUsd?: number;
}

export interface RunSpecV1 {
  schemaVersion: typeof RUN_SPEC_VERSION;
  runId: string;
  createdAt: string;
  task: RunTaskV1;
  agent: AgentRefV1;
  model: ModelRefV1;
  limits: RunLimitsV1;
  metadata?: JsonObject;
}

export interface RunEventV1 {
  schemaVersion: typeof RUN_EVENT_VERSION;
  runId: string;
  sequence: number;
  timestamp: string;
  /** Namespaced event name, for example `tool.started` or `run.completed`. */
  type: string;
  data: JsonObject;
}

/** Alias for consumers which use the shorter protocol name. */
export type EventV1 = RunEventV1;

export interface EvidenceFileV1 {
  /** Portable, bundle-root-relative POSIX path. */
  path: string;
  size: number;
  sha256: Sha256Digest;
  mediaType: string;
  role?: string;
}

export interface EvidenceManifestBodyV1 {
  schemaVersion: typeof EVIDENCE_MANIFEST_VERSION;
  runId: string;
  createdAt: string;
  files: EvidenceFileV1[];
  metadata?: JsonObject;
}

export interface EvidenceManifestV1 extends EvidenceManifestBodyV1 {
  /** SHA-256 of the canonical JSON encoding of the manifest without this key. */
  manifestId: Sha256Digest;
}

export type RunSpec = RunSpecV1;
export type RunEvent = RunEventV1;
export type Event = RunEventV1;
export type EvidenceManifest = EvidenceManifestV1;
