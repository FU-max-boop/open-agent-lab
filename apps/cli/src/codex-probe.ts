import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  buildCodexProbeInvocation,
  runCodexInvocation,
} from "./codex-runner.js";

const PROBE_KEY = "open-agent-lab-local-probe";
const CALL_ID = "call_open_agent_lab_probe";
const OUTPUT_FILE = "codex-probe.txt";
const OUTPUT_TEXT = "codex-native-responses\n";
const MAX_REQUEST_BYTES = 5 * 1024 * 1024;

interface ProbeRequest {
  method: string;
  url: string;
  authorization: string | undefined;
  body: Record<string, unknown>;
}

export interface CodexProbeResult {
  ok: true;
  requests: number;
  callId: string;
  tool: string;
  output: string;
  sawThreadStart: boolean;
  sawTurnComplete: boolean;
}

async function jsonBody(request: IncomingMessage): Promise<Record<string, unknown>> {
  const chunks: Buffer[] = [];
  let bytes = 0;
  for await (const chunk of request) {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    bytes += buffer.byteLength;
    if (bytes > MAX_REQUEST_BYTES) throw new Error("Codex probe request exceeded 5 MiB.");
    chunks.push(buffer);
  }
  const value: unknown = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("Codex probe request body must be a JSON object.");
  }
  return value as Record<string, unknown>;
}

function event(response: ServerResponse, value: Record<string, unknown>): void {
  response.write(`event: ${String(value.type)}\n`);
  response.write(`data: ${JSON.stringify(value)}\n\n`);
}

function complete(response: ServerResponse, id: string): void {
  event(response, {
    type: "response.completed",
    response: {
      id,
      usage: {
        input_tokens: 10,
        input_tokens_details: { cached_tokens: 0 },
        output_tokens: 5,
        output_tokens_details: { reasoning_tokens: 0 },
        total_tokens: 15,
      },
    },
  });
  response.end();
}

function functionTool(body: Record<string, unknown>): string {
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

function toolResult(body: Record<string, unknown>): string | undefined {
  if (!Array.isArray(body.input)) return undefined;
  const item = body.input.find(
    (candidate) =>
      typeof candidate === "object" &&
      candidate !== null &&
      !Array.isArray(candidate) &&
      candidate.type === "function_call_output" &&
      candidate.call_id === CALL_ID,
  );
  if (item === undefined || !("output" in item) || typeof item.output !== "string") {
    return undefined;
  }
  return item.output;
}

function probeToolCommand(): string {
  return `exposed=0
[ -n "\${OPEN_AGENT_LAB_PROBE_KEY:-}" ] && exposed=1
grep -Fq '${PROBE_KEY}' "\$CODEX_HOME/auth.json" 2>/dev/null && exposed=1
if [ -d /proc ]; then
  for file in \$(find /proc -maxdepth 2 -name environ -type f 2>/dev/null); do
    if [ -r "\$file" ] && tr '\\0' '\\n' < "\$file" 2>/dev/null | grep -Fqx 'OPEN_AGENT_LAB_PROBE_KEY=${PROBE_KEY}'; then
      exposed=1
      break
    fi
  done
fi
pid=\$PPID
while [ "\${pid:-0}" -gt 1 ] 2>/dev/null; do
  ps eww -p "\$pid" -o command= 2>/dev/null | grep -Fq 'OPEN_AGENT_LAB_PROBE_KEY=${PROBE_KEY}' && exposed=1
  [ "\$exposed" -eq 1 ] && break
  pid=\$(ps -p "\$pid" -o ppid= 2>/dev/null | tr -d ' ')
done
if [ "\$exposed" -eq 1 ]; then
  printf 'secret-exposed\\n'
else
  printf 'codex-native-responses\\n'
fi > ${OUTPUT_FILE}`;
}

export async function runCodexProbe(codexPath = "codex"): Promise<CodexProbeResult> {
  const workspace = await mkdtemp(join(tmpdir(), "open-agent-lab-codex-probe-"));
  const requests: ProbeRequest[] = [];
  let tool = "";
  let toolOutput = "";
  const server = createServer(async (request, response) => {
    try {
      const body = await jsonBody(request);
      requests.push({
        method: request.method ?? "",
        url: request.url ?? "",
        authorization: request.headers.authorization,
        body,
      });
      if (request.method !== "POST" || request.url !== "/responses") {
        response.writeHead(404).end();
        return;
      }
      if (request.headers.authorization !== `Bearer ${PROBE_KEY}`) {
        response.writeHead(401).end();
        return;
      }
      response.writeHead(200, {
        "content-type": "text/event-stream",
        "cache-control": "no-cache",
        connection: "close",
      });
      const id = `resp_probe_${requests.length}`;
      event(response, { type: "response.created", response: { id } });
      if (requests.length === 1) {
        tool = functionTool(body);
        event(response, {
          type: "response.output_item.done",
          item: {
            type: "function_call",
            call_id: CALL_ID,
            name: tool,
            arguments: JSON.stringify({
              cmd: probeToolCommand(),
            }),
          },
        });
      } else {
        const output = toolResult(body);
        if (output === undefined) throw new Error("Codex did not return the probe tool result.");
        toolOutput = output;
        event(response, {
          type: "response.output_item.done",
          item: {
            type: "message",
            role: "assistant",
            id: "msg_probe_complete",
            content: [{ type: "output_text", text: "Probe complete." }],
          },
        });
      }
      complete(response, id);
    } catch (error) {
      response.writeHead(500, { "content-type": "text/plain" });
      response.end(error instanceof Error ? error.message : String(error));
    }
  });

  let stdout = "";
  let stderr = "";
  try {
    await new Promise<void>((resolveListen, reject) => {
      server.once("error", reject);
      server.listen(0, "127.0.0.1", resolveListen);
    });
    const address = server.address();
    if (address === null || typeof address === "string") {
      throw new Error("Codex probe failed to bind a loopback port.");
    }
    const invocation = buildCodexProbeInvocation({
      workspace,
      prompt: "Use the available shell tool exactly once, then report completion.",
      baseUrl: `http://127.0.0.1:${address.port}`,
      codexPath,
    });
    const code = await runCodexInvocation(
      invocation,
      { ...process.env, OPEN_AGENT_LAB_PROBE_KEY: PROBE_KEY },
      {
        stdout: (chunk) => (stdout += chunk),
        stderr: (chunk) => (stderr += chunk),
      },
    );
    if (code !== 0) throw new Error(`Codex probe exited ${code}: ${stderr.slice(-2_000)}`);
    const output = await readFile(join(workspace, OUTPUT_FILE), "utf8").catch((error: unknown) => {
      const detail = error instanceof Error ? error.message : String(error);
      throw new Error(`Codex probe tool produced no effect: ${detail}; ${toolOutput.slice(-1_000)}`);
    });
    if (output !== OUTPUT_TEXT) {
      throw new Error(`Codex probe tool effect did not match: ${JSON.stringify(output)}.`);
    }
    if (requests.length !== 2) {
      throw new Error(`Codex probe expected 2 Responses requests, received ${requests.length}.`);
    }
    const request = requests[0];
    if (request?.body.stream !== true) throw new Error("Codex did not request a streamed response.");
    return {
      ok: true,
      requests: requests.length,
      callId: CALL_ID,
      tool,
      output: output.trimEnd(),
      sawThreadStart: stdout.includes('"type":"thread.started"'),
      sawTurnComplete: stdout.includes('"type":"turn.completed"'),
    };
  } finally {
    await new Promise<void>((resolveClose) => server.close(() => resolveClose()));
    await rm(workspace, { force: true, recursive: true });
  }
}
