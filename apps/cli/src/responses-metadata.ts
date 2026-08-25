import { canonicalJson } from "@open-agent-lab/contracts";

export interface ResponseMetadata {
  responseId: string | null;
  returnedModel: string | null;
  modelConsistency: "consistent" | "conflict" | "missing";
  modelSources: Record<string, string>;
  systemFingerprint: string | null;
  terminalEvent: string | null;
  terminalStatus: "completed" | "failed" | "incomplete" | null;
  incompleteReason: string | null;
  usage: Record<string, number> | null;
  metadataConflicts: string[];
  parseErrors: number;
}

export interface ToolOutputContinuationBinding {
  type: "function_call_output" | "custom_tool_call_output";
  callId: string;
}

type TerminalStatus = Exclude<ResponseMetadata["terminalStatus"], null>;

const TERMINAL_STATUS_BY_EVENT = {
  "response.completed": "completed",
  "response.failed": "failed",
  "response.incomplete": "incomplete",
} as const satisfies Record<string, TerminalStatus>;

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function safeString(value: unknown): string | null {
  return typeof value === "string" &&
    value.length > 0 &&
    value.length <= 512 &&
    !/[\u0000-\u001f]/u.test(value)
    ? value
    : null;
}

function numericUsage(value: unknown): Record<string, number> | null {
  if (!isObject(value)) return null;
  const fields: Record<string, number> = {};
  for (const key of [
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_input_tokens",
    "reasoning_output_tokens",
  ]) {
    const candidate = value[key];
    if (candidate === undefined) continue;
    if (typeof candidate !== "number" || !Number.isSafeInteger(candidate) || candidate < 0) {
      throw new Error(`invalid ${key}`);
    }
    fields[key] = candidate;
  }
  const inputDetails = value.input_tokens_details;
  if (
    inputDetails !== undefined &&
    inputDetails !== null &&
    !isObject(inputDetails)
  ) {
    throw new Error("invalid input token details");
  }
  if (isObject(inputDetails) && inputDetails.cached_tokens !== undefined) {
    if (
      typeof inputDetails.cached_tokens !== "number" ||
      !Number.isSafeInteger(inputDetails.cached_tokens) ||
      inputDetails.cached_tokens < 0
    ) {
      throw new Error("invalid cached tokens");
    }
    if (
      fields.cached_input_tokens !== undefined &&
      fields.cached_input_tokens !== inputDetails.cached_tokens
    ) {
      throw new Error("conflicting cached token aliases");
    }
    fields.cached_input_tokens = inputDetails.cached_tokens;
  }
  const outputDetails = value.output_tokens_details;
  if (
    outputDetails !== undefined &&
    outputDetails !== null &&
    !isObject(outputDetails)
  ) {
    throw new Error("invalid output token details");
  }
  if (isObject(outputDetails) && outputDetails.reasoning_tokens !== undefined) {
    if (
      typeof outputDetails.reasoning_tokens !== "number" ||
      !Number.isSafeInteger(outputDetails.reasoning_tokens) ||
      outputDetails.reasoning_tokens < 0
    ) {
      throw new Error("invalid reasoning tokens");
    }
    if (
      fields.reasoning_output_tokens !== undefined &&
      fields.reasoning_output_tokens !== outputDetails.reasoning_tokens
    ) {
      throw new Error("conflicting reasoning token aliases");
    }
    fields.reasoning_output_tokens = outputDetails.reasoning_tokens;
  }
  if (
    (fields.input_tokens !== undefined &&
      fields.cached_input_tokens !== undefined &&
      fields.cached_input_tokens > fields.input_tokens) ||
    (fields.output_tokens !== undefined &&
      fields.reasoning_output_tokens !== undefined &&
      fields.reasoning_output_tokens > fields.output_tokens) ||
    (fields.input_tokens !== undefined &&
      fields.output_tokens !== undefined &&
      fields.total_tokens !== undefined &&
      fields.total_tokens !== fields.input_tokens + fields.output_tokens)
  ) {
    throw new Error("impossible token usage");
  }
  return Object.keys(fields).length > 0 ? fields : null;
}

function terminalMetadata(
  eventType: keyof typeof TERMINAL_STATUS_BY_EVENT,
  response: Record<string, unknown>,
): {
  terminalStatus: TerminalStatus | null;
  incompleteReason: string | null;
} {
  const expectedStatus = TERMINAL_STATUS_BY_EVENT[eventType];
  if (
    response.status !== undefined &&
    response.status !== null &&
    response.status !== expectedStatus
  ) {
    throw new Error("terminal response status mismatch");
  }
  const terminalStatus = response.status === expectedStatus ? expectedStatus : null;

  const error = response.error;
  const incompleteDetails = response.incomplete_details;
  if (expectedStatus === "completed") {
    if (
      (error !== undefined && error !== null) ||
      (incompleteDetails !== undefined && incompleteDetails !== null)
    ) {
      throw new Error("completed response carries terminal error details");
    }
    return { terminalStatus, incompleteReason: null };
  }
  if (expectedStatus === "failed") {
    if (
      (error !== undefined && error !== null && !isObject(error)) ||
      (incompleteDetails !== undefined && incompleteDetails !== null)
    ) {
      throw new Error("failed response carries invalid terminal details");
    }
    return { terminalStatus, incompleteReason: null };
  }
  if (
    (error !== undefined && error !== null) ||
    !isObject(incompleteDetails)
  ) {
    throw new Error("incomplete response carries invalid terminal details");
  }
  const incompleteReason = safeString(incompleteDetails.reason);
  if (incompleteReason === null) throw new Error("invalid incomplete response reason");
  return { terminalStatus, incompleteReason };
}

function completedToolOutput(
  response: Record<string, unknown>,
): ToolOutputContinuationBinding | null {
  if (!Array.isArray(response.output)) return null;
  let binding: ToolOutputContinuationBinding | null = null;
  for (const value of response.output) {
    if (!isObject(value)) return null;
    if (value.type === "reasoning") continue;
    if (value.type !== "function_call" && value.type !== "custom_tool_call") return null;
    const callId = safeString(value.call_id);
    if (callId === null || binding !== null) return null;
    binding = {
      type: value.type === "function_call"
        ? "function_call_output"
        : "custom_tool_call_output",
      callId,
    };
  }
  return binding;
}

export class SseMetadataObserver {
  private readonly models = new Map<string, string>();
  private readonly modelValues = new Set<string>();
  private readonly responseIds = new Set<string>();
  private readonly fingerprints = new Set<string>();
  private readonly terminalEvents = new Set<string>();
  private readonly usages = new Map<string, Record<string, number>>();
  private terminalModel: string | null = null;
  private terminalResponseId: string | null = null;
  private terminalStatus: TerminalStatus | null = null;
  private incompleteReason: string | null = null;
  private terminalUsage: Record<string, number> | null = null;
  private terminalToolOutput: ToolOutputContinuationBinding | null = null;
  private parseErrors = 0;
  private eventIndex = 0;
  private modelConflict = false;
  private terminalFrames = 0;

  constructor(modelHeader: string | null) {
    if (modelHeader !== null) this.addModel("http.openai-model", modelHeader);
  }

  finish(): ResponseMetadata {
    const conflicts = [
      ...(this.modelConflict ? ["model"] : []),
      ...(this.responseIds.size > 1 ? ["response_id"] : []),
      ...(this.fingerprints.size > 1 ? ["system_fingerprint"] : []),
      ...(this.terminalFrames > 1 ? ["terminal_event"] : []),
      ...(this.usages.size > 1 ? ["usage"] : []),
    ];
    return {
      responseId:
        this.terminalFrames === 1 && this.responseIds.size === 1
          ? this.terminalResponseId
          : null,
      returnedModel:
        this.terminalFrames === 1 && !this.modelConflict && this.modelValues.size === 1
          ? this.terminalModel
          : null,
      modelConsistency:
        this.modelValues.size === 0 ? "missing" : this.modelConflict ? "conflict" : "consistent",
      modelSources: Object.fromEntries(this.models),
      systemFingerprint: this.fingerprints.size === 1 ? [...this.fingerprints][0]! : null,
      terminalEvent:
        this.terminalFrames === 1 && this.terminalEvents.size === 1
          ? [...this.terminalEvents][0]!
          : null,
      terminalStatus: this.terminalFrames === 1 ? this.terminalStatus : null,
      incompleteReason: this.terminalFrames === 1 ? this.incompleteReason : null,
      usage: this.terminalFrames === 1 ? this.terminalUsage : null,
      metadataConflicts: conflicts,
      parseErrors: this.parseErrors,
    };
  }

  recordParseError(): void {
    this.parseErrors += 1;
  }

  toolOutputContinuation(): ToolOutputContinuationBinding | null {
    if (
      this.terminalFrames !== 1 ||
      this.terminalEvents.size !== 1 ||
      !this.terminalEvents.has("response.completed")
    ) {
      return null;
    }
    return this.terminalToolOutput === null ? null : { ...this.terminalToolOutput };
  }

  observe(event: Record<string, unknown>): void {
    try {
      const eventType = safeString(event.type);
      if (eventType === null) {
        this.parseErrors += 1;
        return;
      }
      this.eventIndex += 1;
      const terminal = Object.hasOwn(TERMINAL_STATUS_BY_EVENT, eventType);
      const response = event.response;
      if (!isObject(response)) {
        if (terminal) this.parseErrors += 1;
        return;
      }
      const terminalTuple = terminal
        ? terminalMetadata(
            eventType as keyof typeof TERMINAL_STATUS_BY_EVENT,
            response,
          )
        : null;
      const validatedTerminalUsage = terminal ? numericUsage(response.usage) : undefined;
      const validatedTerminalUsageKey = validatedTerminalUsage
        ? canonicalJson(validatedTerminalUsage)
        : null;
      if (terminal) {
        this.terminalFrames += 1;
        this.addBounded(this.terminalEvents, eventType);
        this.terminalStatus = terminalTuple!.terminalStatus;
        this.incompleteReason = terminalTuple!.incompleteReason;
        this.terminalToolOutput = eventType === "response.completed"
          ? completedToolOutput(response)
          : null;
      }
      const responseId = safeString(response.id);
      if (responseId !== null) {
        this.addBounded(this.responseIds, responseId);
        if (terminal) this.terminalResponseId = responseId;
      }
      const model = safeString(response.model);
      if (model !== null) {
        this.addModel(`event.${eventType}.response.model`, model, terminal);
        if (terminal) this.terminalModel = model;
      }
      const headers = response.headers;
      if (isObject(headers)) {
        const headerModel = safeString(headers["openai-model"]);
        if (headerModel !== null) {
          this.addModel(
            `event.${eventType}.response.headers.openai-model`,
            headerModel,
            terminal,
          );
        }
      }
      const fingerprint = safeString(response.system_fingerprint);
      if (fingerprint !== null) this.addBounded(this.fingerprints, fingerprint);
      const usage = terminal ? validatedTerminalUsage! : numericUsage(response.usage);
      if (usage !== null) {
        const key = validatedTerminalUsageKey ?? canonicalJson(usage);
        if (this.usages.size < 2 || this.usages.has(key)) this.usages.set(key, usage);
        if (terminal) this.terminalUsage = usage;
      }
    } catch {
      this.parseErrors += 1;
    }
  }

  private addModel(source: string, model: string, terminal = false): void {
    if (this.modelValues.size > 0 && !this.modelValues.has(model)) this.modelConflict = true;
    if (this.modelValues.size < 2 || this.modelValues.has(model)) this.modelValues.add(model);
    if (this.models.size < (terminal ? 16 : 15)) {
      this.models.set(`${source}.${this.eventIndex}`, model);
    }
  }

  private addBounded(values: Set<string>, value: string): void {
    if (values.size < 2 || values.has(value)) values.add(value);
  }
}
