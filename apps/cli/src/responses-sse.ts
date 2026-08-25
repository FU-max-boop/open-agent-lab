import { canonicalJson } from "@open-agent-lab/contracts";

export const MAX_SSE_EVENT_BYTES = 1_048_576;
export interface ParsedSseFrame {
  raw: Uint8Array;
  event: Record<string, unknown> | null;
  error: "invalid_sse" | "sse_event_too_large" | null;
}
export interface SecretGuardStage {
  secret: boolean;
  invalid: boolean;
  commit: () => void;
}
type Channel = "custom" | "output" | "reasoning" | "summary";
type StatefulItemKind = "custom" | "message" | "other" | "reasoning" | "web";
interface GuardState {
  activeCall: string | null;
  activeItem: string | null;
  citation: CitationState | null;
  tails: Record<Channel, Map<string, string>>;
}
interface CitationState {
  hidden: boolean;
  identity: string;
  pending: string;
  visibleTail: string;
}
interface NestedStatus { secret: boolean; invalid: boolean }
interface DeltaChannel { channel: Channel; identity: string; value: string }
type JsonContext = { kind: "array" } | { kind: "object"; keys: Set<string>; expectsKey: boolean };
function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function optionalString(value: unknown): boolean {
  return value === undefined || value === null || typeof value === "string";
}
function optionalStringArray(value: unknown): boolean {
  return value === undefined || value === null ||
    (Array.isArray(value) && value.every((part) => typeof part === "string"));
}
function optionalSafeInteger(value: unknown): boolean {
  return value === undefined || value === null || Number.isSafeInteger(value);
}
function validEventEnvelope(event: Record<string, unknown>): boolean {
  return optionalString(event.item_id) &&
    optionalString(event.call_id) &&
    optionalString(event.delta) &&
    optionalString(event.text) &&
    optionalSafeInteger(event.summary_index) &&
    optionalSafeInteger(event.content_index);
}
function validInternalMetadata(value: unknown): boolean {
  return value === undefined || value === null ||
    (isObject(value) &&
      optionalString(value.turn_id) &&
      (value.create_time === undefined || value.create_time === null ||
        (typeof value.create_time === "number" && Number.isFinite(value.create_time))));
}
function validContent(value: unknown, types: ReadonlySet<string>): boolean {
  if (!Array.isArray(value)) return false;
  return value.every(
    (part) =>
      isObject(part) &&
      typeof part.type === "string" &&
      types.has(part.type) &&
      typeof part.text === "string",
  );
}
function statefulItemKind(item: Record<string, unknown>): StatefulItemKind | null {
  const type = item.type;
  if (typeof type !== "string" || !optionalString(item.id)) return null;
  if (type === "message") {
    return item.role === "assistant" &&
      validContent(item.content, new Set(["output_text"])) &&
      (item.phase === undefined || item.phase === null ||
        item.phase === "commentary" || item.phase === "final_answer") &&
      validInternalMetadata(item.internal_chat_message_metadata_passthrough)
      ? "message"
      : null;
  }
  if (type === "reasoning") {
    const content = item.content;
    return validContent(item.summary, new Set(["summary_text"])) &&
      (content === undefined || content === null || validContent(content, new Set(["reasoning_text", "text"]))) &&
      optionalString(item.encrypted_content) &&
      validInternalMetadata(item.internal_chat_message_metadata_passthrough)
      ? "reasoning"
      : null;
  }
  if (type === "custom_tool_call") {
    return typeof item.call_id === "string" &&
      typeof item.name === "string" &&
      typeof item.input === "string" &&
      optionalString(item.status) &&
      optionalString(item.namespace) &&
      validInternalMetadata(item.internal_chat_message_metadata_passthrough)
      ? "custom"
      : null;
  }
  if (type === "web_search_call") {
    return optionalString(item.status) &&
      validWebSearchAction(item.action) &&
      validInternalMetadata(item.internal_chat_message_metadata_passthrough)
      ? "web"
      : null;
  }
  return "other";
}
function validWebSearchAction(value: unknown): boolean {
  if (value === undefined || value === null) return true;
  if (!isObject(value) || typeof value.type !== "string") return false;
  if (value.type === "search") return optionalString(value.query) && optionalStringArray(value.queries);
  if (value.type === "open_page") return optionalString(value.url);
  if (value.type === "find_in_page") return optionalString(value.url) && optionalString(value.pattern);
  return true;
}
function webSearchActionDetail(value: unknown): string {
  if (!isObject(value) || typeof value.type !== "string") return "";
  if (value.type === "search") {
    if (typeof value.query === "string" && value.query.length > 0) return value.query;
    const first = Array.isArray(value.queries) && typeof value.queries[0] === "string"
      ? value.queries[0]
      : "";
    return Array.isArray(value.queries) && value.queries.length > 1 && first.length > 0
      ? `${first} ...`
      : first;
  }
  if (value.type === "open_page") return typeof value.url === "string" ? value.url : "";
  if (value.type === "find_in_page") {
    const url = typeof value.url === "string" ? value.url : null;
    const pattern = typeof value.pattern === "string" ? value.pattern : null;
    if (pattern !== null && url !== null) return `'${pattern}' in ${url}`;
    if (pattern !== null) return `'${pattern}'`;
    return url ?? "";
  }
  return "";
}
function updateJsonContext(token: string, stack: JsonContext[]): void {
  const context = stack.at(-1);
  if (token === "{") stack.push({ kind: "object", keys: new Set(), expectsKey: true });
  else if (token === "[") stack.push({ kind: "array" });
  else if (token === "}" || token === "]") stack.pop();
  else if (token === "," && context?.kind === "object") context.expectsKey = true;
}
function hasDuplicateObjectKeys(source: string): boolean {
  const stack: JsonContext[] = [];
  for (const match of source.matchAll(/"(?:\\[\s\S]|[^"\\])*"|[{}\[\],]/gu)) {
    const token = match[0];
    if (!token.startsWith('"')) {
      updateJsonContext(token, stack);
      continue;
    }
    const context = stack.at(-1);
    if (context?.kind === "object" && context.expectsKey) {
      const key = JSON.parse(token) as string;
      if (context.keys.has(key)) return true;
      context.keys.add(key);
      context.expectsKey = false;
    }
  }
  return false;
}
function valueContainsSecret(value: unknown, secret: string): boolean {
  const pending: unknown[] = [value];
  while (pending.length > 0) {
    const candidate = pending.pop();
    if (typeof candidate === "string" && candidate.includes(secret)) return true;
    if (Array.isArray(candidate)) {
      for (const nested of candidate) pending.push(nested);
    } else if (isObject(candidate)) {
      for (const [key, nested] of Object.entries(candidate)) {
        if (key.includes(secret)) return true;
        pending.push(nested);
      }
    }
  }
  return false;
}
function nextTail(
  previous: string | undefined,
  value: string,
  secret: string,
): { hit: boolean; suffix: string } {
  const retained = previous ?? "";
  const limit = secret.length - 1;
  const combined = `${retained}${value}`;
  return {
    hit: `${retained}${value.slice(0, limit)}`.includes(secret),
    suffix: limit === 0 ? "" : combined.slice(-limit),
  };
}
const CITATION_OPEN = "<oai-mem-citation>";
const CITATION_CLOSE = "</oai-mem-citation>";
function longestSuffixPrefix(value: string, marker: string): number {
  const maximum = Math.min(value.length, marker.length - 1);
  for (let length = maximum; length > 0; length -= 1) {
    if (value.endsWith(marker.slice(0, length))) return length;
  }
  return 0;
}
function pushCitationText(state: CitationState, value: string, secret: string): boolean {
  let pending = `${state.pending}${value}`;
  state.pending = "";
  while (pending.length > 0) {
    const marker = state.hidden ? CITATION_CLOSE : CITATION_OPEN;
    const found = pending.indexOf(marker);
    if (found >= 0) {
      if (!state.hidden) {
        const visible = nextTail(state.visibleTail, pending.slice(0, found), secret);
        state.visibleTail = visible.suffix;
        if (visible.hit) return true;
      }
      pending = pending.slice(found + marker.length);
      state.hidden = !state.hidden;
      continue;
    }
    const keep = longestSuffixPrefix(pending, marker);
    const consumed = pending.slice(0, pending.length - keep);
    if (!state.hidden) {
      const visible = nextTail(state.visibleTail, consumed, secret);
      state.visibleTail = visible.suffix;
      if (visible.hit) return true;
    }
    state.pending = pending.slice(pending.length - keep);
    return false;
  }
  return false;
}
function nestedArgumentsStatus(item: Record<string, unknown>, secret: string): NestedStatus {
  if (item.type !== "function_call" || typeof item.arguments !== "string") return { secret: false, invalid: false };
  if (item.arguments.length === 0) return { secret: false, invalid: false };
  try {
    const invalid = hasDuplicateObjectKeys(item.arguments);
    const nested = JSON.parse(item.arguments) as unknown;
    canonicalJson(nested);
    return { secret: valueContainsSecret(nested, secret), invalid };
  } catch {
    return { secret: false, invalid: true };
  }
}
function freshState(): GuardState {
  const tails = { custom: new Map(), output: new Map(), reasoning: new Map(), summary: new Map() };
  return { activeCall: null, activeItem: null, citation: null, tails };
}
function cloneState(state: GuardState): GuardState {
  const tails = { custom: new Map(state.tails.custom), output: new Map(state.tails.output), reasoning: new Map(state.tails.reasoning), summary: new Map(state.tails.summary) };
  return {
    activeCall: state.activeCall,
    activeItem: state.activeItem,
    citation: state.citation === null ? null : { ...state.citation },
    tails,
  };
}
function typedTexts(
  value: unknown, acceptedTypes: ReadonlySet<string>,
): Array<{ identity: string; text: string }> {
  if (!Array.isArray(value)) return [];
  const found: Array<{ identity: string; text: string }> = [];
  for (let index = 0; index < value.length; index += 1) {
    const candidate = value[index];
    if (
      isObject(candidate) &&
      typeof candidate.type === "string" &&
      acceptedTypes.has(candidate.type) &&
      typeof candidate.text === "string"
    ) found.push({ identity: String(index), text: candidate.text });
  }
  return found;
}
function sseData(body: Uint8Array, first: boolean): string {
  const hasBom = first && body[0] === 0xef && body[1] === 0xbb && body[2] === 0xbf;
  const text = new TextDecoder("utf-8", { fatal: true }).decode(hasBom ? body.subarray(3) : body);
  const data: string[] = [];
  for (const line of text.split(/\r\n|\r|\n/u)) {
    const colon = line.indexOf(":");
    const field = colon < 0 ? line : line.slice(0, colon);
    let value = colon < 0 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "data") data.push(value);
  }
  return data.join("\n");
}
function parseFrame(raw: Uint8Array, body: Uint8Array, first: boolean): ParsedSseFrame {
  if (body.byteLength > MAX_SSE_EVENT_BYTES) {
    return { raw, event: null, error: "sse_event_too_large" };
  }
  try {
    const source = sseData(body, first);
    if (source.length === 0 || source === "[DONE]") return { raw, event: null, error: null };
    const duplicate = hasDuplicateObjectKeys(source);
    const event = JSON.parse(source) as unknown;
    canonicalJson(event);
    if (!isObject(event) || typeof event.type !== "string") throw new Error("invalid event");
    return { raw, event, error: duplicate ? "invalid_sse" : null };
  } catch {
    return { raw, event: null, error: "invalid_sse" };
  }
}
function parseTail(raw: Uint8Array, first: boolean): ParsedSseFrame {
  if (raw.byteLength > MAX_SSE_EVENT_BYTES) {
    return { raw, event: null, error: "sse_event_too_large" };
  }
  try {
    sseData(raw, first);
    return { raw, event: null, error: null };
  } catch {
    return { raw, event: null, error: "invalid_sse" };
  }
}
export class ResponsesSseParser {
  private buffer = Buffer.alloc(0);
  private scanAt = 0; private lineStart = 0; private lastContentEnd = 0;
  private first = true; private stopped = false;
  feed(chunk: Uint8Array): ParsedSseFrame[] {
    if (this.stopped || chunk.byteLength === 0) return [];
    this.buffer = Buffer.concat([this.buffer, chunk]);
    return this.drain(false);
  }
  finish(): ParsedSseFrame[] {
    return this.stopped ? [] : this.drain(true);
  }
  private drain(flush: boolean): ParsedSseFrame[] {
    const frames: ParsedSseFrame[] = [];
    while (this.scanAt < this.buffer.length) {
      const byte = this.buffer[this.scanAt];
      if (byte !== 0x0a && byte !== 0x0d) {
        this.scanAt += 1;
        continue;
      }
      if (byte === 0x0d && this.scanAt + 1 === this.buffer.length && !flush) break;
      const crlf = byte === 0x0d && this.buffer[this.scanAt + 1] === 0x0a;
      const eolEnd = this.scanAt + (crlf ? 2 : 1);
      if (this.scanAt === this.lineStart) {
        const raw = this.buffer.subarray(0, eolEnd);
        const frame = parseFrame(raw, this.buffer.subarray(0, this.lastContentEnd), this.first);
        frames.push(frame);
        this.first = false;
        this.buffer = this.buffer.subarray(eolEnd);
        this.scanAt = this.lineStart = this.lastContentEnd = 0;
        this.stopped = frame.error !== null;
        if (this.stopped) break;
      } else {
        this.lastContentEnd = this.scanAt;
        this.lineStart = eolEnd;
        this.scanAt = eolEnd;
      }
    }
    if (!this.stopped && !flush && this.buffer.length > MAX_SSE_EVENT_BYTES + 4) {
      frames.push({ raw: this.buffer, event: null, error: "sse_event_too_large" });
      this.stopped = true;
    } else if (!this.stopped && flush && this.buffer.length > 0) {
      const frame = parseTail(this.buffer, this.first);
      frames.push(frame);
      this.first = false;
      this.stopped = frame.error !== null;
      this.buffer = Buffer.alloc(0);
    }
    return frames;
  }
}
export class Codex149SecretGuard {
  private state = freshState();
  constructor(private readonly secret: string) {}
  stage(frame: ParsedSseFrame): SecretGuardStage {
    const raw = Buffer.from(frame.raw.buffer, frame.raw.byteOffset, frame.raw.byteLength);
    if (raw.includes(Buffer.from(this.secret))) return this.result(true, false, this.state);
    const event = frame.event;
    if (event === null) return this.result(false, false, this.state);
    if (valueContainsSecret(event, this.secret)) return this.result(true, false, this.state);
    if (!validEventEnvelope(event)) return this.result(false, true, this.state);
    const next = cloneState(this.state);
    const itemStatus = this.transitionItem(event, next);
    if (itemStatus?.secret || itemStatus?.invalid) {
      return this.result(itemStatus.secret, itemStatus.invalid, next);
    }
    if (event.type === "response.completed" && this.finishCitation(next)) {
      return this.result(true, false, next);
    }
    const delta = itemStatus === null ? this.deltaChannel(event, next) : null;
    const hit = delta === null ? false : this.extend(next, delta.channel, delta.identity, delta.value);
    return this.result(hit, false, next);
  }
  private extend(state: GuardState, channel: Channel, identity: string, value: unknown): boolean {
    if (typeof value !== "string") return false;
    if (
      channel === "output" &&
      state.citation?.identity === identity &&
      pushCitationText(state.citation, value, this.secret)
    ) return true;
    const tails = state.tails[channel];
    const result = nextTail(tails.get(identity), value, this.secret);
    tails.set(identity, result.suffix);
    return result.hit;
  }
  private transitionItem(event: Record<string, unknown>, state: GuardState): NestedStatus | null {
    if (event.type !== "response.output_item.added" && event.type !== "response.output_item.done") {
      return null;
    }
    const item = isObject(event.item) ? event.item : null;
    if (item === null) return { secret: false, invalid: true };
    const kind = statefulItemKind(item);
    if (kind === null) return { secret: false, invalid: true };
    const nested = nestedArgumentsStatus(item, this.secret);
    if (nested.secret || nested.invalid) return nested;
    const added = event.type === "response.output_item.added";
    if (added && this.seedAdded(state, item, kind)) return { secret: true, invalid: false };
    if (!added) {
      if (this.finishCitation(state)) return { secret: true, invalid: false };
      const finalState = freshState();
      if (kind !== "other" && this.seedAdded(finalState, item, kind)) {
        return { secret: true, invalid: false };
      }
      if (this.finishCitation(finalState)) return { secret: true, invalid: false };
      Object.assign(state, freshState());
    }
    return nested;
  }
  private finishCitation(state: GuardState): boolean {
    const citation = state.citation;
    state.citation = null;
    if (citation === null || citation.hidden) return false;
    const visible = nextTail(citation.visibleTail, citation.pending, this.secret);
    return visible.hit;
  }
  private deltaChannel(event: Record<string, unknown>, state: GuardState): DeltaChannel | null {
    if (typeof event.delta !== "string") return null;
    const active = state.activeItem;
    if (event.type === "response.output_text.delta") {
      return active === null ? null : { channel: "output", identity: active, value: event.delta };
    }
    if (event.type === "response.reasoning_summary_text.delta" && Number.isSafeInteger(event.summary_index)) {
      return active === null ? null : { channel: "summary", identity: `${active}:${String(event.summary_index)}`, value: event.delta };
    }
    if (event.type === "response.reasoning_text.delta" && Number.isSafeInteger(event.content_index)) {
      return active === null ? null : { channel: "reasoning", identity: `${active}:${String(event.content_index)}`, value: event.delta };
    }
    if (event.type !== "response.custom_tool_call_input.delta") return null;
    const itemId = typeof event.item_id === "string" ? event.item_id : null;
    const callId = typeof event.call_id === "string" ? event.call_id : null;
    if (state.activeCall === null || (itemId === null && callId === null)) return null;
    if (callId !== null && callId !== state.activeCall) return null;
    return { channel: "custom", identity: state.activeCall, value: event.delta };
  }
  private seedAdded(
    state: GuardState,
    item: Record<string, unknown>,
    kind: StatefulItemKind,
  ): boolean {
    if (kind === "other") return false;
    if (kind === "custom") {
      state.tails.custom.clear();
      state.activeCall = item.call_id as string;
      return this.extend(state, "custom", state.activeCall, item.input);
    }
    state.tails.output.clear();
    state.tails.reasoning.clear();
    state.tails.summary.clear();
    state.citation = null;
    const type = item.type as string;
    const itemId =
      (typeof item.id === "string" ? item.id : null) ??
      type;
    state.activeItem = itemId;
    if (kind === "message") {
      state.citation = {
        hidden: false,
        identity: state.activeItem,
        pending: "",
        visibleTail: "",
      };
      const joined = typedTexts(item.content, new Set(["output_text"])).map(({ text }) => text).join("");
      return joined.includes(this.secret) || this.extend(state, "output", state.activeItem, joined);
    }
    if (kind === "reasoning") {
      for (const part of typedTexts(item.summary, new Set(["summary_text"]))) {
        if (this.extend(state, "summary", `${state.activeItem}:${part.identity}`, part.text)) return true;
      }
      for (const part of typedTexts(item.content, new Set(["reasoning_text", "text"]))) {
        if (this.extend(state, "reasoning", `${state.activeItem}:${part.identity}`, part.text)) return true;
      }
    }
    if (kind === "web") {
      return this.extend(
        state,
        "output",
        state.activeItem,
        webSearchActionDetail(item.action),
      );
    }
    return false;
  }
  private result(secret: boolean, invalid: boolean, next: GuardState): SecretGuardStage {
    return {
      secret,
      invalid,
      commit: () => {
        if (!secret && !invalid) this.state = next;
      },
    };
  }
}
export function inspectNonSuccessBody(
  body: Uint8Array,
  secret: string,
): "invalid" | "safe" | "secret" {
  const raw = Buffer.from(body.buffer, body.byteOffset, body.byteLength);
  if (raw.includes(Buffer.from(secret))) return "secret";
  let decoded: string;
  try {
    decoded = new TextDecoder("utf-8", { fatal: true }).decode(body);
  } catch {
    return "invalid";
  }
  if (decoded.includes(secret)) return "secret";
  try {
    const parsed = JSON.parse(decoded) as unknown;
    return valueContainsSecret(parsed, secret) ? "secret" : "safe";
  } catch {
    return "safe";
  }
}
