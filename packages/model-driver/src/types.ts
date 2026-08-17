export type JsonPrimitive = null | boolean | number | string;

export type JsonValue = JsonPrimitive | JsonObject | JsonValue[];

export interface JsonObject {
  [key: string]: JsonValue;
}

/**
 * Capabilities are discovered from the configured endpoint. They are never
 * inferred from a model name, which keeps routing policy separate from model
 * identity and allows self-hosted endpoints to report their real behavior.
 */
export interface ModelCapabilities {
  /** Accepts and can emit text. */
  text: boolean;
  /** Accepts image content parts. */
  image: boolean;
  /** Supports tool definitions and tool calls. */
  tools: boolean;
  /** Can request more than one tool call in a single turn. */
  parallelTools: boolean;
  /** Enforces declared JSON schemas rather than treating them as hints. */
  strictSchema: boolean;
  /** Can expose a separate reasoning stream or reasoning control. */
  reasoning: boolean;
  /** Maximum total context window, in tokens. */
  context: number;
  /** Maximum generated output, in tokens. */
  output: number;
}

export interface ModelCapabilityRequirements {
  text?: true;
  image?: true;
  tools?: true;
  parallelTools?: true;
  strictSchema?: true;
  reasoning?: true;
  minContext?: number;
  minOutput?: number;
}

export interface TextContentPart {
  type: "text";
  text: string;
}

export interface ImageUrlSource {
  type: "url";
  url: string;
}

export interface ImageBase64Source {
  type: "base64";
  mediaType: string;
  data: string;
}

export interface ImageContentPart {
  type: "image";
  source: ImageUrlSource | ImageBase64Source;
  detail?: "auto" | "low" | "high";
}

export interface ToolCallContentPart {
  type: "tool_call";
  callId: string;
  name: string;
  arguments: JsonValue;
}

export interface ToolResultContentPart {
  type: "tool_result";
  callId: string;
  content: string;
  isError?: boolean;
}

export type ModelContentPart =
  | TextContentPart
  | ImageContentPart
  | ToolCallContentPart
  | ToolResultContentPart;

export type ModelMessageRole = "system" | "user" | "assistant" | "tool";

export interface ModelMessage {
  role: ModelMessageRole;
  content: readonly ModelContentPart[];
}

export interface ModelToolDefinition {
  name: string;
  description?: string;
  inputSchema: JsonValue;
  /** Request schema enforcement. Requires the `strictSchema` capability. */
  strict?: boolean;
}

export type ModelToolChoice =
  | "auto"
  | "none"
  | "required"
  | { name: string };

export interface ModelResponseSchema {
  name: string;
  schema: JsonValue;
  /** Request schema enforcement. Requires the `strictSchema` capability. */
  strict?: boolean;
}

export interface ModelReasoningOptions {
  enabled: boolean;
  effort?: "low" | "medium" | "high";
}

export interface ModelGenerationOptions {
  maxOutputTokens?: number;
  temperature?: number;
  topP?: number;
  stop?: readonly string[];
}

export interface ModelRequest {
  messages: readonly ModelMessage[];
  tools?: readonly ModelToolDefinition[];
  toolChoice?: ModelToolChoice;
  parallelToolCalls?: boolean;
  responseSchema?: ModelResponseSchema;
  reasoning?: ModelReasoningOptions;
  generation?: ModelGenerationOptions;
  /** Trace-safe adapter metadata. Secrets must not be placed here. */
  metadata?: JsonObject;
}

export interface ModelProbeOptions {
  signal?: AbortSignal;
}

export interface ModelCallOptions {
  signal?: AbortSignal;
}

export interface TextDeltaEvent {
  type: "text_delta";
  delta: string;
}

export interface ReasoningDeltaEvent {
  type: "reasoning_delta";
  delta: string;
}

/**
 * Tool arguments arrive as JSON text fragments. `callId` and `name` may occur
 * only on the first fragment, but an event must carry at least one non-empty
 * field.
 */
export interface ToolCallDeltaEvent {
  type: "tool_call_delta";
  index: number;
  callId?: string;
  name?: string;
  argumentsDelta?: string;
}

export interface ToolCallCompleteEvent {
  type: "tool_call_complete";
  index: number;
  callId: string;
  name: string;
  arguments: JsonValue;
}

export interface ModelUsage {
  inputTokens: number;
  outputTokens: number;
  totalTokens: number;
  cachedInputTokens?: number;
  reasoningTokens?: number;
}

export interface UsageEvent {
  type: "usage";
  usage: ModelUsage;
}

export type ModelFinishReason =
  | "stop"
  | "length"
  | "tool_calls"
  | "content_filter"
  | "cancelled"
  | "other";

export interface FinishEvent {
  type: "finish";
  reason: ModelFinishReason;
  /** Raw provider reason, retained only when normalization loses information. */
  providerReason?: string;
}

export interface ModelErrorEvent {
  type: "error";
  code: string;
  message: string;
  retryable: boolean;
  providerCode?: string;
}

export type ModelStreamEvent =
  | TextDeltaEvent
  | ReasoningDeltaEvent
  | ToolCallDeltaEvent
  | ToolCallCompleteEvent
  | UsageEvent
  | FinishEvent
  | ModelErrorEvent;

export interface ModelDriver {
  /** Stable adapter instance identifier for logs; it is not used for routing. */
  readonly driverId: string;

  /** Probe the configured endpoint and report its effective capabilities. */
  probe(options?: ModelProbeOptions): Promise<ModelCapabilities>;

  /** Produce normalized events. `finish` or `error` must be the final event. */
  stream(
    request: ModelRequest,
    options?: ModelCallOptions,
  ): AsyncIterable<ModelStreamEvent>;
}

export interface StartedModelDriver {
  driver: ModelDriver;
  capabilities: Readonly<ModelCapabilities>;
}

export interface CompletedToolCall {
  index: number;
  callId: string;
  name: string;
  arguments: JsonValue;
}

export interface ModelStreamResult {
  text: string;
  reasoning: string;
  toolCalls: readonly CompletedToolCall[];
  usage?: ModelUsage;
  finish?: FinishEvent;
  error?: ModelErrorEvent;
}
