import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  buildCodexProbeInvocation,
  runCodexInvocation,
} from "./codex-runner.js";
import {
  startNativeResponsesRelay,
  verifyRelaySeal,
  type NativeResponsesRelay,
} from "./responses-relay.js";
import { startResponsesFixture } from "./responses-fixture.js";

const PROBE_KEY = "open-agent-lab-local-probe-00000000";
const UPSTREAM_PROBE_KEY = "open-agent-lab-upstream-probe-0000";
const PROBE_MODEL = "open-agent-lab-probe";
const CALL_ID = "call_open_agent_lab_probe";
const OUTPUT_FILE = "codex-probe.txt";
const OUTPUT_TEXT = "codex-native-responses\n";
export interface CodexProbeResult {
  ok: true;
  requests: number;
  callId: string;
  tool: string;
  output: string;
  sawThreadStart: boolean;
  sawTurnComplete: boolean;
  sawDeveloperInstruction: boolean;
}

function codexEventTypes(stdout: string): Set<string> {
  const types = new Set<string>();
  for (const [index, line] of stdout.split(/\r?\n/u).entries()) {
    if (line === "") continue;
    let event: unknown;
    try {
      event = JSON.parse(line);
    } catch {
      throw new Error(`Codex probe emitted invalid JSONL at line ${index + 1}.`);
    }
    if (typeof event !== "object" || event === null || Array.isArray(event)) {
      throw new Error(`Codex probe emitted invalid JSONL event at line ${index + 1}.`);
    }
    const type = (event as Record<string, unknown>).type;
    if (typeof type !== "string") {
      throw new Error(`Codex probe emitted an untyped JSONL event at line ${index + 1}.`);
    }
    types.add(type);
  }
  return types;
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

export async function runCodexProbe(
  codexPath = "codex",
  throughRelay = false,
  developerInstruction?: string,
): Promise<CodexProbeResult> {
  const workspace = await mkdtemp(join(tmpdir(), "open-agent-lab-codex-probe-"));
  const sidecarPath = join(workspace, "provider-metadata.ndjson");
  let relay: NativeResponsesRelay | undefined;
  const fixture = await startResponsesFixture({
    bearer: throughRelay ? UPSTREAM_PROBE_KEY : PROBE_KEY,
    model: PROBE_MODEL,
    command: probeToolCommand(),
    finalMessage: "Probe complete.",
    callId: CALL_ID,
    ...(developerInstruction === undefined ? {} : { instructionMarker: developerInstruction }),
  });

  let stdout = "";
  let stderr = "";
  try {
    if (throughRelay) {
      relay = await startNativeResponsesRelay({
        runId: "codex-relay-probe",
        providerId: "probe",
        buildId: "development",
        expectedModel: PROBE_MODEL,
        upstreamResponsesUrl: `${fixture.baseUrl}/responses`,
        upstreamBearer: UPSTREAM_PROBE_KEY,
        clientBearer: PROBE_KEY,
        sidecarPath,
        expiresAtMs: Date.now() + 60_000,
      });
    }
    const invocation = buildCodexProbeInvocation({
      workspace,
      prompt: "Use the available shell tool exactly once, then report completion.",
      baseUrl: relay?.baseUrl ?? fixture.baseUrl,
      codexPath,
      ...(developerInstruction === undefined ? {} : { developerInstruction }),
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
    const snapshot = fixture.snapshot();
    const output = await readFile(join(workspace, OUTPUT_FILE), "utf8").catch((error: unknown) => {
      const detail = error instanceof Error ? error.message : String(error);
      throw new Error(
        `Codex probe tool produced no effect: ${detail}; ${snapshot.toolOutput.slice(-1_000)}`,
      );
    });
    if (output !== OUTPUT_TEXT) {
      throw new Error(`Codex probe tool effect did not match: ${JSON.stringify(output)}.`);
    }
    if (snapshot.requests.length !== 2) {
      throw new Error(
        `Codex probe expected 2 Responses requests, received ${snapshot.requests.length}.`,
      );
    }
    const request = snapshot.requests[0];
    if (request?.body.stream !== true) {
      throw new Error("Codex did not request a streamed response.");
    }
    const paths =
      developerInstruction === undefined
        ? []
        : instructionPaths(request?.body, developerInstruction);
    const input = request?.body.input;
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
    const sawDeveloperInstruction =
      developerInstruction !== undefined &&
      paths.length === 1 &&
      paths[0] === "$.input[0].content[0].text" &&
      messageRecord?.role === "developer" &&
      partRecord?.type === "input_text" &&
      typeof partRecord.text === "string" &&
      partRecord.text.split(developerInstruction).length === 2;
    if (developerInstruction !== undefined && !sawDeveloperInstruction) {
      throw new Error(
        "Codex did not forward the exact developer instruction at the expected path; " +
          `found ${JSON.stringify(paths)}.`,
      );
    }
    const eventTypes = codexEventTypes(stdout);
    if (!eventTypes.has("thread.started")) {
      throw new Error("Codex probe did not observe thread.started.");
    }
    if (!eventTypes.has("turn.completed")) {
      throw new Error("Codex probe did not observe turn.completed.");
    }
    return {
      ok: true,
      requests: snapshot.requests.length,
      callId: CALL_ID,
      tool: snapshot.toolName,
      output: output.trimEnd(),
      sawThreadStart: true,
      sawTurnComplete: true,
      sawDeveloperInstruction,
    };
  } finally {
    if (relay !== undefined) {
      const summary = await relay.close();
      const journal = await readFile(sidecarPath, "utf8");
      verifyRelaySeal(journal, await readFile(relay.sealPath, "utf8"));
      if (summary.eventCount !== 6) {
        throw new Error(`Relay probe expected 6 metadata events, received ${summary.eventCount}.`);
      }
    }
    await fixture.close();
    await rm(workspace, { force: true, recursive: true });
  }
}
