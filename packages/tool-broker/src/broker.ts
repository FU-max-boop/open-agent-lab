import { createHash } from "node:crypto";

import {
  canonicalJson,
  type JsonObject,
  type JsonValue,
  type Sha256Digest,
} from "@open-agent-lab/contracts";

import { ToolBrokerError } from "./errors.js";
import {
  TOOL_EFFECT_CLASSES,
  type PersistedToolInvocation,
  type ToolDefinition,
  type ToolExecutionContext,
  type ToolExecutionResult,
  type ToolInvocationRequest,
  type ToolReconciliation,
} from "./types.js";

const EFFECTS = new Set<string>(TOOL_EFFECT_CLASSES);
const DIGEST = /^sha256:[a-f0-9]{64}$/u;

function safeString(value: unknown, label: string): string {
  if (
    typeof value !== "string" ||
    value.trim() === "" ||
    value.length > 256 ||
    /[\u0000-\u001f\u007f]/u.test(value)
  ) {
    throw new ToolBrokerError("invalid_invocation", `${label} must be a non-empty safe string.`);
  }
  return value;
}

function deepFreeze<T>(value: T): T {
  if (typeof value === "object" && value !== null) {
    for (const child of Object.values(value)) deepFreeze(child);
    Object.freeze(value);
  }
  return value;
}

function strictJson<T>(value: T, code: "invalid_invocation" | "invalid_result", label: string): T {
  try {
    return deepFreeze(JSON.parse(canonicalJson(value)) as T);
  } catch (error) {
    throw new ToolBrokerError(code, `${label} must contain only strict JSON.`, { cause: error });
  }
}

function digest(value: unknown): Sha256Digest {
  return `sha256:${createHash("sha256").update(canonicalJson(value)).digest("hex")}`;
}

function validDigest(value: unknown, label: string): Sha256Digest {
  if (typeof value !== "string" || !DIGEST.test(value)) {
    throw new ToolBrokerError("invalid_invocation", `${label} must be a SHA-256 digest.`);
  }
  return value as Sha256Digest;
}

function assertActive(context: ToolExecutionContext): void {
  if (context.signal?.aborted === true) {
    throw new ToolBrokerError("aborted", "Tool operation was aborted.");
  }
}

function actionFingerprint(
  invocation: Pick<
    PersistedToolInvocation,
    "toolName" | "contractDigest" | "effect" | "arguments" | "stateFingerprint"
  >,
): Sha256Digest {
  return digest({
    arguments: invocation.arguments,
    contractDigest: invocation.contractDigest,
    effect: invocation.effect,
    stateFingerprint: invocation.stateFingerprint,
    toolName: invocation.toolName,
  });
}

function normalizeResult(result: ToolExecutionResult): ToolExecutionResult {
  if (typeof result !== "object" || result === null || !("output" in result)) {
    throw new ToolBrokerError("invalid_result", "Tool result must contain an output value.");
  }
  const metadata = result.metadata === undefined
    ? undefined
    : strictJson(result.metadata, "invalid_result", "Tool metadata");
  if (metadata !== undefined && (typeof metadata !== "object" || metadata === null || Array.isArray(metadata))) {
    throw new ToolBrokerError("invalid_result", "Tool metadata must be a JSON object.");
  }
  return Object.freeze({
    output: strictJson(result.output, "invalid_result", "Tool output"),
    ...(metadata === undefined ? {} : { metadata: metadata as JsonObject }),
  });
}

export class ToolBroker {
  readonly #definitions = new Map<string, Readonly<ToolDefinition>>();

  constructor(definitions: readonly ToolDefinition[]) {
    for (const definition of definitions) {
      let name: string;
      let contractDigest: Sha256Digest;
      try {
        name = safeString(definition.name, "Tool name");
        contractDigest = validDigest(definition.contractDigest, "Tool contract digest");
      } catch (error) {
        throw new ToolBrokerError("invalid_definition", "Tool identity is invalid.", { cause: error });
      }
      if (
        !EFFECTS.has(definition.effect) ||
        typeof definition.stateFingerprint !== "function" ||
        typeof definition.execute !== "function" ||
        (definition.reconcile !== undefined && typeof definition.reconcile !== "function")
      ) {
        throw new ToolBrokerError("invalid_definition", `Tool '${name}' has an invalid contract.`);
      }
      if (this.#definitions.has(name)) {
        throw new ToolBrokerError("duplicate_tool", `Tool '${name}' is already registered.`);
      }
      this.#definitions.set(name, Object.freeze({ ...definition, name, contractDigest }));
    }
  }

  async prepare(
    request: ToolInvocationRequest,
    context: ToolExecutionContext,
  ): Promise<PersistedToolInvocation> {
    assertActive(context);
    const invocationId = safeString(request.invocationId, "Invocation ID");
    const toolName = safeString(request.toolName, "Tool name");
    const definition = this.#require(toolName);
    const argumentsValue = strictJson(request.arguments, "invalid_invocation", "Tool arguments");
    const stateFingerprint = validDigest(
      await definition.stateFingerprint(argumentsValue, context),
      "State fingerprint",
    );
    assertActive(context);
    const base = {
      invocationId,
      toolName,
      arguments: argumentsValue,
      contractDigest: definition.contractDigest,
      effect: definition.effect,
      stateFingerprint,
    };
    const fingerprint = actionFingerprint(base);
    return Object.freeze({
      ...base,
      actionFingerprint: fingerprint,
      ...(definition.effect === "idempotent"
        ? { idempotencyKey: digest({ actionFingerprint: fingerprint, runId: context.runId }) }
        : {}),
    });
  }

  #restore(value: PersistedToolInvocation, context: ToolExecutionContext): PersistedToolInvocation {
    const invocation: PersistedToolInvocation = {
      invocationId: safeString(value.invocationId, "Invocation ID"),
      toolName: safeString(value.toolName, "Tool name"),
      arguments: strictJson(value.arguments, "invalid_invocation", "Tool arguments") as JsonValue,
      contractDigest: validDigest(value.contractDigest, "Tool contract digest"),
      effect: value.effect,
      stateFingerprint: validDigest(value.stateFingerprint, "State fingerprint"),
      actionFingerprint: validDigest(value.actionFingerprint, "Action fingerprint"),
      ...(value.idempotencyKey === undefined
        ? {}
        : { idempotencyKey: validDigest(value.idempotencyKey, "Idempotency key") }),
    };
    if (!EFFECTS.has(invocation.effect) || actionFingerprint(invocation) !== invocation.actionFingerprint) {
      throw new ToolBrokerError("invalid_invocation", "Persisted tool identity is invalid.");
    }
    const expectedKey = invocation.effect === "idempotent"
      ? digest({ actionFingerprint: invocation.actionFingerprint, runId: context.runId })
      : undefined;
    if (invocation.idempotencyKey !== expectedKey) {
      throw new ToolBrokerError("invalid_invocation", "Persisted idempotency key is invalid.");
    }
    const definition = this.#require(invocation.toolName);
    if (
      definition.contractDigest !== invocation.contractDigest ||
      definition.effect !== invocation.effect
    ) {
      throw new ToolBrokerError("contract_drift", `Tool '${invocation.toolName}' contract changed.`);
    }
    return Object.freeze(invocation);
  }

  async execute(
    value: PersistedToolInvocation,
    context: ToolExecutionContext,
  ): Promise<ToolExecutionResult> {
    const invocation = this.#restore(value, context);
    const definition = this.#require(invocation.toolName);
    const current = validDigest(
      await definition.stateFingerprint(invocation.arguments, context),
      "State fingerprint",
    );
    if (current !== invocation.stateFingerprint) {
      throw new ToolBrokerError("precondition_changed", "Tool state changed before execution.");
    }
    assertActive(context);
    const result = await definition.execute(invocation, context);
    assertActive(context);
    return normalizeResult(result);
  }

  async reconcile(
    value: PersistedToolInvocation,
    context: ToolExecutionContext,
  ): Promise<ToolReconciliation> {
    const invocation = this.#restore(value, context);
    assertActive(context);
    const reconcile = this.#require(invocation.toolName).reconcile;
    if (reconcile === undefined) {
      return { status: "unknown", reason: "Tool does not implement reconciliation." };
    }
    const outcome = await reconcile(invocation, context);
    assertActive(context);
    if (typeof outcome !== "object" || outcome === null || Array.isArray(outcome)) {
      throw new ToolBrokerError("invalid_result", "Reconciliation returned an invalid outcome.");
    }
    if (outcome.status === "applied") {
      return { status: "applied", result: normalizeResult(outcome.result) };
    }
    if (outcome.status === "not_applied") return outcome;
    if (outcome.status !== "unknown" || typeof outcome.reason !== "string" || outcome.reason.trim() === "") {
      throw new ToolBrokerError("invalid_result", "Reconciliation returned an invalid outcome.");
    }
    return { status: "unknown", reason: outcome.reason };
  }

  #require(name: string): Readonly<ToolDefinition> {
    const definition = this.#definitions.get(name);
    if (definition === undefined) {
      throw new ToolBrokerError("unknown_tool", `Tool '${name}' is not registered.`);
    }
    return definition;
  }
}
