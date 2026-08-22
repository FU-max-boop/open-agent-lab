import { canonicalJson, type JsonObject, type JsonValue } from "@open-agent-lab/contracts";
import {
  TOOL_EFFECT_CLASSES,
  type PersistedToolInvocation,
  type ToolExecutionResult,
} from "@open-agent-lab/tool-broker";

import { DIGEST_PATTERN, kernelDigest } from "./digest.js";
import { KernelError } from "./errors.js";
import {
  KERNEL_STATE_VERSION,
  type KernelEventV1,
  type KernelStateSnapshotV1,
  type VerifierRecordV1,
} from "./types.js";

const EFFECTS = new Set<string>(TOOL_EFFECT_CLASSES);

function object(value: unknown, label: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new KernelError("corrupt_journal", `${label} must be an object.`);
  }
  return value as Record<string, unknown>;
}

function text(value: unknown, label: string): string {
  if (typeof value !== "string" || value.trim() === "") {
    throw new KernelError("corrupt_journal", `${label} must be a non-empty string.`);
  }
  return value;
}

function digest(value: unknown, label: string): `sha256:${string}` {
  const result = text(value, label);
  if (!DIGEST_PATTERN.test(result)) {
    throw new KernelError("corrupt_journal", `${label} must be a SHA-256 digest.`);
  }
  return result as `sha256:${string}`;
}

function json<T>(value: T, label: string): T {
  try {
    return JSON.parse(canonicalJson(value)) as T;
  } catch (error) {
    throw new KernelError("corrupt_journal", `${label} must be strict JSON.`, { cause: error });
  }
}

function boundFingerprint(state: KernelStateSnapshotV1, invocationId: string) {
  return Object.hasOwn(state.invocationFingerprints, invocationId)
    ? state.invocationFingerprints[invocationId]
    : undefined;
}

function invocation(value: unknown, runId: string): PersistedToolInvocation {
  const item = object(value, "Tool invocation");
  const effect = text(item.effect, "Tool effect");
  if (!EFFECTS.has(effect)) {
    throw new KernelError("corrupt_journal", "Tool effect is invalid.");
  }
  const parsed: PersistedToolInvocation = {
    invocationId: text(item.invocationId, "Invocation ID"),
    toolName: text(item.toolName, "Tool name"),
    arguments: json(item.arguments as JsonValue, "Tool arguments"),
    contractDigest: digest(item.contractDigest, "Tool contract digest"),
    effect: effect as PersistedToolInvocation["effect"],
    stateFingerprint: digest(item.stateFingerprint, "State fingerprint"),
    actionFingerprint: digest(item.actionFingerprint, "Action fingerprint"),
    ...(item.idempotencyKey === undefined
      ? {}
      : { idempotencyKey: digest(item.idempotencyKey, "Idempotency key") }),
  };
  if (
    parsed.actionFingerprint !== kernelDigest({
      arguments: parsed.arguments,
      contractDigest: parsed.contractDigest,
      effect: parsed.effect,
      stateFingerprint: parsed.stateFingerprint,
      toolName: parsed.toolName,
    }) ||
    (parsed.effect === "idempotent" && parsed.idempotencyKey !== kernelDigest({
      actionFingerprint: parsed.actionFingerprint,
      runId,
    })) ||
    (parsed.effect !== "idempotent" && parsed.idempotencyKey !== undefined)
  ) {
    throw new KernelError("corrupt_journal", "Persisted idempotency key is invalid.");
  }
  return parsed;
}

function result(value: unknown): ToolExecutionResult {
  const item = object(value, "Tool result");
  if (!("output" in item)) throw new KernelError("corrupt_journal", "Tool result has no output.");
  const metadata = item.metadata === undefined
    ? undefined
    : object(item.metadata, "Tool metadata");
  return {
    output: json(item.output as JsonValue, "Tool output"),
    ...(metadata === undefined ? {} : { metadata: json(metadata as JsonObject, "Tool metadata") }),
  };
}

function verifier(value: unknown, state: KernelStateSnapshotV1): VerifierRecordV1 {
  const item = object(value, "Verifier record");
  const record: VerifierRecordV1 = {
    runId: text(item.runId, "Verifier run ID"),
    taskDigest: digest(item.taskDigest, "Verifier task digest"),
    workspaceDigest: digest(item.workspaceDigest, "Verifier workspace digest"),
    verifierId: text(item.verifierId, "Verifier ID"),
    verifierVersion: text(item.verifierVersion, "Verifier version"),
    passed: item.passed as boolean,
    ...(item.details === undefined ? {} : { details: json(item.details as JsonValue, "Details") }),
  };
  if (
    typeof record.passed !== "boolean" ||
    record.runId !== state.runId ||
    record.taskDigest !== state.taskDigest
  ) {
    throw new KernelError("corrupt_journal", "Verifier record is not bound to this run.");
  }
  return record;
}

export function initialState(runId: string, taskDigest: `sha256:${string}`): KernelStateSnapshotV1 {
  return {
    schemaVersion: KERNEL_STATE_VERSION,
    runId,
    taskDigest,
    lifecycle: "running",
    invocationFingerprints: {},
    completed: [],
  };
}

function requireState(state: KernelStateSnapshotV1, lifecycle: string, pending = false): void {
  if (state.lifecycle !== lifecycle || (pending && state.pending === undefined)) {
    throw new KernelError("invalid_state", `Event is invalid while run is '${state.lifecycle}'.`);
  }
}

export function reduceEvent(
  state: KernelStateSnapshotV1,
  event: Pick<KernelEventV1, "sequence" | "type" | "data">,
): KernelStateSnapshotV1 {
  const data = object(event.data, `${event.type} data`);
  let next: KernelStateSnapshotV1;
  switch (event.type) {
    case "run.created":
      if (
        event.sequence !== 0 ||
        digest(data.taskDigest, "Task digest") !== state.taskDigest ||
        state.completed.length !== 0
      ) {
        throw new KernelError("invalid_state", "run.created must be the first bound event.");
      }
      next = state;
      break;
    case "tool.intent": {
      requireState(state, "running");
      if (state.pending !== undefined) throw new KernelError("invalid_state", "A tool is already pending.");
      const pending = invocation(data.invocation, state.runId);
      const existing = boundFingerprint(state, pending.invocationId);
      if (existing !== undefined) {
        throw new KernelError("invocation_conflict", "Invocation ID is already bound.");
      }
      next = {
        ...state,
        pending,
        invocationFingerprints: {
          ...state.invocationFingerprints,
          [pending.invocationId]: pending.actionFingerprint,
        },
      };
      break;
    }
    case "tool.completed": {
      requireState(state, "running", true);
      const invocationId = text(data.invocationId, "Invocation ID");
      if (state.pending?.invocationId !== invocationId) {
        throw new KernelError("invalid_state", "Completion does not match the pending tool.");
      }
      const { pending, ...rest } = state;
      next = {
        ...rest,
        completed: [...state.completed, { invocation: state.pending, result: result(data.result) }],
      };
      break;
    }
    case "tool.reused": {
      requireState(state, "running");
      if (state.pending !== undefined) throw new KernelError("invalid_state", "Cannot reuse while a tool is pending.");
      const reused = invocation(data.invocation, state.runId);
      const existing = boundFingerprint(state, reused.invocationId);
      const source = state.completed.find(
        (entry) => entry.invocation.actionFingerprint === reused.actionFingerprint,
      );
      if (
        source === undefined ||
        (existing !== undefined && existing !== reused.actionFingerprint) ||
        (existing === undefined && reused.effect !== "read_only" && reused.effect !== "idempotent")
      ) {
        throw new KernelError("invocation_conflict", "Reused invocation is not a safe completed action.");
      }
      next = {
        ...state,
        invocationFingerprints: {
          ...state.invocationFingerprints,
          [reused.invocationId]: reused.actionFingerprint,
        },
      };
      break;
    }
    case "review.required":
      requireState(state, "running", true);
      next = {
        ...state,
        lifecycle: "needs_review",
        review: {
          invocationId: state.pending?.invocationId ?? "",
          reason: text(data.reason, "Review reason"),
        },
      };
      break;
    case "review.confirmed_applied": {
      requireState(state, "needs_review", true);
      if (text(data.invocationId, "Invocation ID") !== state.pending?.invocationId) {
        throw new KernelError("invalid_state", "Review decision does not match the pending tool.");
      }
      text(data.operator, "Operator");
      text(data.reason, "Review reason");
      const { pending, review, ...rest } = state;
      next = {
        ...rest,
        lifecycle: "running",
        completed: [...state.completed, { invocation: state.pending, result: result(data.result) }],
      };
      break;
    }
    case "review.retry_authorized": {
      requireState(state, "needs_review", true);
      if (text(data.invocationId, "Invocation ID") !== state.pending?.invocationId) {
        throw new KernelError("invalid_state", "Review decision does not match the pending tool.");
      }
      text(data.operator, "Operator");
      text(data.reason, "Review reason");
      const { review, ...rest } = state;
      next = { ...rest, lifecycle: "running" };
      break;
    }
    case "review.aborted": {
      requireState(state, "needs_review", true);
      if (text(data.invocationId, "Invocation ID") !== state.pending?.invocationId) {
        throw new KernelError("invalid_state", "Review decision does not match the pending tool.");
      }
      text(data.operator, "Operator");
      const { pending, review, ...rest } = state;
      next = {
        ...rest,
        lifecycle: "failed",
        terminalReason: text(data.reason, "Abort reason"),
      };
      break;
    }
    case "verification.completed": {
      requireState(state, "running");
      if (state.pending !== undefined) throw new KernelError("invalid_state", "Cannot verify a pending tool.");
      const record = verifier(data.record, state);
      next = {
        ...state,
        lifecycle: record.passed ? "succeeded" : "failed",
        verification: record,
        ...(record.passed ? {} : { terminalReason: "Independent verifier rejected the run." }),
      };
      break;
    }
    case "run.cancelled":
      if (
        !(
          state.lifecycle === "running" &&
          (state.pending === undefined || state.pending.effect === "read_only")
        ) &&
        !(state.lifecycle === "needs_review" && state.pending?.effect === "read_only")
      ) {
        throw new KernelError("invalid_state", "Run has an effect which cannot be discarded.");
      }
      {
        const { pending, review, ...rest } = state;
        next = {
          ...rest,
          lifecycle: "cancelled",
          terminalReason: text(data.reason, "Cancellation reason"),
        };
      }
      break;
    default:
      throw new KernelError("corrupt_journal", `Unknown kernel event '${event.type}'.`);
  }
  assertState(next);
  return next;
}

export function assertState(state: KernelStateSnapshotV1): void {
  if (
    typeof state.invocationFingerprints !== "object" ||
    state.invocationFingerprints === null ||
    Array.isArray(state.invocationFingerprints)
  ) {
    throw new KernelError("invalid_state", "Invocation fingerprint index is invalid.");
  }
  for (const value of Object.values(state.invocationFingerprints)) {
    if (typeof value !== "string" || !DIGEST_PATTERN.test(value)) {
      throw new KernelError("invalid_state", "Invocation fingerprint index is invalid.");
    }
  }
  if (state.lifecycle === "needs_review") {
    if (state.pending === undefined || state.review?.invocationId !== state.pending.invocationId) {
      throw new KernelError("invalid_state", "needs_review must identify its pending tool.");
    }
  } else if (state.review !== undefined) {
    throw new KernelError("invalid_state", "Review data is only valid in needs_review.");
  }
  if (["succeeded", "failed", "cancelled"].includes(state.lifecycle) && state.pending !== undefined) {
    throw new KernelError("invalid_state", "Terminal state cannot retain a pending tool.");
  }
  if (state.lifecycle === "succeeded" && state.verification?.passed !== true) {
    throw new KernelError("invalid_state", "Success requires a passing verifier record.");
  }
  if (
    state.pending !== undefined &&
    boundFingerprint(state, state.pending.invocationId) !== state.pending.actionFingerprint
  ) {
    throw new KernelError("invalid_state", "Pending invocation is not bound in the fingerprint index.");
  }
}
