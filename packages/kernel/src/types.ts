import type { JsonObject, JsonValue, Sha256Digest } from "@open-agent-lab/contracts";
import type {
  PersistedToolInvocation,
  ToolExecutionResult,
} from "@open-agent-lab/tool-broker";

export const KERNEL_STORE_VERSION = "open-agent-lab.kernel-store.v1" as const;
export const KERNEL_EVENT_VERSION = "open-agent-lab.kernel-event.v1" as const;
export const KERNEL_CHECKPOINT_VERSION = "open-agent-lab.kernel-checkpoint.v1" as const;
export const KERNEL_STATE_VERSION = "open-agent-lab.kernel-state.v1" as const;

export type RunLifecycle = "running" | "needs_review" | "succeeded" | "failed" | "cancelled";

export interface KernelStoreMetadataV1 {
  schemaVersion: typeof KERNEL_STORE_VERSION;
  runId: string;
  taskDigest: Sha256Digest;
  createdAt: string;
}

export interface KernelEventBodyV1 {
  schemaVersion: typeof KERNEL_EVENT_VERSION;
  runId: string;
  sequence: number;
  timestamp: string;
  type: string;
  data: JsonObject;
  previousEventHash: Sha256Digest | null;
}

export interface KernelEventV1 extends KernelEventBodyV1 {
  eventHash: Sha256Digest;
}

export interface CompletedInvocationV1 {
  invocation: PersistedToolInvocation;
  result: ToolExecutionResult;
}

export interface VerifierRecordV1 {
  runId: string;
  taskDigest: Sha256Digest;
  workspaceDigest: Sha256Digest;
  verifierId: string;
  verifierVersion: string;
  passed: boolean;
  details?: JsonValue;
}

export interface KernelStateSnapshotV1 {
  schemaVersion: typeof KERNEL_STATE_VERSION;
  runId: string;
  taskDigest: Sha256Digest;
  lifecycle: RunLifecycle;
  pending?: PersistedToolInvocation;
  review?: { invocationId: string; reason: string };
  invocationFingerprints: Record<string, Sha256Digest>;
  completed: CompletedInvocationV1[];
  verification?: VerifierRecordV1;
  terminalReason?: string;
}

export interface KernelCheckpointBodyV1 {
  schemaVersion: typeof KERNEL_CHECKPOINT_VERSION;
  runId: string;
  lastSequence: number;
  lastEventHash: Sha256Digest;
  state: KernelStateSnapshotV1;
}

export interface KernelCheckpointV1 extends KernelCheckpointBodyV1 {
  checkpointHash: Sha256Digest;
}

export interface EventHead {
  sequence: number;
  eventHash: Sha256Digest | null;
}

export type ReviewDecision =
  | {
      action: "confirmed_applied";
      operator: string;
      reason: string;
      result: ToolExecutionResult;
    }
  | {
      action: "confirmed_not_applied_then_retry";
      operator: string;
      reason: string;
    }
  | { action: "abort"; operator: string; reason: string };

export interface VerifierOutcome {
  workspaceDigest: Sha256Digest;
  passed: boolean;
  details?: JsonValue;
}

export interface RunVerifier {
  id: string;
  version: string;
  verify(input: Readonly<{
    runId: string;
    taskDigest: Sha256Digest;
    signal: AbortSignal;
  }>): Promise<VerifierOutcome>;
}

export interface KernelResumeResult {
  state: Readonly<KernelStateSnapshotV1>;
  action: "nothing_pending" | "replayed" | "reconciled" | "needs_review";
  result?: ToolExecutionResult;
}
