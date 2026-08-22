import type { JsonObject, JsonValue, Sha256Digest } from "@open-agent-lab/contracts";

export const TOOL_EFFECT_CLASSES = [
  "read_only",
  "idempotent",
  "workspace_mutation",
  "external_non_idempotent",
] as const;

export type ToolEffectClass = (typeof TOOL_EFFECT_CLASSES)[number];

export interface ToolInvocationRequest {
  invocationId: string;
  toolName: string;
  arguments: JsonValue;
}

export interface PersistedToolInvocation extends ToolInvocationRequest {
  /** Digest of the normalized schema and recovery contract. */
  contractDigest: Sha256Digest;
  effect: ToolEffectClass;
  stateFingerprint: Sha256Digest;
  actionFingerprint: Sha256Digest;
  /** Runtime-derived and stable; models and callers cannot choose it. */
  idempotencyKey?: Sha256Digest;
}

export interface ToolExecutionContext {
  runId: string;
  signal?: AbortSignal;
}

export interface ToolExecutionResult {
  output: JsonValue;
  /** Trace-safe metadata. Secrets must never be placed here. */
  metadata?: JsonObject;
}

export type ToolReconciliation =
  | { status: "applied"; result: ToolExecutionResult }
  | { status: "not_applied" }
  | { status: "unknown"; reason: string };

export interface ToolDefinition {
  name: string;
  /** Caller-supplied digest; change it whenever schema or recovery semantics change. */
  contractDigest: Sha256Digest;
  effect: ToolEffectClass;
  /** Used for action identity and early drift rejection; this probe is not an atomic lock. */
  stateFingerprint(
    argumentsValue: JsonValue,
    context: ToolExecutionContext,
  ): Promise<Sha256Digest>;
  /**
   * Mutation tools must atomically compare invocation.stateFingerprint at the
   * effect boundary (for example under a workspace lock or remote CAS).
   */
  execute(
    invocation: Readonly<PersistedToolInvocation>,
    context: ToolExecutionContext,
  ): Promise<ToolExecutionResult>;
  reconcile?(
    invocation: Readonly<PersistedToolInvocation>,
    context: ToolExecutionContext,
  ): Promise<ToolReconciliation>;
}
