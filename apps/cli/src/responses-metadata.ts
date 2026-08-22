import { canonicalJson } from "@open-agent-lab/contracts";

const MAX_SSE_EVENT_BYTES = 1_048_576;

export interface ResponseMetadata {
  responseId: string | null;
  returnedModel: string | null;
  modelConsistency: "consistent" | "conflict" | "missing";
  modelSources: Record<string, string>;
  systemFingerprint: string | null;
  terminalEvent: string | null;
  usage: Record<string, number> | null;
  metadataConflicts: string[];
  parseErrors: number;
}

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
    if (typeof candidate === "number" && Number.isSafeInteger(candidate) && candidate >= 0) {
      fields[key] = candidate;
    }
  }
  const inputDetails = value.input_tokens_details;
  if (
    isObject(inputDetails) &&
    typeof inputDetails.cached_tokens === "number" &&
    Number.isSafeInteger(inputDetails.cached_tokens) &&
    inputDetails.cached_tokens >= 0
  ) {
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
    isObject(outputDetails) &&
    typeof outputDetails.reasoning_tokens === "number" &&
    Number.isSafeInteger(outputDetails.reasoning_tokens) &&
    outputDetails.reasoning_tokens >= 0
  ) {
    if (
      fields.reasoning_output_tokens !== undefined &&
      fields.reasoning_output_tokens !== outputDetails.reasoning_tokens
    ) {
      throw new Error("conflicting reasoning token aliases");
    }
    fields.reasoning_output_tokens = outputDetails.reasoning_tokens;
  }
  return Object.keys(fields).length > 0 ? fields : null;
}

function hasDuplicateObjectKeys(source: string): boolean {
  type Context =
    | { kind: "array" }
    | { kind: "object"; keys: Set<string>; expectsKey: boolean };
  const stack: Context[] = [];
  for (let index = 0; index < source.length; index += 1) {
    const character = source[index];
    if (character === '"') {
      const start = index;
      while (++index < source.length) {
        if (source[index] === "\\") index += 1;
        else if (source[index] === '"') break;
      }
      const context = stack.at(-1);
      if (context?.kind === "object" && context.expectsKey) {
        const key = JSON.parse(source.slice(start, index + 1)) as string;
        if (context.keys.has(key)) return true;
        context.keys.add(key);
        context.expectsKey = false;
      }
    } else if (character === "{") {
      stack.push({ kind: "object", keys: new Set(), expectsKey: true });
    } else if (character === "[") {
      stack.push({ kind: "array" });
    } else if (character === "}" || character === "]") {
      stack.pop();
    } else if (character === ",") {
      const context = stack.at(-1);
      if (context?.kind === "object") context.expectsKey = true;
    }
  }
  return false;
}

export class SseMetadataObserver {
  private readonly decoder = new TextDecoder("utf-8", { fatal: true });
  private readonly models = new Map<string, string>();
  private readonly modelValues = new Set<string>();
  private readonly responseIds = new Set<string>();
  private readonly fingerprints = new Set<string>();
  private readonly terminalEvents = new Set<string>();
  private readonly usages = new Map<string, Record<string, number>>();
  private buffer = "";
  private disabled = false;
  private terminalModel: string | null = null;
  private terminalResponseId: string | null = null;
  private terminalUsage: Record<string, number> | null = null;
  private parseErrors = 0;
  private eventIndex = 0;
  private modelConflict = false;
  private terminalFrames = 0;

  constructor(modelHeader: string | null) {
    if (modelHeader !== null) this.addModel("http.openai-model", modelHeader);
  }

  feed(chunk: Uint8Array): void {
    if (this.disabled) return;
    try {
      this.buffer += this.decoder.decode(chunk, { stream: true });
      this.consume(false);
    } catch {
      this.failParsing();
    }
  }

  finish(): ResponseMetadata {
    if (!this.disabled) {
      try {
        this.buffer += this.decoder.decode();
        this.consume(true);
      } catch {
        this.failParsing();
      }
    }
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
      usage: this.terminalFrames === 1 ? this.terminalUsage : null,
      metadataConflicts: conflicts,
      parseErrors: this.parseErrors,
    };
  }

  private consume(flush: boolean): void {
    while (true) {
      const match = /(?:\r\n|\r|\n){2}/u.exec(this.buffer);
      if (match === null) break;
      const frame = this.buffer.slice(0, match.index);
      this.buffer = this.buffer.slice(match.index + match[0].length);
      this.observe(frame);
    }
    if (flush && this.buffer.length > 0) {
      this.observe(this.buffer);
      this.buffer = "";
    }
    if (this.buffer.length > MAX_SSE_EVENT_BYTES) {
      this.failParsing();
    }
  }

  private observe(frame: string): void {
    if (frame.length > MAX_SSE_EVENT_BYTES) {
      this.parseErrors += 1;
      return;
    }
    const data = frame
      .split(/\r\n|\r|\n/u)
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trimStart())
      .join("\n");
    if (data.length === 0 || data === "[DONE]") return;
    try {
      if (hasDuplicateObjectKeys(data)) throw new Error("duplicate JSON key");
      const event = JSON.parse(data) as unknown;
      canonicalJson(event);
      if (!isObject(event)) {
        this.parseErrors += 1;
        return;
      }
      const eventType = safeString(event.type);
      if (eventType === null) {
        this.parseErrors += 1;
        return;
      }
      this.eventIndex += 1;
      const terminal =
        eventType.startsWith("response.") &&
        ["response.completed", "response.failed", "response.incomplete"].includes(eventType);
      const response = event.response;
      if (!isObject(response)) {
        if (terminal) this.parseErrors += 1;
        return;
      }
      if (terminal) {
        this.terminalFrames += 1;
        this.addBounded(this.terminalEvents, eventType);
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
      const usage = numericUsage(response.usage);
      if (usage !== null) {
        const key = canonicalJson(usage);
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

  private failParsing(): void {
    this.parseErrors += 1;
    this.buffer = "";
    this.disabled = true;
  }
}
