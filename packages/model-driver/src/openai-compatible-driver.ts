import { assertJsonValue, canonicalJson } from "@open-agent-lab/contracts";
import OpenAI, {
  APIConnectionError,
  APIConnectionTimeoutError,
  APIError,
  APIUserAbortError,
  OpenAIError,
} from "openai";
import type {
  ChatCompletionAssistantMessageParam,
  ChatCompletionCreateParamsNonStreaming,
  ChatCompletionMessageParam,
} from "openai/resources/chat/completions";

import {
  assertRequestSupported,
  parseModelCapabilities,
} from "./capabilities.js";
import { ModelContractError } from "./errors.js";
import type {
  JsonObject,
  JsonValue,
  ModelCallOptions,
  ModelCapabilities,
  ModelDriver,
  ModelErrorEvent,
  ModelFinishReason,
  ModelMessage,
  ModelProbeOptions,
  ModelRequest,
  ModelResponseInfo,
  ModelStreamEvent,
  ModelUsage,
} from "./types.js";

export type OpenAICompatibleDialect = "deepseek" | "glm";

export interface OpenAICompatibleDriverOptions {
  driverId: string;
  dialect: OpenAICompatibleDialect;
  baseUrl: string;
  model: string;
  apiKey: string;
  capabilities: ModelCapabilities;
  fetch?: typeof fetch;
  timeoutMs?: number;
}

interface ProviderExtensions {
  reasoning_content?: unknown;
}

interface ProviderUsage {
  prompt_tokens?: unknown;
  completion_tokens?: unknown;
  total_tokens?: unknown;
  prompt_cache_hit_tokens?: unknown;
  prompt_tokens_details?: unknown;
  completion_tokens_details?: unknown;
}

interface ResponseIdentity {
  responseId: string;
  model: string;
  providerRequestId?: string;
  systemFingerprint?: string;
}

const TOOL_NAME = /^[A-Za-z0-9_-]{1,64}$/;
const API_KEY = /^[\x21-\x7E]+$/;
const IMAGE_MEDIA_TYPE = /^image\/(?:gif|jpeg|png|webp)$/;
const DEFAULT_TIMEOUT_MS = 120_000;

function isAborted(signal: AbortSignal | undefined): boolean {
  return signal?.aborted === true;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function requireRecord(value: unknown, field: string): Record<string, unknown> {
  if (!isRecord(value))
    return invalidStream(`Provider ${field} must be an object.`);
  return value;
}

function invalidRequest(message: string): never {
  throw new ModelContractError("invalid_request", message);
}

function invalidStream(message: string): never {
  throw new ModelContractError("invalid_stream", message);
}

function requireText(value: unknown, field: string): string {
  if (typeof value !== "string" || value.trim() === "") {
    return invalidStream(`Provider ${field} must be a non-empty string.`);
  }
  return value;
}

function optionalText(value: unknown, field: string): string | undefined {
  if (value === undefined || value === null) return undefined;
  if (typeof value !== "string" || value.trim() === "") {
    return invalidStream(`Provider ${field} must be a non-empty string.`);
  }
  return value;
}

function optionalContent(value: unknown, field: string): string | undefined {
  if (value === undefined || value === null || value === "") return undefined;
  if (typeof value !== "string") {
    return invalidStream(`Provider ${field} must be a string.`);
  }
  return value;
}

function validateName(name: string, field: string): void {
  if (!TOOL_NAME.test(name)) {
    invalidRequest(`${field} must match ${TOOL_NAME}.`);
  }
}

function serializeJson(value: JsonValue, field: string): string {
  try {
    return canonicalJson(value);
  } catch {
    return invalidRequest(`${field} must be strict JSON.`);
  }
}

function normalizeBaseUrl(value: string): string {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new ModelContractError(
      "invalid_configuration",
      "baseUrl must be an absolute URL.",
    );
  }
  const local = ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
  if (url.protocol !== "https:" && !(url.protocol === "http:" && local)) {
    throw new ModelContractError(
      "invalid_configuration",
      "baseUrl must use HTTPS, except for an explicit loopback host.",
    );
  }
  if (
    url.username !== "" ||
    url.password !== "" ||
    url.search !== "" ||
    url.hash !== ""
  ) {
    throw new ModelContractError(
      "invalid_configuration",
      "baseUrl cannot contain credentials, a query, or a fragment.",
    );
  }
  url.pathname = url.pathname.replace(/\/+$/, "");
  if (url.pathname.endsWith("/chat/completions")) {
    throw new ModelContractError(
      "invalid_configuration",
      "baseUrl must not include the /chat/completions endpoint.",
    );
  }
  return url.toString().replace(/\/$/, "");
}

function restrictedFetch(
  apiKey: string,
  transport: typeof fetch,
): typeof fetch {
  return async (input, init = {}) => {
    const headers = new Headers({
      accept: "application/json",
      authorization: `Bearer ${apiKey}`,
      "content-type": "application/json",
    });
    return transport(input, { ...init, headers, redirect: "error" });
  };
}

function messageParts(message: ModelMessage): ChatCompletionMessageParam[] {
  if (!Array.isArray(message.content) || message.content.length === 0) {
    return invalidRequest(`A ${message.role} message must contain content.`);
  }

  if (message.role === "system") {
    const text = message.content
      .map((part) => {
        if (part.type !== "text") {
          return invalidRequest("System messages may contain only text.");
        }
        return part.text;
      })
      .join("");
    if (text === "") invalidRequest("A system message must not be empty.");
    return [{ role: "system", content: text }];
  }

  if (message.role === "user") {
    if (message.content.every((part) => part.type === "text")) {
      const text = message.content.map((part) => part.text).join("");
      if (text === "") invalidRequest("A user message must not be empty.");
      return [{ role: "user", content: text }];
    }
    const content = message.content.map((part) => {
      if (part.type === "text")
        return { type: "text" as const, text: part.text };
      if (part.type !== "image") {
        return invalidRequest(
          "User messages may contain only text and images.",
        );
      }
      if (part.source.type === "url") {
        invalidRequest(
          "The portable profile accepts only inline base64 images.",
        );
      }
      if (
        !IMAGE_MEDIA_TYPE.test(part.source.mediaType) ||
        part.source.data === "" ||
        part.source.data.length % 4 !== 0 ||
        !/^[A-Za-z0-9+/]*={0,2}$/.test(part.source.data)
      ) {
        invalidRequest(
          "A base64 image requires a supported media type and valid data.",
        );
      }
      const url = `data:${part.source.mediaType};base64,${part.source.data}`;
      return {
        type: "image_url" as const,
        image_url: {
          url,
          ...(part.detail === undefined ? {} : { detail: part.detail }),
        },
      };
    });
    if (content.every((part) => part.type === "text" && part.text === "")) {
      invalidRequest("A user message must not be empty.");
    }
    return [{ role: "user", content }];
  }

  if (message.role === "assistant") {
    let content = "";
    let reasoning = "";
    const toolCalls: NonNullable<
      ChatCompletionAssistantMessageParam["tool_calls"]
    > = [];
    for (const part of message.content) {
      if (part.type === "text") content += part.text;
      else if (part.type === "reasoning") reasoning += part.text;
      else if (part.type === "tool_call") {
        validateName(part.name, "Tool-call name");
        if (part.callId.trim() === "")
          invalidRequest("Tool-call ID must not be empty.");
        toolCalls.push({
          id: part.callId,
          type: "function",
          function: {
            name: part.name,
            arguments: serializeJson(part.arguments, "Tool-call arguments"),
          },
        });
      } else {
        return invalidRequest(
          "Assistant messages may contain only text, reasoning, and tool calls.",
        );
      }
    }
    if (toolCalls.length > 1) {
      invalidRequest(
        "The portable profile rejects parallel tool-call history.",
      );
    }
    if (content === "" && reasoning === "" && toolCalls.length === 0) {
      invalidRequest("An assistant message must not be empty.");
    }
    const result: ChatCompletionAssistantMessageParam & ProviderExtensions = {
      role: "assistant",
      content: content === "" && toolCalls.length > 0 ? "" : content || null,
      ...(reasoning === "" ? {} : { reasoning_content: reasoning }),
      ...(toolCalls.length === 0 ? {} : { tool_calls: toolCalls }),
    };
    return [result];
  }

  if (message.role !== "tool") {
    return invalidRequest("Message role is not supported.");
  }
  if (message.content.length !== 1) {
    return invalidRequest(
      "The portable profile accepts one tool result per message.",
    );
  }

  return message.content.map((part) => {
    if (part.type !== "tool_result") {
      return invalidRequest("Tool messages may contain only tool results.");
    }
    if (part.callId.trim() === "")
      invalidRequest("Tool-result call ID must not be empty.");
    if (part.isError === true) {
      invalidRequest(
        "OpenAI-compatible chat cannot preserve isError; encode a structured error in content.",
      );
    }
    return { role: "tool", tool_call_id: part.callId, content: part.content };
  });
}

function mapMessages(
  messages: readonly ModelMessage[],
): ChatCompletionMessageParam[] {
  return messages.flatMap(messageParts);
}

function mapTools(
  tools: ModelRequest["tools"],
): ChatCompletionCreateParamsNonStreaming["tools"] {
  if (tools === undefined || tools.length === 0) return undefined;
  const names = new Set<string>();
  return tools.map((tool) => {
    validateName(tool.name, "Tool name");
    if (names.has(tool.name))
      invalidRequest(`Duplicate tool name '${tool.name}'.`);
    names.add(tool.name);
    if (!isRecord(tool.inputSchema)) {
      invalidRequest(`Tool '${tool.name}' inputSchema must be a JSON object.`);
    }
    serializeJson(tool.inputSchema, `Tool '${tool.name}' inputSchema`);
    return {
      type: "function",
      function: {
        name: tool.name,
        ...(tool.description === undefined
          ? {}
          : { description: tool.description }),
        parameters: tool.inputSchema,
      },
    };
  });
}

function validateGeneration(request: ModelRequest, thinking: boolean): void {
  const generation = request.generation;
  if (generation === undefined) return;
  if (
    generation.temperature !== undefined &&
    (!Number.isFinite(generation.temperature) ||
      generation.temperature < 0 ||
      generation.temperature > 1)
  ) {
    invalidRequest("temperature must be in the interval [0, 1].");
  }
  if (
    generation.topP !== undefined &&
    (!Number.isFinite(generation.topP) ||
      generation.topP < 0.01 ||
      generation.topP > 1)
  ) {
    invalidRequest("topP must be in the interval [0.01, 1].");
  }
  if (generation.stop?.some((stop) => stop === "") === true) {
    invalidRequest("Stop sequences must not be empty.");
  }
  if ((generation.stop?.length ?? 0) > 4) {
    invalidRequest("At most four stop sequences are portable.");
  }
  if (
    thinking &&
    (generation.temperature !== undefined || generation.topP !== undefined)
  ) {
    invalidRequest("temperature and topP are not portable in thinking mode.");
  }
}

function hasToolHistory(request: ModelRequest): boolean {
  return request.messages.some((message) =>
    message.content.some(
      (part) => part.type === "tool_call" || part.type === "tool_result",
    ),
  );
}

function hasReasoningHistory(request: ModelRequest): boolean {
  return request.messages.some((message) =>
    message.content.some((part) => part.type === "reasoning"),
  );
}

type CommonBody = ChatCompletionCreateParamsNonStreaming & {
  thinking?: { type: "enabled" | "disabled"; clear_thinking?: false };
};

function requestBody(
  request: ModelRequest,
  dialect: OpenAICompatibleDialect,
  model: string,
): { body: CommonBody; allowedToolNames: ReadonlySet<string> } {
  if (request.responseSchema !== undefined) {
    invalidRequest(
      "responseSchema is not supported by the portable provider profile.",
    );
  }
  if (
    request.toolChoice !== undefined &&
    request.toolChoice !== "auto" &&
    request.toolChoice !== "none"
  ) {
    invalidRequest(
      "The portable provider profile supports only auto or none tool choice.",
    );
  }
  const sendTools = request.toolChoice !== "none";
  const tools = sendTools ? mapTools(request.tools) : undefined;
  if (request.toolChoice === "auto" && tools === undefined) {
    invalidRequest("toolChoice auto requires at least one tool definition.");
  }
  const usesToolProtocol = tools !== undefined || hasToolHistory(request);
  const reasoningHistory = hasReasoningHistory(request);
  if (request.reasoning?.enabled === false && reasoningHistory) {
    invalidRequest(
      "Reasoning cannot be disabled while replaying reasoning history.",
    );
  }
  const enableThinking =
    request.reasoning?.enabled === true || reasoningHistory;
  validateGeneration(request, enableThinking);
  const reasoning = request.reasoning;
  const generation = request.generation;

  const body: CommonBody = {
    model,
    messages: mapMessages(request.messages),
    stream: false,
    ...(tools === undefined ? {} : { tools }),
    ...(generation?.maxOutputTokens === undefined
      ? {}
      : { max_tokens: generation.maxOutputTokens }),
    ...(generation?.temperature === undefined
      ? {}
      : { temperature: generation.temperature }),
    ...(generation?.topP === undefined ? {} : { top_p: generation.topP }),
    ...(generation?.stop === undefined ? {} : { stop: [...generation.stop] }),
    ...(reasoning?.effort === undefined
      ? {}
      : { reasoning_effort: reasoning.effort }),
  };
  if (reasoning !== undefined || (enableThinking && usesToolProtocol)) {
    body.thinking = {
      type: enableThinking ? "enabled" : "disabled",
      ...(dialect === "glm" && enableThinking && usesToolProtocol
        ? { clear_thinking: false as const }
        : {}),
    };
  }
  return {
    body,
    allowedToolNames: new Set(
      request.toolChoice === "none"
        ? []
        : (request.tools ?? []).map((tool) => tool.name),
    ),
  };
}

function optionalCounter(value: unknown, field: string): number | undefined {
  if (value === undefined || value === null) return undefined;
  if (!Number.isSafeInteger(value) || (value as number) < 0) {
    return invalidStream(
      `Provider usage ${field} must be a non-negative safe integer.`,
    );
  }
  return value as number;
}

function requiredCounter(value: unknown, field: string): number {
  return (
    optionalCounter(value, field) ??
    invalidStream(`Provider usage ${field} is required.`)
  );
}

function normalizeUsage(value: unknown): ModelUsage {
  const usage = requireRecord(value, "usage") as ProviderUsage;
  const inputTokens = requiredCounter(usage.prompt_tokens, "prompt_tokens");
  const outputTokens = requiredCounter(
    usage.completion_tokens,
    "completion_tokens",
  );
  const totalTokens = requiredCounter(usage.total_tokens, "total_tokens");
  if (totalTokens !== inputTokens + outputTokens) {
    invalidStream("Provider usage total_tokens is inconsistent.");
  }

  const promptDetails = isRecord(usage.prompt_tokens_details)
    ? usage.prompt_tokens_details
    : undefined;
  const completionDetails = isRecord(usage.completion_tokens_details)
    ? usage.completion_tokens_details
    : undefined;
  const standardCached = optionalCounter(
    promptDetails?.cached_tokens,
    "cached_tokens",
  );
  const providerCached = optionalCounter(
    usage.prompt_cache_hit_tokens,
    "prompt_cache_hit_tokens",
  );
  if (
    standardCached !== undefined &&
    providerCached !== undefined &&
    standardCached !== providerCached
  ) {
    invalidStream("Provider usage cache counters disagree.");
  }
  const cachedInputTokens = standardCached ?? providerCached;
  const reasoningTokens = optionalCounter(
    completionDetails?.reasoning_tokens,
    "reasoning_tokens",
  );
  return {
    inputTokens,
    outputTokens,
    totalTokens,
    ...(cachedInputTokens === undefined ? {} : { cachedInputTokens }),
    ...(reasoningTokens === undefined ? {} : { reasoningTokens }),
  };
}

function responseIdentity(
  value: unknown,
  requestHeader: string | null,
): ResponseIdentity {
  const response = requireRecord(value, "response");
  const responseId = requireText(response.id, "response id");
  const model = requireText(response.model, "model");
  const bodyRequestId = optionalText(response.request_id, "request id");
  const headerRequestId = optionalText(requestHeader, "request header id");
  const providerRequestId = bodyRequestId ?? headerRequestId;
  const systemFingerprint = optionalText(
    response.system_fingerprint,
    "system fingerprint",
  );
  return {
    responseId,
    model,
    ...(providerRequestId === undefined ? {} : { providerRequestId }),
    ...(systemFingerprint === undefined ? {} : { systemFingerprint }),
  };
}

function responseInfo(identity: ResponseIdentity): ModelResponseInfo {
  return { ...identity };
}

function completionFailure(
  dialect: OpenAICompatibleDialect,
  value: unknown,
): ModelErrorEvent | undefined {
  if (dialect === "deepseek" && value === "insufficient_system_resource") {
    return {
      type: "error",
      code: "insufficient_system_resource",
      message: "Provider reported insufficient system resources.",
      retryable: true,
    };
  }
  if (dialect === "glm" && value === "network_error") {
    return {
      type: "error",
      code: "network_error",
      message: "Provider inference failed due to a network error.",
      retryable: true,
    };
  }
  if (dialect === "glm" && value === "model_context_window_exceeded") {
    return {
      type: "error",
      code: "context_window_exceeded",
      message: "Provider rejected a request that exceeded its context window.",
      retryable: false,
    };
  }
  return undefined;
}

function finishReason(
  value: unknown,
  dialect: OpenAICompatibleDialect,
): {
  reason: ModelFinishReason;
  providerReason?: string;
} {
  if (typeof value !== "string" || value === "") {
    return invalidStream("Provider finish_reason must be a non-empty string.");
  }
  if (["stop", "length", "tool_calls", "content_filter"].includes(value)) {
    return { reason: value as ModelFinishReason };
  }
  if (dialect === "glm" && value === "sensitive") {
    return { reason: "content_filter", providerReason: value };
  }
  if (value === "function_call")
    return { reason: "tool_calls", providerReason: value };
  return { reason: "other", providerReason: value };
}

function toolArguments(value: string, index: number): JsonObject {
  let parsed: unknown;
  try {
    parsed = JSON.parse(value) as unknown;
    assertJsonValue(parsed);
  } catch {
    return invalidStream(
      `Provider tool call ${index} emitted invalid JSON arguments.`,
    );
  }
  if (!isRecord(parsed)) {
    return invalidStream(
      `Provider tool call ${index} arguments must be a JSON object.`,
    );
  }
  return parsed as JsonObject;
}

function providerFailure(error: unknown): ModelStreamEvent | undefined {
  if (error instanceof APIConnectionTimeoutError) {
    return {
      type: "error",
      code: "timeout",
      message: "Provider request timed out.",
      retryable: true,
    };
  }
  if (error instanceof APIConnectionError) {
    return {
      type: "error",
      code: "connection_error",
      message: "Provider connection failed.",
      retryable: true,
    };
  }
  if (!(error instanceof APIError)) {
    return error instanceof OpenAIError ||
      error instanceof SyntaxError ||
      error instanceof TypeError
      ? {
          type: "error",
          code: "provider_protocol_error",
          message: "Provider response could not be decoded.",
          retryable: false,
        }
      : undefined;
  }
  const status = error.status;
  const retryable =
    status === 408 ||
    status === 409 ||
    status === 429 ||
    (typeof status === "number" && status >= 500);
  const code =
    status === 429
      ? "rate_limited"
      : typeof status === "number"
        ? `http_${status}`
        : "provider_error";
  return {
    type: "error",
    code,
    message:
      typeof status === "number"
        ? `Provider request failed with HTTP ${status}.`
        : "Provider request failed.",
    retryable,
  };
}

/** One strict Chat Completions adapter shared by the GLM and DeepSeek dialects. */
export class OpenAICompatibleDriver implements ModelDriver {
  readonly driverId: string;
  readonly #dialect: OpenAICompatibleDialect;
  readonly #model: string;
  readonly #capabilities: Readonly<ModelCapabilities>;
  readonly #client: OpenAI;

  constructor(options: OpenAICompatibleDriverOptions) {
    if (
      typeof options.driverId !== "string" ||
      typeof options.model !== "string" ||
      typeof options.baseUrl !== "string" ||
      typeof options.apiKey !== "string" ||
      options.driverId.trim() === "" ||
      options.model.trim() === "" ||
      !API_KEY.test(options.apiKey)
    ) {
      throw new ModelContractError(
        "invalid_configuration",
        "driverId, model, baseUrl, and apiKey must be valid non-empty strings.",
      );
    }
    const timeout = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    if (!Number.isSafeInteger(timeout) || timeout <= 0) {
      throw new ModelContractError(
        "invalid_configuration",
        "timeoutMs must be positive.",
      );
    }
    if (options.dialect !== "glm" && options.dialect !== "deepseek") {
      throw new ModelContractError(
        "invalid_configuration",
        "dialect must be glm or deepseek.",
      );
    }
    this.#capabilities = parseModelCapabilities(options.capabilities);
    if (
      !this.#capabilities.text ||
      this.#capabilities.parallelTools ||
      this.#capabilities.strictSchema
    ) {
      throw new ModelContractError(
        "invalid_configuration",
        "The portable profile requires text and cannot claim parallelTools or strictSchema.",
      );
    }
    this.driverId = options.driverId;
    this.#dialect = options.dialect;
    this.#model = options.model;
    try {
      this.#client = new OpenAI({
        apiKey: options.apiKey,
        adminAPIKey: null,
        baseURL: normalizeBaseUrl(options.baseUrl),
        organization: null,
        project: null,
        webhookSecret: null,
        timeout,
        maxRetries: 0,
        logLevel: "off",
        fetchOptions: { redirect: "error" },
        fetch: restrictedFetch(
          options.apiKey,
          options.fetch ?? globalThis.fetch,
        ),
      });
    } catch {
      throw new ModelContractError(
        "invalid_configuration",
        "Model driver configuration could not be initialized.",
      );
    }
  }

  async probe(options: ModelProbeOptions = {}): Promise<ModelCapabilities> {
    if (isAborted(options.signal)) {
      throw new ModelContractError("aborted", "Capability probe was aborted.");
    }
    return structuredClone(this.#capabilities);
  }

  async *stream(
    request: ModelRequest,
    options: ModelCallOptions = {},
  ): AsyncIterable<ModelStreamEvent> {
    if (isAborted(options.signal)) {
      throw new ModelContractError("aborted", "Model request was aborted.");
    }
    assertRequestSupported(request, this.#capabilities);
    const { body, allowedToolNames } = requestBody(
      request,
      this.#dialect,
      this.#model,
    );

    try {
      yield* this.#complete(body, allowedToolNames, options.signal);
    } catch (error) {
      if (isAborted(options.signal) || error instanceof APIUserAbortError) {
        throw new ModelContractError("aborted", "Model request was aborted.");
      }
      if (error instanceof ModelContractError) throw error;
      const normalized = providerFailure(error);
      if (normalized === undefined) throw error;
      yield normalized;
    }
  }

  async *#complete(
    body: CommonBody,
    allowedToolNames: ReadonlySet<string>,
    signal: AbortSignal | undefined,
  ): AsyncIterable<ModelStreamEvent> {
    const pending = this.#client.chat.completions.create(body, { signal });
    const { data, response } = await pending.withResponse();
    const completion = requireRecord(data, "response");
    const identity = responseIdentity(
      completion,
      response.headers.get("x-request-id"),
    );
    if (
      !Array.isArray(completion.choices) ||
      completion.choices.length !== 1 ||
      !isRecord(completion.choices[0]) ||
      completion.choices[0].index !== 0
    ) {
      invalidStream("Provider must return exactly choice index 0.");
    }
    const choice = completion.choices[0];
    const message = requireRecord(choice.message, "choice message");
    if (message.role !== "assistant") {
      invalidStream("Provider choice message must have the assistant role.");
    }
    const failure = completionFailure(this.#dialect, choice.finish_reason);
    if (failure !== undefined) {
      yield { type: "response_info", info: responseInfo(identity) };
      yield failure;
      return;
    }
    if (optionalContent(message.refusal, "message refusal") !== undefined) {
      invalidStream("Provider returned an unsupported refusal payload.");
    }
    const reasoning = optionalContent(
      message.reasoning_content,
      "reasoning_content",
    );
    const content = optionalContent(message.content, "message content");

    if (
      message.tool_calls !== undefined &&
      !Array.isArray(message.tool_calls)
    ) {
      invalidStream("Provider tool_calls must be an array.");
    }
    const calls = message.tool_calls ?? [];
    if (calls.length > 1) {
      invalidStream("The portable profile returned parallel tool calls.");
    }
    const completedCalls: Array<{
      type: "tool_call_complete";
      index: number;
      callId: string;
      name: string;
      arguments: JsonObject;
    }> = [];
    for (const [index, rawCall] of calls.entries()) {
      const call = requireRecord(rawCall, `tool call ${index}`);
      if (call.type !== "function")
        invalidStream(`Provider tool call ${index} is not a function.`);
      const callId = requireText(call.id, `tool call ${index} id`);
      const fn = requireRecord(call.function, `tool call ${index} function`);
      const name = requireText(fn.name, `tool call ${index} name`);
      if (!TOOL_NAME.test(name)) {
        invalidStream(`Provider tool call ${index} name is invalid.`);
      }
      if (!allowedToolNames.has(name)) {
        invalidStream(`Provider called undeclared tool '${name}'.`);
      }
      completedCalls.push({
        type: "tool_call_complete",
        index,
        callId,
        name,
        arguments: toolArguments(
          requireText(fn.arguments, `tool call ${index} arguments`),
          index,
        ),
      });
    }
    const finish = finishReason(choice.finish_reason, this.#dialect);
    if ((finish.reason === "tool_calls") !== calls.length > 0) {
      invalidStream("Provider finish reason and tool calls disagree.");
    }
    if (
      body.thinking?.type === "enabled" &&
      calls.length > 0 &&
      reasoning === undefined
    ) {
      invalidStream(
        "Thinking tool calls must include reasoning_content for replay.",
      );
    }
    if (completion.usage === undefined || completion.usage === null) {
      invalidStream("Provider response must include usage.");
    }
    const usage = normalizeUsage(completion.usage);

    yield { type: "response_info", info: responseInfo(identity) };
    if (reasoning !== undefined && finish.reason !== "content_filter")
      yield { type: "reasoning_delta", delta: reasoning };
    if (content !== undefined && finish.reason !== "content_filter") {
      yield { type: "text_delta", delta: content };
    }
    yield* completedCalls;
    yield { type: "usage", usage };
    yield { type: "finish", ...finish };
  }
}
