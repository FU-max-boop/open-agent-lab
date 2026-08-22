import { createServer, type IncomingMessage, type ServerResponse } from "node:http";

const MAX_REQUEST_BYTES = 5 * 1024 * 1024;
const DEFAULT_CALL_ID = "call_open_agent_lab_fixture";

export interface ResponsesFixtureRequest {
  readonly method: string;
  readonly url: string;
  readonly body: Record<string, unknown>;
}

export interface ResponsesFixtureSnapshot {
  readonly requests: readonly ResponsesFixtureRequest[];
  readonly toolName: string;
  readonly toolOutput: string;
}

export interface ResponsesFixtureOptions {
  readonly bearer: string;
  readonly model: string;
  readonly command: string;
  readonly finalMessage?: string;
  readonly callId?: string;
  readonly instructionMarker?: string;
  readonly host?: string;
  readonly port?: number;
}

export interface ResponsesFixture {
  readonly baseUrl: string;
  snapshot(): ResponsesFixtureSnapshot;
  close(): Promise<void>;
}

async function jsonBody(request: IncomingMessage): Promise<Record<string, unknown>> {
  const chunks: Buffer[] = [];
  let bytes = 0;
  for await (const chunk of request) {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    bytes += buffer.byteLength;
    if (bytes > MAX_REQUEST_BYTES) throw new Error("Fixture request exceeded 5 MiB.");
    chunks.push(buffer);
  }
  const value: unknown = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("Fixture request body must be a JSON object.");
  }
  return value as Record<string, unknown>;
}

function event(response: ServerResponse, value: Record<string, unknown>): void {
  response.write(`event: ${String(value.type)}\n`);
  response.write(`data: ${JSON.stringify(value)}\n\n`);
}

function complete(response: ServerResponse, id: string, model: string): void {
  event(response, {
    type: "response.completed",
    response: {
      id,
      model,
      usage: {
        input_tokens: 64,
        input_tokens_details: { cached_tokens: 0 },
        output_tokens: 16,
        output_tokens_details: { reasoning_tokens: 0 },
        total_tokens: 80,
      },
    },
  });
  response.end();
}

function execTool(body: Record<string, unknown>): string {
  const tools = Array.isArray(body.tools) ? body.tools : [];
  const found = tools.find(
    (tool): tool is Record<string, unknown> =>
      typeof tool === "object" &&
      tool !== null &&
      !Array.isArray(tool) &&
      tool.type === "function" &&
      tool.name === "exec_command",
  );
  if (found === undefined) throw new Error("Codex did not advertise exec_command.");
  return "exec_command";
}

function toolResult(body: Record<string, unknown>, callId: string): string | undefined {
  if (!Array.isArray(body.input)) return undefined;
  const item = body.input.find(
    (candidate) =>
      typeof candidate === "object" &&
      candidate !== null &&
      !Array.isArray(candidate) &&
      candidate.type === "function_call_output" &&
      candidate.call_id === callId,
  );
  return item !== undefined && "output" in item && typeof item.output === "string"
    ? item.output
    : undefined;
}

function instructionPaths(value: unknown, marker: string, path = "$"): string[] {
  if (typeof value === "string") return value.includes(marker) ? [path] : [];
  if (Array.isArray(value)) {
    return value.flatMap((item, index) => instructionPaths(item, marker, `${path}[${index}]`));
  }
  if (typeof value !== "object" || value === null) return [];
  return Object.entries(value).flatMap(([key, item]) =>
    instructionPaths(item, marker, `${path}.${key}`),
  );
}

function hasInstructionMarker(body: Record<string, unknown>, marker: string): boolean {
  const paths = instructionPaths(body, marker);
  if (paths.length === 0) return false;
  if (paths.length !== 1 || paths[0] !== "$.input[0].content[0].text") {
    throw new Error("Codex placed the frozen developer instruction at an invalid path.");
  }
  const input = body.input;
  const message = Array.isArray(input) ? input[0] : undefined;
  const messageRecord =
    typeof message === "object" && message !== null && !Array.isArray(message)
      ? (message as Record<string, unknown>)
      : undefined;
  const content = messageRecord?.content;
  const part = Array.isArray(content) ? content[0] : undefined;
  const partRecord =
    typeof part === "object" && part !== null && !Array.isArray(part)
      ? (part as Record<string, unknown>)
      : undefined;
  if (
    messageRecord?.role !== "developer" ||
    partRecord?.type !== "input_text" ||
    typeof partRecord?.text !== "string"
  ) {
    throw new Error("Codex developer instruction envelope drifted.");
  }
  const first = partRecord.text.indexOf(marker);
  if (partRecord.text.indexOf(marker, first + marker.length) >= 0) {
    throw new Error("Codex repeated the frozen developer instruction.");
  }
  return true;
}

/** Deterministic two-response fixture for real Codex shell-tool probes. */
export async function startResponsesFixture(
  options: ResponsesFixtureOptions,
): Promise<ResponsesFixture> {
  if (options.bearer.trim() === "") throw new Error("Fixture bearer must be non-empty.");
  if (!/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/u.test(options.model)) {
    throw new Error("Fixture model must be a safe provider model ID.");
  }
  if (options.command.trim() === "") throw new Error("Fixture command must be non-empty.");
  if (options.instructionMarker !== undefined && options.instructionMarker.trim() === "") {
    throw new Error("Fixture instruction marker must be non-empty.");
  }

  const callId = options.callId ?? DEFAULT_CALL_ID;
  const requests: ResponsesFixtureRequest[] = [];
  let toolName = "";
  let toolOutput = "";
  let toolIssued = false;
  let completed = false;
  let instructionMarkerPresent: boolean | undefined;
  const server = createServer(async (request, response) => {
    try {
      if (request.method !== "POST" || request.url !== "/responses") {
        response.writeHead(404).end();
        return;
      }
      if (request.headers.authorization !== `Bearer ${options.bearer}`) {
        response.writeHead(401).end();
        return;
      }
      const body = await jsonBody(request);
      if (body.model !== options.model || body.stream !== true) {
        throw new Error("Codex fixture request did not preserve the frozen model and stream.");
      }
      if (options.instructionMarker !== undefined) {
        const present = hasInstructionMarker(body, options.instructionMarker);
        if (
          instructionMarkerPresent !== undefined &&
          present !== instructionMarkerPresent
        ) {
          throw new Error("Codex developer instructions changed during the fixture run.");
        }
        instructionMarkerPresent = present;
      }
      if (completed) throw new Error("Codex sent a request after fixture completion.");
      requests.push({ method: request.method, url: request.url, body });

      response.writeHead(200, {
        "content-type": "text/event-stream",
        "cache-control": "no-cache",
        connection: "close",
        "openai-model": options.model,
        "x-request-id": `provider-fixture-${requests.length}`,
      });
      const idPrefix = instructionMarkerPresent
        ? "resp_fixture_verify_instruction_"
        : "resp_fixture_";
      const id = `${idPrefix}${requests.length}`;
      event(response, { type: "response.created", response: { id, model: options.model } });
      const output = toolResult(body, callId);
      if (output === undefined) {
        if (toolIssued) throw new Error("Codex repeated the fixture turn without tool output.");
        toolName = execTool(body);
        toolIssued = true;
        event(response, {
          type: "response.output_item.done",
          item: {
            type: "function_call",
            call_id: callId,
            name: toolName,
            arguments: JSON.stringify({ cmd: options.command }),
          },
        });
      } else {
        if (!toolIssued) throw new Error("Codex returned tool output before a fixture call.");
        toolOutput = output;
        completed = true;
        event(response, {
          type: "response.output_item.done",
          item: {
            type: "message",
            role: "assistant",
            id: "msg_fixture_complete",
            content: [{ type: "output_text", text: options.finalMessage ?? "Done." }],
          },
        });
      }
      complete(response, id, options.model);
    } catch (error) {
      if (!response.headersSent) response.writeHead(500, { "content-type": "text/plain" });
      response.end(error instanceof Error ? error.message : String(error));
    }
  });

  await new Promise<void>((resolveListen, reject) => {
    server.once("error", reject);
    server.listen(options.port ?? 0, options.host ?? "127.0.0.1", resolveListen);
  });
  const address = server.address();
  if (address === null || typeof address === "string") {
    throw new Error("Responses fixture failed to bind a TCP port.");
  }
  return Object.freeze({
    baseUrl: `http://${options.host ?? "127.0.0.1"}:${address.port}`,
    snapshot: () => structuredClone({ requests, toolName, toolOutput }),
    close: () =>
      new Promise<void>((resolveClose, reject) => {
        server.close((error) => (error === undefined ? resolveClose() : reject(error)));
      }),
  });
}
