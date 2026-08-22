import { ModelContractError } from "./errors.js";
import type {
  CompletedToolCall,
  JsonValue,
  ModelErrorEvent,
  ModelResponseInfo,
  ModelStreamEvent,
  ModelStreamResult,
  ModelUsage,
  ToolCallCompleteEvent,
  ToolCallDeltaEvent,
} from "./types.js";

interface PartialToolCall {
  callId?: string;
  name?: string;
  argumentsJson: string;
  completed: boolean;
}

function isNonNegativeSafeInteger(value: number): boolean {
  return Number.isSafeInteger(value) && value >= 0;
}

function validateUsage(usage: ModelUsage): void {
  const counters = [
    ["inputTokens", usage.inputTokens],
    ["outputTokens", usage.outputTokens],
    ["totalTokens", usage.totalTokens],
    ["cachedInputTokens", usage.cachedInputTokens],
    ["reasoningTokens", usage.reasoningTokens],
  ] as const;

  for (const [name, value] of counters) {
    if (value !== undefined && !isNonNegativeSafeInteger(value)) {
      throw new ModelContractError(
        "invalid_stream",
        `Usage counter '${name}' must be a non-negative safe integer.`,
      );
    }
  }
  if (usage.totalTokens !== usage.inputTokens + usage.outputTokens) {
    throw new ModelContractError(
      "invalid_stream",
      "Usage totalTokens must equal inputTokens + outputTokens.",
    );
  }
  if (
    usage.cachedInputTokens !== undefined &&
    usage.cachedInputTokens > usage.inputTokens
  ) {
    throw new ModelContractError(
      "invalid_stream",
      "Usage cachedInputTokens cannot exceed inputTokens.",
    );
  }
  if (
    usage.reasoningTokens !== undefined &&
    usage.reasoningTokens > usage.outputTokens
  ) {
    throw new ModelContractError(
      "invalid_stream",
      "Usage reasoningTokens cannot exceed outputTokens.",
    );
  }
}

function requireToolIndex(index: number): void {
  if (!Number.isSafeInteger(index) || index < 0) {
    throw new ModelContractError(
      "invalid_stream",
      "Tool-call index must be a non-negative safe integer.",
    );
  }
}

function validateToolDelta(event: ToolCallDeltaEvent): void {
  requireToolIndex(event.index);
  if (
    (event.callId === undefined || event.callId === "") &&
    (event.name === undefined || event.name === "") &&
    (event.argumentsDelta === undefined || event.argumentsDelta === "")
  ) {
    throw new ModelContractError(
      "invalid_stream",
      "A tool_call_delta must carry a callId, name, or argumentsDelta.",
    );
  }
}

function setStableField(
  partial: PartialToolCall,
  field: "callId" | "name",
  incoming: string | undefined,
  index: number,
): void {
  if (incoming === undefined) return;
  if (incoming === "") {
    throw new ModelContractError(
      "invalid_stream",
      `Tool call ${index} has an empty ${field}.`,
    );
  }
  const previous = partial[field];
  if (previous !== undefined && previous !== incoming) {
    throw new ModelContractError(
      "invalid_stream",
      `Tool call ${index} changed ${field} during streaming.`,
    );
  }
  partial[field] = incoming;
}

function jsonValuesEqual(left: JsonValue, right: JsonValue): boolean {
  if (left === right) return true;
  if (Array.isArray(left) || Array.isArray(right)) {
    if (!Array.isArray(left) || !Array.isArray(right)) return false;
    return (
      left.length === right.length &&
      left.every((value, index) => jsonValuesEqual(value, right[index]!))
    );
  }
  if (
    typeof left === "object" &&
    left !== null &&
    typeof right === "object" &&
    right !== null
  ) {
    const leftKeys = Object.keys(left).sort();
    const rightKeys = Object.keys(right).sort();
    return (
      leftKeys.length === rightKeys.length &&
      leftKeys.every(
        (key, index) =>
          key === rightKeys[index] && jsonValuesEqual(left[key]!, right[key]!),
      )
    );
  }
  return false;
}

function completeToolCall(
  event: ToolCallCompleteEvent,
  partials: Map<number, PartialToolCall>,
): CompletedToolCall {
  requireToolIndex(event.index);
  if (event.callId === "" || event.name === "") {
    throw new ModelContractError(
      "invalid_stream",
      `Completed tool call ${event.index} requires a callId and name.`,
    );
  }

  const partial = partials.get(event.index);
  if (partial?.completed === true) {
    throw new ModelContractError(
      "invalid_stream",
      `Tool call ${event.index} completed more than once.`,
    );
  }
  if (partial !== undefined) {
    setStableField(partial, "callId", event.callId, event.index);
    setStableField(partial, "name", event.name, event.index);
    if (partial.argumentsJson !== "") {
      let parsed: unknown;
      try {
        parsed = JSON.parse(partial.argumentsJson) as unknown;
      } catch {
        throw new ModelContractError(
          "invalid_stream",
          `Tool call ${event.index} emitted invalid JSON argument fragments.`,
        );
      }
      if (!jsonValuesEqual(parsed as JsonValue, event.arguments)) {
        throw new ModelContractError(
          "invalid_stream",
          `Tool call ${event.index} complete arguments do not match its deltas.`,
        );
      }
    }
    partial.completed = true;
  } else {
    partials.set(event.index, {
      callId: event.callId,
      name: event.name,
      argumentsJson: "",
      completed: true,
    });
  }

  return Object.freeze({
    index: event.index,
    callId: event.callId,
    name: event.name,
    arguments: event.arguments,
  });
}

function validateError(event: ModelErrorEvent): void {
  if (
    typeof event.code !== "string" ||
    typeof event.message !== "string" ||
    event.code.trim() === "" ||
    event.message.trim() === ""
  ) {
    throw new ModelContractError(
      "invalid_stream",
      "An error event requires a non-empty code and message.",
    );
  }
}

const RESPONSE_INFO_FIELDS = [
  "responseId",
  "providerRequestId",
  "model",
  "systemFingerprint",
] as const;

function normalizeResponseInfo(value: unknown): Readonly<ModelResponseInfo> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new ModelContractError(
      "invalid_stream",
      "response_info must be an object.",
    );
  }
  const info = value as Record<string, unknown>;
  if (
    Object.keys(info).some(
      (key) =>
        !RESPONSE_INFO_FIELDS.includes(
          key as (typeof RESPONSE_INFO_FIELDS)[number],
        ),
    )
  ) {
    throw new ModelContractError(
      "invalid_stream",
      "response_info contains an unsupported identity field.",
    );
  }
  const fields = RESPONSE_INFO_FIELDS.map((key) => info[key]);
  if (fields.every((value) => value === undefined)) {
    throw new ModelContractError(
      "invalid_stream",
      "response_info must contain at least one identity field.",
    );
  }
  if (
    fields.some(
      (value) =>
        value !== undefined &&
        (typeof value !== "string" || value.trim() === ""),
    )
  ) {
    throw new ModelContractError(
      "invalid_stream",
      "response_info identity fields must be non-empty strings.",
    );
  }
  return Object.freeze({
    ...(info.responseId === undefined
      ? {}
      : { responseId: info.responseId as string }),
    ...(info.providerRequestId === undefined
      ? {}
      : { providerRequestId: info.providerRequestId as string }),
    ...(info.model === undefined ? {} : { model: info.model as string }),
    ...(info.systemFingerprint === undefined
      ? {}
      : { systemFingerprint: info.systemFingerprint as string }),
  });
}

/**
 * Consume and validate a normalized stream. This is shared by production
 * orchestration and adapter conformance tests, so malformed adapters fail at a
 * single deterministic boundary.
 */
export async function collectModelStream(
  stream: AsyncIterable<ModelStreamEvent>,
): Promise<ModelStreamResult> {
  let text = "";
  let reasoning = "";
  let responseInfo: Readonly<ModelResponseInfo> | undefined;
  let usage: ModelUsage | undefined;
  let finish: ModelStreamResult["finish"];
  let error: ModelStreamResult["error"];
  let terminal = false;
  const partials = new Map<number, PartialToolCall>();
  const toolCalls: CompletedToolCall[] = [];

  for await (const event of stream) {
    if (terminal) {
      throw new ModelContractError(
        "invalid_stream",
        "A model stream emitted an event after its terminal event.",
      );
    }

    switch (event.type) {
      case "text_delta":
        if (event.delta === "") {
          throw new ModelContractError(
            "invalid_stream",
            "text_delta must not be empty.",
          );
        }
        text += event.delta;
        break;
      case "reasoning_delta":
        if (event.delta === "") {
          throw new ModelContractError(
            "invalid_stream",
            "reasoning_delta must not be empty.",
          );
        }
        reasoning += event.delta;
        break;
      case "tool_call_delta": {
        validateToolDelta(event);
        const partial = partials.get(event.index) ?? {
          argumentsJson: "",
          completed: false,
        };
        if (partial.completed) {
          throw new ModelContractError(
            "invalid_stream",
            `Tool call ${event.index} emitted a delta after completion.`,
          );
        }
        setStableField(partial, "callId", event.callId, event.index);
        setStableField(partial, "name", event.name, event.index);
        partial.argumentsJson += event.argumentsDelta ?? "";
        partials.set(event.index, partial);
        break;
      }
      case "tool_call_complete":
        toolCalls.push(completeToolCall(event, partials));
        break;
      case "response_info":
        if (responseInfo !== undefined) {
          throw new ModelContractError(
            "invalid_stream",
            "A normalized model stream may emit response_info only once.",
          );
        }
        responseInfo = normalizeResponseInfo(event.info);
        break;
      case "usage":
        if (usage !== undefined) {
          throw new ModelContractError(
            "invalid_stream",
            "A normalized model stream may emit usage only once.",
          );
        }
        validateUsage(event.usage);
        usage = Object.freeze({ ...event.usage });
        break;
      case "finish":
        finish = Object.freeze({ ...event });
        terminal = true;
        break;
      case "error":
        validateError(event);
        error = Object.freeze({ ...event });
        terminal = true;
        break;
      default: {
        const unreachable: never = event;
        throw new ModelContractError(
          "invalid_stream",
          `Unknown model stream event: ${JSON.stringify(unreachable)}`,
        );
      }
    }
  }

  if (!terminal) {
    throw new ModelContractError(
      "invalid_stream",
      "A model stream ended without a finish or error event.",
    );
  }
  for (const [index, partial] of partials) {
    if (!partial.completed) {
      throw new ModelContractError(
        "invalid_stream",
        `Tool call ${index} did not emit tool_call_complete.`,
      );
    }
  }

  return Object.freeze({
    text,
    reasoning,
    toolCalls: Object.freeze(
      [...toolCalls].sort((left, right) => left.index - right.index),
    ),
    ...(responseInfo === undefined ? {} : { responseInfo }),
    ...(usage === undefined ? {} : { usage }),
    ...(finish === undefined ? {} : { finish }),
    ...(error === undefined ? {} : { error }),
  });
}
