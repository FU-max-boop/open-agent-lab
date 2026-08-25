import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import {
  createServer,
  request as httpRequest,
  type IncomingMessage,
  type RequestListener,
} from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { canonicalJson } from "@open-agent-lab/contracts";
import { sha256 } from "@open-agent-lab/evidence";

import {
  startNativeResponsesRelay,
  verifyRelayJournal,
  verifyRelaySeal,
  type NativeResponsesRelay,
  type NativeResponsesRelayOptions,
} from "../src/responses-relay.js";

const MODEL = "glm-5.3";
const CLIENT_BEARER = "relay-client-token-0000000000000001";
const PROVIDER_BEARER = "provider-secret-1234567890abcdef";
const TURN_STATE_HEADER = "x-codex-turn-state";
const COMPLETE_USAGE = { input_tokens: 7, output_tokens: 3, total_tokens: 10 } as const;

interface TestServer {
  url: string;
  close: () => Promise<void>;
}

async function listen(handler: RequestListener): Promise<TestServer> {
  const server = createServer(handler);
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      server.off("error", reject);
      resolve();
    });
  });
  const address = server.address();
  assert.ok(address !== null && typeof address !== "string");
  return {
    url: `http://127.0.0.1:${address.port}`,
    close: async () => {
      server.closeIdleConnections();
      await new Promise<void>((resolve, reject) => {
        server.close((error) => (error === undefined ? resolve() : reject(error)));
      });
    },
  };
}

async function body(request: IncomingMessage): Promise<Buffer> {
  const chunks: Buffer[] = [];
  for await (const chunk of request) {
    chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
  }
  return Buffer.concat(chunks);
}

function requestBody(overrides: Record<string, unknown> = {}): string {
  return JSON.stringify({ model: MODEL, stream: true, store: false, input: "probe", ...overrides });
}

async function relayRequest(
  relay: NativeResponsesRelay,
  init: RequestInit = {},
  path = "/responses",
): Promise<Response> {
  const { body: requestPayload, headers, ...rest } = init;
  return fetch(`${relay.baseUrl}${path}`, {
    method: "POST",
    ...rest,
    headers: {
      authorization: `Bearer ${CLIENT_BEARER}`,
      "content-type": "application/json",
      ...headers,
    },
    body: requestPayload ?? requestBody(),
  });
}

async function turnStateRequest(
  relay: NativeResponsesRelay,
  turnStates: string[],
  clientRequestId?: string,
): Promise<Response> {
  const payload = requestBody();
  return new Promise((resolve, reject) => {
    const request = httpRequest(
      `${relay.baseUrl}/responses`,
      {
        method: "POST",
        headers: {
          authorization: `Bearer ${CLIENT_BEARER}`,
          "content-type": "application/json",
          "content-length": Buffer.byteLength(payload),
          [TURN_STATE_HEADER]: turnStates,
          ...(clientRequestId === undefined
            ? {}
            : { "x-client-request-id": clientRequestId }),
        },
      },
      (response) => {
        void body(response).then((value) => {
          assert.ok(response.statusCode !== undefined);
          resolve(new Response(value, { status: response.statusCode }));
        }, reject);
      },
    );
    request.on("error", reject);
    request.end(payload);
  });
}

async function interruptedRelayRequest(
  relay: NativeResponsesRelay,
  onData?: (body: Buffer) => void,
): Promise<{ body: Buffer; complete: boolean; status: number }> {
  const payload = requestBody();
  return new Promise((resolve, reject) => {
    const request = httpRequest(
      `${relay.baseUrl}/responses`,
      {
        method: "POST",
        headers: {
          authorization: `Bearer ${CLIENT_BEARER}`,
          "content-type": "application/json",
          "content-length": Buffer.byteLength(payload),
        },
      },
      (response) => {
        const chunks: Buffer[] = [];
        response.on("data", (chunk: Buffer) => {
          chunks.push(chunk);
          onData?.(Buffer.concat(chunks));
        });
        response.on("error", () => undefined);
        response.once("close", () => {
          assert.ok(response.statusCode !== undefined);
          resolve({
            body: Buffer.concat(chunks),
            complete: response.complete,
            status: response.statusCode,
          });
        });
      },
    );
    request.on("error", reject);
    request.end(payload);
  });
}

async function fixture(
  t: { after: (callback: () => void | Promise<void>) => void },
  upstream: TestServer,
  overrides: Partial<NativeResponsesRelayOptions> = {},
  closeUpstream = true,
): Promise<{ relay: NativeResponsesRelay; sidecarPath: string }> {
  const directory = await mkdtemp(join(tmpdir(), "open-agent-lab-relay-test-"));
  const sidecarPath = join(directory, "relay.jsonl");
  const relay = await startNativeResponsesRelay({
    runId: "relay-test",
    providerId: "test",
    buildId: "development",
    expectedModel: MODEL,
    budgetClass: "unmetered_route_probe",
    upstreamResponsesUrl: `${upstream.url}/responses`,
    upstreamBearer: PROVIDER_BEARER,
    clientBearer: CLIENT_BEARER,
    sidecarPath,
    expiresAtMs: Date.now() + 60_000,
    ...overrides,
  });
  t.after(async () => {
    await relay.close();
    if (closeUpstream) await upstream.close();
    await rm(directory, { force: true, recursive: true });
  });
  return { relay, sidecarPath };
}

function records(content: string): Record<string, unknown>[] {
  return content
    .trimEnd()
    .split("\n")
    .map((line) => JSON.parse(line) as Record<string, unknown>);
}

function rechain(entries: Record<string, unknown>[]): string {
  let previous: string | null = null;
  return `${entries
    .map((entry) => {
      const body: Record<string, unknown> = { ...entry, previousEventSha256: previous };
      delete body.eventSha256;
      const eventSha256 = sha256(canonicalJson(body));
      previous = eventSha256;
      return canonicalJson({ ...body, eventSha256 });
    })
    .join("\n")}\n`;
}

function reseal(marker: Record<string, unknown>): string {
  const body = { ...marker };
  delete body.markerSha256;
  return `${canonicalJson({ ...body, markerSha256: sha256(canonicalJson(body)) })}\n`;
}

interface BudgetStep {
  terminal: "completed" | "incomplete";
  outputTokens: number;
  toolCall?: boolean;
}

function budgetResponse(step: BudgetStep, ordinal: number): Response {
  const item = {
    type: "function_call",
    id: `function-${ordinal}`,
    call_id: `call-${ordinal}`,
    name: "exec_command",
    arguments: '{"cmd":"true"}',
    status: "completed",
  };
  const response = {
    id: `budget-response-${ordinal}`,
    model: MODEL,
    status: step.terminal,
    ...(step.terminal === "incomplete"
      ? { incomplete_details: { reason: "max_output_tokens" } }
      : {}),
    ...(step.toolCall === true ? { output: [item] } : {}),
    usage: {
      input_tokens: 1,
      output_tokens: step.outputTokens,
      total_tokens: step.outputTokens + 1,
    },
  };
  const frames = step.toolCall === true
    ? [`data: ${JSON.stringify({ type: "response.output_item.done", item })}\n\n`]
    : [];
  frames.push(
    `data: ${JSON.stringify({ type: `response.${step.terminal}`, response })}\n\n`,
  );
  return new Response(frames.join(""), {
    status: 200,
    headers: { "content-type": "text/event-stream", "openai-model": MODEL },
  });
}

async function budgetFixture(
  t: { after: (callback: () => void | Promise<void>) => void },
  budgetClass: NativeResponsesRelayOptions["budgetClass"],
  steps: BudgetStep[],
  maxRequests = 3,
  beforeFetch?: (ordinal: number, sidecarPath: string) => void | Promise<void>,
): Promise<{
  bodies: Record<string, unknown>[];
  relay: NativeResponsesRelay;
  sidecarPath: string;
}> {
  const unused = await listen((_request, response) => response.end());
  const bodies: Record<string, unknown>[] = [];
  let sidecarPath = "";
  const result = await fixture(t, unused, {
    budgetClass,
    maxRequests,
    fetchImpl: (async (_input: string | URL | Request, init?: RequestInit) => {
      const ordinal = bodies.length + 1;
      await beforeFetch?.(ordinal, sidecarPath);
      assert.ok(init?.body instanceof Uint8Array);
      bodies.push(JSON.parse(Buffer.from(init.body).toString()) as Record<string, unknown>);
      const step = steps[bodies.length - 1];
      assert.ok(step !== undefined, "unexpected upstream fetch");
      return budgetResponse(step, bodies.length);
    }) as typeof fetch,
  });
  sidecarPath = result.sidecarPath;
  return { ...result, bodies };
}

async function sealedEvidence(
  relay: NativeResponsesRelay,
  sidecarPath: string,
): Promise<{ journal: string; summary: Awaited<ReturnType<NativeResponsesRelay["seal"]>> }> {
  const summary = await relay.seal();
  const journal = await readFile(sidecarPath, "utf8");
  assert.deepEqual(verifyRelaySeal(journal, await readFile(relay.sealPath, "utf8")), summary);
  return { journal, summary };
}

async function assertRelayError(response: Response, status: number, code: string): Promise<void> {
  assert.equal(response.status, status);
  assert.deepEqual(await response.json(), { error: { code } });
}

test("relay preserves split SSE bytes, injects only provider auth, and journals metadata", async (t) => {
  const sse = Buffer.from(
    [
      'event: response.created\ndata: {"type":"response.created","response":{"id":"resp_1","model":"glm-5.3","system_fingerprint":"fp_1"}}\n\n',
      'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"中"}\n\n',
      'event: response.completed\ndata: {"type":"response.completed","response":{"id":"resp_1","model":"glm-5.3","usage":{"input_tokens":7,"output_tokens":3,"total_tokens":10,"input_tokens_details":{"cached_tokens":2},"output_tokens_details":{"reasoning_tokens":1}}}}\n\n',
    ].join(""),
  );
  const unicodeAt = sse.indexOf(Buffer.from("中"));
  assert.ok(unicodeAt > 0);
  let observedBody: Uint8Array = new Uint8Array();
  let observedHeaders: IncomingMessage["headers"] = {};
  const upstream = await listen((request, response) => {
    void (async () => {
      observedBody = await body(request);
      observedHeaders = request.headers;
      response.writeHead(200, {
        "cache-control": "no-cache",
        "content-type": "text/event-stream",
        "openai-model": MODEL,
        "set-cookie": "must-not-cross=1",
        "x-private-provider-header": "must-not-cross",
        "x-request-id": "provider-request-1",
      });
      for (const chunk of [
        sse.subarray(0, unicodeAt + 1),
        sse.subarray(unicodeAt + 1, unicodeAt + 2),
        sse.subarray(unicodeAt + 2),
      ]) {
        response.write(chunk);
        await new Promise<void>((resolve) => setImmediate(resolve));
      }
      response.end();
    })().catch((error: unknown) => response.destroy(error instanceof Error ? error : undefined));
  });
  const { relay, sidecarPath } = await fixture(t, upstream);
  const sent = requestBody();
  const response = await relayRequest(relay, {
    body: sent,
    headers: {
      authorization: `Bearer ${CLIENT_BEARER}`,
      "content-type": "application/json; charset=utf-8",
      cookie: "attacker=1",
      "proxy-authorization": "Basic attacker",
      "x-api-key": "attacker-key",
      "x-client-request-id": "codex-turn-1",
      "x-forwarded-host": "attacker.invalid",
    },
  });

  assert.equal(response.status, 200);
  assert.deepEqual(Buffer.from(await response.arrayBuffer()), sse);
  assert.equal(response.headers.get("x-request-id"), "provider-request-1");
  assert.equal(response.headers.get("openai-model"), MODEL);
  assert.equal(response.headers.get("set-cookie"), null);
  assert.equal(response.headers.get("x-private-provider-header"), null);
  assert.deepEqual(Buffer.from(observedBody), Buffer.from(canonicalJson(JSON.parse(sent))));
  assert.equal(observedHeaders.authorization, `Bearer ${PROVIDER_BEARER}`);
  assert.equal(observedHeaders.accept, "text/event-stream");
  assert.equal(observedHeaders["accept-encoding"], "identity");
  assert.equal(observedHeaders["content-type"], "application/json");
  assert.equal(observedHeaders["x-client-request-id"], "codex-turn-1");
  for (const name of ["cookie", "proxy-authorization", "x-api-key", "x-forwarded-host"]) {
    assert.equal(observedHeaders[name], undefined);
  }

  assert.equal(verifyRelayJournal(await readFile(sidecarPath, "utf8")).eventCount, 3);
  const summary = await relay.close();
  const journal = await readFile(sidecarPath, "utf8");
  assert.deepEqual(verifyRelayJournal(journal), {
    eventCount: summary.eventCount,
    chainHead: summary.chainHead,
  });
  assert.deepEqual(
    verifyRelaySeal(journal, await readFile(relay.sealPath, "utf8")),
    summary,
  );
  const sealText = await readFile(relay.sealPath, "utf8");
  const seal = JSON.parse(sealText) as Record<string, unknown>;
  const widened = records(journal);
  widened[0] = { ...widened[0], authorization: "must-not-be-accepted" };
  assert.throws(() => verifyRelayJournal(rechain(widened)), /record/u);
  const widenedUsage = records(journal);
  widenedUsage[2] = {
    ...widenedUsage[2],
    usage: { ...(widenedUsage[2]?.usage as Record<string, unknown>), secret: 1 },
  };
  assert.throws(() => verifyRelayJournal(rechain(widenedUsage)), /record/u);
  const widenedSources = records(journal);
  widenedSources[2] = {
    ...widenedSources[2],
    modelSources: { ...(widenedSources[2]?.modelSources as object), secret: MODEL },
  };
  assert.throws(() => verifyRelayJournal(rechain(widenedSources)), /record/u);
  for (const [index, field] of [
    [0, "at"],
    [0, "clientRequestId"],
    [1, "modelHeader"],
    [2, "systemFingerprint"],
  ] as const) {
    const widenedScalar = records(journal);
    widenedScalar[index] = {
      ...widenedScalar[index],
      [field]: { authorization: "must-not-be-accepted" },
    };
    assert.throws(() => verifyRelayJournal(rechain(widenedScalar)), /record/u);
  }
  const oversizedSourceIndex = records(journal);
  oversizedSourceIndex[2] = {
    ...oversizedSourceIndex[2],
    modelSources: { "event.response.completed.response.model.999999999999999999999": MODEL },
  };
  assert.throws(() => verifyRelayJournal(rechain(oversizedSourceIndex)), /record/u);
  const unicodeSource = records(journal);
  unicodeSource[2] = {
    ...unicodeSource[2],
    modelSources: { "event.response.\u2028probe.response.model.1": MODEL },
  };
  assert.doesNotThrow(() => verifyRelayJournal(rechain(unicodeSource)));
  assert.throws(
    () => verifyRelaySeal(journal, reseal({ ...seal, authorization: "must-not-be-accepted" })),
    /marker/u,
  );
  assert.throws(
    () => verifyRelaySeal(journal, reseal({ ...seal, rejectedRequests: { unknown: 1 } })),
    /marker/u,
  );
  for (const count of [0, 2]) {
    assert.throws(
      () =>
        verifyRelaySeal(
          journal,
          reseal({ ...seal, rejectedRequests: { client_disconnected_after_close: count } }),
        ),
      /marker/u,
    );
  }
  assert.throws(
    () =>
      verifyRelaySeal(
        journal,
        reseal({ ...seal, rejectedRequests: { upstream_secret_echo: 2 } }),
      ),
    /marker/u,
  );
  const audited = reseal({
    ...seal,
    rejectedRequests: { client_disconnected_after_close: 1 },
  });
  assert.doesNotThrow(() => verifyRelaySeal(journal, audited));
  assert.throws(
    () => verifyRelaySeal(journal, audited.replace("after_close\":1", "after_close\":1.0")),
    /canonical/u,
  );
  assert.throws(() => verifyRelayJournal(journal.trimEnd()), /newline/u);
  assert.equal(summary.eventCount, 3);
  assert.deepEqual(summary.rejectedRequests, {});
  assert.ok(!journal.includes(PROVIDER_BEARER));
  assert.ok(!journal.includes(CLIENT_BEARER));
  const entries = records(journal);
  assert.equal(entries[0]?.event, "transport.responses.request");
  assert.equal(entries[0]?.clientRequestId, "codex-turn-1");
  assert.equal(entries[1]?.providerRequestId, "provider-request-1");
  assert.equal(typeof entries[1]?.headersMs, "number");
  assert.equal("firstByteMs" in (entries[1] ?? {}), false);
  assert.equal(typeof entries[2]?.firstByteMs, "number");
  assert.deepEqual(entries[2], {
    ...entries[2],
    errorCategory: null,
    event: "transport.responses.closed",
    modelConsistency: "consistent",
    parseErrors: 0,
    providerRequestId: "provider-request-1",
    responseId: "resp_1",
    returnedModel: MODEL,
    systemFingerprint: "fp_1",
    terminalEvent: "response.completed",
    transportState: "completed",
    usage: {
      cached_input_tokens: 2,
      input_tokens: 7,
      output_tokens: 3,
      reasoning_output_tokens: 1,
      total_tokens: 10,
    },
  });

  const tampered = records(journal);
  tampered[2] = { ...tampered[2], returnedModel: "attacker-model" };
  assert.throws(
    () => verifyRelayJournal(`${tampered.map((entry) => JSON.stringify(entry)).join("\n")}\n`),
    /chain mismatch at line 3/,
  );
});

test("relay forwards, queues, and omits absent per-request Codex turn state", async (t) => {
  const turnState = "s".repeat(512);
  const contenderStates = ["contender-a", "contender-b"];
  const observedTurnStates: Array<string | string[] | undefined> = [];
  let releaseActive!: () => void;
  const activeMayFinish = new Promise<void>((resolve) => {
    releaseActive = resolve;
  });
  let activeStarted!: () => void;
  const activeDidStart = new Promise<void>((resolve) => {
    activeStarted = resolve;
  });
  const upstream = await listen((request, response) => {
    const ordinal = observedTurnStates.length + 1;
    observedTurnStates.push(request.headers[TURN_STATE_HEADER]);
    void (async () => {
      if (ordinal === 2) {
        activeStarted();
        await activeMayFinish;
      }
      response.writeHead(200, {
        "content-type": "text/event-stream",
        "openai-model": MODEL,
        ...(ordinal === 1 ? { [TURN_STATE_HEADER]: turnState } : {}),
        "x-request-id": `provider-turn-${ordinal}`,
      });
      response.end(
        `data: ${JSON.stringify({
          type: "response.completed",
          response: { id: `resp-turn-${ordinal}`, model: MODEL, usage: COMPLETE_USAGE },
        })}\n\n`,
      );
    })().catch((error: unknown) =>
      response.destroy(error instanceof Error ? error : undefined),
    );
  });
  const { relay, sidecarPath } = await fixture(t, upstream);
  t.after(() => releaseActive());

  const bootstrap = await relayRequest(relay);
  assert.equal(bootstrap.headers.get(TURN_STATE_HEADER), turnState);
  await bootstrap.arrayBuffer();

  const active = relayRequest(relay, {
    headers: { [TURN_STATE_HEADER]: turnState },
  });
  await activeDidStart;
  const contenders = contenderStates.map((state) =>
    relayRequest(relay, { headers: { [TURN_STATE_HEADER]: state } }),
  );
  const rejected = await Promise.race(
    contenders.map((candidate, index) =>
      candidate.then(async (response) =>
        response.status === 429
          ? { index, response }
          : await new Promise<never>(() => undefined),
      ),
    ),
  );
  await assertRelayError(rejected.response, 429, "concurrency_exceeded");
  const queuedIndex = 1 - rejected.index;
  const queued = contenders[queuedIndex];
  assert.ok(queued !== undefined);
  releaseActive();

  for (const response of await Promise.all([active, queued])) {
    assert.equal(response.status, 200);
    await response.arrayBuffer();
  }
  const reset = await relayRequest(relay);
  assert.equal(reset.status, 200);
  await reset.arrayBuffer();

  assert.deepEqual(observedTurnStates, [
    undefined,
    turnState,
    contenderStates[queuedIndex],
    undefined,
  ]);
  await relay.close();
  const evidence = `${await readFile(sidecarPath, "utf8")}\n${await readFile(
    relay.sealPath,
    "utf8",
  )}`;
  for (const state of [turnState, ...contenderStates]) assert.ok(!evidence.includes(state));
});

test("relay rejects malformed client turn state instead of treating it as absent", async (t) => {
  const validTurnState = "s".repeat(512);
  let upstreamRequests = 0;
  let observedTurnState: string | string[] | undefined;
  const upstream = await listen((request, response) => {
    upstreamRequests += 1;
    observedTurnState = request.headers[TURN_STATE_HEADER];
    response.end();
  });
  const { relay, sidecarPath } = await fixture(t, upstream, { maxRequests: 1 });

  const malformed = [
    { name: "empty", values: [""] },
    { name: "duplicate", values: ["first", "second"] },
    { name: "ambiguous comma", values: ["first,second"] },
    { name: "htab", values: ["bad\tstate"] },
    { name: "non-ascii", values: ["é"] },
    { name: "oversized", values: ["x".repeat(513)] },
  ];
  for (const { name, values } of malformed) {
    const response = await turnStateRequest(relay, values);
    assert.equal(response.status, 400, name);
    assert.deepEqual(await response.json(), { error: { code: "invalid_turn_state" } }, name);
  }
  assert.equal(upstreamRequests, 0);

  const boundary = await turnStateRequest(relay, [validTurnState]);
  assert.equal(boundary.status, 200);
  await boundary.arrayBuffer();
  assert.equal(upstreamRequests, 1);
  assert.equal(observedTurnState, validTurnState);
  const summary = await relay.close();
  assert.deepEqual(summary.rejectedRequests, { invalid_turn_state: 6 });
  assert.deepEqual(
    verifyRelaySeal(
      await readFile(sidecarPath, "utf8"),
      await readFile(relay.sealPath, "utf8"),
    ),
    summary,
  );
});

test("relay rejects malformed upstream turn state before writing client headers", async (t) => {
  const invalidTurnStates = ["", "first,second", "bad\tstate", "é", "x".repeat(513)];
  const upstream = await listen((_request, response) => response.end());
  t.after(upstream.close);
  for (const turnState of invalidTurnStates) {
    const { relay, sidecarPath } = await fixture(
      t,
      upstream,
      {
        maxRequests: 1,
        fetchImpl: async () =>
          new Response(null, {
            status: 200,
            headers: { [TURN_STATE_HEADER]: turnState },
          }),
      },
      false,
    );
    const response = await relayRequest(relay);
    assert.equal(response.headers.get(TURN_STATE_HEADER), null);
    await assertRelayError(response, 502, "upstream_failure");
    const summary = await relay.close();
    assert.deepEqual(summary.rejectedRequests, { invalid_turn_state: 1 });
    const entries = records(await readFile(sidecarPath, "utf8"));
    const headers = entries[1];
    const closed = entries[2];
    assert.equal(headers?.status, 200);
    assert.equal(closed?.transportState, "failed");
    assert.equal(closed?.errorCategory, "upstream_failure");
    assert.equal(closed?.responseBytes, 0);
  }
});

test("relay rejects folded duplicate upstream turn state before writing headers", async (t) => {
  const upstream = await listen((_request, response) => {
    response.setHeader(TURN_STATE_HEADER, ["first", "second"]);
    response.end();
  });
  const { relay, sidecarPath } = await fixture(t, upstream, { maxRequests: 1 });

  const response = await relayRequest(relay);
  assert.equal(response.headers.get(TURN_STATE_HEADER), null);
  await assertRelayError(response, 502, "upstream_failure");

  const summary = await relay.close();
  assert.deepEqual(summary.rejectedRequests, { invalid_turn_state: 1 });
  const entries = records(await readFile(sidecarPath, "utf8"));
  assert.equal(entries[1]?.status, 200);
  assert.equal(entries[2]?.transportState, "failed");
  assert.equal(entries[2]?.errorCategory, "upstream_failure");
  assert.equal(entries[2]?.responseBytes, 0);
});

test("relay rejects upstream metadata that echoes its provider credential", async (t) => {
  const headerCases: Array<{
    status: number;
    error: string;
    headers: Record<string, string>;
    body?: string;
  }> = [
    {
      status: 200,
      error: "upstream_failure",
      headers: { "openai-model": MODEL, "x-request-id": PROVIDER_BEARER },
    },
    {
      status: 200,
      error: "upstream_failure",
      headers: {
        "openai-model": MODEL,
        "request-id": `prefix-${PROVIDER_BEARER}-suffix`,
      },
    },
    {
      status: 200,
      error: "upstream_failure",
      headers: {
        "openai-model": `prefix-${PROVIDER_BEARER}`,
        "x-request-id": "provider-request-safe",
      },
    },
    {
      status: 200,
      error: "upstream_failure",
      headers: {
        "cache-control": `${"x".repeat(513)}${PROVIDER_BEARER}`,
        "openai-model": MODEL,
        "x-request-id": "provider-request-safe",
      },
    },
    {
      status: 200,
      error: "upstream_failure",
      headers: {
        "openai-model": MODEL,
        [TURN_STATE_HEADER]: `${"x".repeat(513)}${PROVIDER_BEARER}`,
        "x-request-id": "provider-request-safe",
      },
    },
    {
      status: 204,
      error: "upstream_failure",
      headers: { "openai-model": MODEL, "x-request-id": PROVIDER_BEARER },
    },
    {
      status: 302,
      error: "upstream_failure",
      headers: { "openai-model": MODEL, "x-request-id": PROVIDER_BEARER },
    },
    {
      status: 400,
      error: "upstream_failure",
      headers: { "content-type": "application/json", "x-request-id": "safe-raw" },
      body: PROVIDER_BEARER,
    },
    {
      status: 422,
      error: "upstream_failure",
      headers: { "content-type": "application/json", "x-request-id": "safe-escaped" },
      body: JSON.stringify({ error: PROVIDER_BEARER }).replaceAll("-", "\\u002d"),
    },
  ];
  const safeResponse = {
    id: "safe-response",
    model: MODEL,
    system_fingerprint: "safe-fingerprint",
    usage: COMPLETE_USAGE,
  };
  const metadataCases = [
    {
      type: "response.completed",
      response: { ...safeResponse, id: PROVIDER_BEARER },
    },
    {
      type: "response.completed",
      response: { ...safeResponse, model: `prefix-${PROVIDER_BEARER}` },
    },
    {
      type: "response.completed",
      response: { ...safeResponse, system_fingerprint: PROVIDER_BEARER },
    },
    {
      type: "response.completed",
      response: {
        ...safeResponse,
        headers: { "openai-model": `prefix-${PROVIDER_BEARER}` },
      },
    },
    {
      type: `probe-${PROVIDER_BEARER}`,
      response: safeResponse,
    },
  ];
  let requestCount = 0;
  const upstream = await listen((_request, response) => {
    const index = requestCount;
    const headerCase = headerCases[index];
    requestCount += 1;
    const metadataCase = index - headerCases.length;
    response.writeHead(headerCase?.status ?? 200, {
      "content-type": "text/event-stream",
      ...(headerCase?.headers ?? {
        "openai-model": MODEL,
        "x-request-id": "provider-request-safe",
      }),
    });
    const event = metadataCases[metadataCase] ?? {
      type: "response.completed",
      response: safeResponse,
    };
    response.end(headerCase?.body ?? `data: ${JSON.stringify(event)}\n\n`);
  });
  t.after(upstream.close);
  for (let index = 0; index < headerCases.length + metadataCases.length; index += 1) {
    const { relay, sidecarPath } = await fixture(t, upstream, { maxRequests: 1 }, false);
    const response = await relayRequest(relay);
    const responseBody = await response.arrayBuffer().catch(() => new ArrayBuffer(0));
    assert.ok(!Buffer.from(responseBody).includes(Buffer.from(PROVIDER_BEARER)));
    assert.equal(response.headers.get("x-request-id"), null);
    assert.ok([...response.headers.values()].every((value) => !value.includes(PROVIDER_BEARER)));
    const summary = await relay.close();
    const journal = await readFile(sidecarPath, "utf8");
    const seal = await readFile(relay.sealPath, "utf8");
    assert.ok(!journal.includes(PROVIDER_BEARER));
    assert.ok(!seal.includes(PROVIDER_BEARER));
    assert.deepEqual(summary.rejectedRequests, { upstream_secret_echo: 1 });
    assert.deepEqual(verifyRelaySeal(journal, seal), summary);
    const entries = records(journal);
    const headerCase = headerCases[index];
    const headers = entries[1];
    const closed = entries[2];
    if (headerCase !== undefined) {
      assert.equal(response.status, 502);
      assert.equal(headers?.status, headerCase.status);
      assert.equal(
        headers?.providerRequestId,
        headerCase.body === undefined ? null : headerCase.headers["x-request-id"],
      );
      assert.equal(closed?.responseBytes, Buffer.byteLength(headerCase.body ?? ""));
      if (headerCase.body !== undefined) {
        assert.equal(closed?.responseSha256, sha256(headerCase.body));
      }
    }
    assert.equal(
      headers?.modelHeader,
      headerCase === undefined ? MODEL : null,
    );
    assert.equal(closed?.transportState, "failed");
    assert.equal(closed?.errorCategory, "upstream_failure");
    for (const field of [
      "responseId",
      "returnedModel",
      "systemFingerprint",
      "terminalEvent",
    ]) {
      assert.equal(closed?.[field], null);
    }
    assert.deepEqual(closed?.modelSources, {});
  }
  assert.equal(requestCount, headerCases.length + metadataCases.length);
});

test("relay fails closed on an invalid UTF-8 non-success body", async (t) => {
  const escaped = JSON.stringify(PROVIDER_BEARER).slice(1, -1).replaceAll("-", "\\u002d");
  const upstreamBody = Buffer.concat([
    Buffer.from(`{"error":{"message":"${escaped}","noise":"`),
    Buffer.from([0xff]),
    Buffer.from('"}}'),
  ]);
  const upstream = await listen((_request, response) => {
    response.writeHead(400, { "content-type": "application/json" });
    response.end(upstreamBody);
  });
  const { relay, sidecarPath } = await fixture(t, upstream, { maxRequests: 1 });

  const response = await relayRequest(relay);
  assert.equal(response.status, 502);
  const clientBody = await response.text();
  assert.deepEqual(JSON.parse(clientBody), { error: { code: "upstream_failure" } });
  assert.ok(!clientBody.includes(PROVIDER_BEARER));

  const summary = await relay.close();
  const journal = await readFile(sidecarPath, "utf8");
  const closed = records(journal)[2];
  assert.deepEqual(summary.rejectedRequests, {});
  assert.equal(closed?.transportState, "failed");
  assert.equal(closed?.errorCategory, "upstream_failure");
  assert.equal(closed?.responseBytes, upstreamBody.byteLength);
  assert.equal(closed?.responseSha256, sha256(upstreamBody));
  assert.ok(!journal.includes(PROVIDER_BEARER));
});

test("relay rejects a split raw first-frame echo before committing client headers", async (t) => {
  const malicious = Buffer.from(`: ${PROVIDER_BEARER}\r\n\r\n`);
  const splitAt = malicious.indexOf(Buffer.from(PROVIDER_BEARER)) + 13;
  const upstream = await listen((_request, response) => {
    void (async () => {
      response.writeHead(200, { "content-type": "text/event-stream" });
      response.write(malicious.subarray(0, splitAt));
      await new Promise<void>((resolve) => setImmediate(resolve));
      response.end(malicious.subarray(splitAt));
    })().catch((error: unknown) => response.destroy(error instanceof Error ? error : undefined));
  });
  const { relay, sidecarPath } = await fixture(t, upstream);

  const response = await relayRequest(relay);
  const clientBody = await response.text();
  assert.equal(response.status, 502);
  assert.deepEqual(JSON.parse(clientBody), { error: { code: "upstream_failure" } });
  assert.ok(!clientBody.includes(PROVIDER_BEARER));

  const summary = await relay.close();
  const journal = await readFile(sidecarPath, "utf8");
  const closed = records(journal)[2];
  assert.deepEqual(summary.rejectedRequests, { upstream_secret_echo: 1 });
  assert.equal(closed?.transportState, "failed");
  assert.equal(closed?.errorCategory, "upstream_failure");
  assert.equal(closed?.responseBytes, malicious.byteLength);
  assert.equal(closed?.responseSha256, sha256(malicious));
  assert.ok(!journal.includes(PROVIDER_BEARER));
});

test("relay preserves a safe prefix but drops an escaped late echo and following frame", async (t) => {
  const escaped = (value: string): string =>
    [...value].map((character) => `\\u${character.charCodeAt(0).toString(16).padStart(4, "0")}`).join("");
  const prefix = PROVIDER_BEARER.slice(0, 17);
  const suffix = PROVIDER_BEARER.slice(17);
  const added = Buffer.from(
    'data: {"type":"response.output_item.added","item":{"type":"message","role":"assistant","id":"msg","content":[]}}\n\n',
  );
  const first = Buffer.from(
    `data: {"type":"response.output_text.delta","delta":"${escaped(prefix)}"}\n\n`,
  );
  const offending = Buffer.from(
    `data: {"type":"response.output_text.delta","delta":"${escaped(suffix)}"}\n\n`,
  );
  const following = Buffer.from('data: {"type":"response.output_text.delta","delta":"after"}\n\n');
  const safePrefix = Buffer.concat([added, first]);
  const allUpstreamBytes = Buffer.concat([safePrefix, offending, following]);
  let acknowledgeSafePrefix!: () => void;
  const safePrefixDelivered = new Promise<void>((resolve) => {
    acknowledgeSafePrefix = resolve;
  });
  t.after(() => acknowledgeSafePrefix());
  const upstream = await listen((_request, response) => {
    void (async () => {
      response.writeHead(200, { "content-type": "text/event-stream" });
      response.write(safePrefix);
      await safePrefixDelivered;
      response.end(Buffer.concat([offending, following]));
    })().catch((error: unknown) => response.destroy(error instanceof Error ? error : undefined));
  });
  const { relay, sidecarPath } = await fixture(t, upstream);

  const client = await interruptedRelayRequest(relay, (body) => {
    if (body.byteLength >= safePrefix.byteLength) acknowledgeSafePrefix();
  });
  assert.equal(client.status, 200);
  assert.equal(client.complete, false);
  assert.deepEqual(client.body, safePrefix);
  assert.ok(!client.body.includes(PROVIDER_BEARER));

  const summary = await relay.close();
  const journal = await readFile(sidecarPath, "utf8");
  const closed = records(journal)[2];
  assert.deepEqual(summary.rejectedRequests, { upstream_secret_echo: 1 });
  assert.equal(closed?.transportState, "failed");
  assert.equal(closed?.errorCategory, "upstream_failure");
  assert.equal(closed?.responseBytes, allUpstreamBytes.byteLength);
  assert.equal(closed?.responseSha256, sha256(allUpstreamBytes));
  assert.ok(!journal.includes(PROVIDER_BEARER));
});

test("relay drops a malformed state-affecting frame before it can reset Codex state", async (t) => {
  const prefix = PROVIDER_BEARER.slice(0, 17);
  const suffix = PROVIDER_BEARER.slice(17);
  const added = Buffer.from(
    'data: {"type":"response.output_item.added","item":{"type":"message","role":"assistant","id":"msg","content":[]}}\n\n',
  );
  const first = Buffer.from(
    `data: ${JSON.stringify({ type: "response.output_text.delta", delta: prefix })}\n\n`,
  );
  const malformed = Buffer.from(
    'data: {"type":"response.output_item.added","item":{"type":"message","role":"assistant","phase":123,"content":[]}}\n\n',
  );
  const following = Buffer.from(
    `data: ${JSON.stringify({ type: "response.output_text.delta", delta: suffix })}\n\n`,
  );
  const safePrefix = Buffer.concat([added, first]);
  const allUpstreamBytes = Buffer.concat([safePrefix, malformed, following]);
  const upstream = await listen((_request, response) => {
    void (async () => {
      response.writeHead(200, { "content-type": "text/event-stream" });
      response.write(safePrefix);
      await new Promise<void>((resolve) => setTimeout(resolve, 20));
      response.end(Buffer.concat([malformed, following]));
    })().catch((error: unknown) => response.destroy(error instanceof Error ? error : undefined));
  });
  const { relay, sidecarPath } = await fixture(t, upstream);

  const client = await interruptedRelayRequest(relay);
  assert.equal(client.status, 200);
  assert.equal(client.complete, false);
  assert.deepEqual(client.body, safePrefix);

  const summary = await relay.close();
  const journal = await readFile(sidecarPath, "utf8");
  const closed = records(journal)[2];
  assert.deepEqual(summary.rejectedRequests, {});
  assert.equal(closed?.transportState, "failed");
  assert.equal(closed?.errorCategory, "upstream_failure");
  assert.equal(closed?.parseErrors, 1);
  assert.equal(closed?.responseBytes, allUpstreamBytes.byteLength);
  assert.equal(closed?.responseSha256, sha256(allUpstreamBytes));
  assert.ok(!journal.includes(PROVIDER_BEARER));
});

test("relay redacts a provider credential from client metadata", async (t) => {
  let upstreamRequests = 0;
  const upstream = await listen((_request, response) => {
    upstreamRequests += 1;
    response.writeHead(200, { "content-type": "text/event-stream" });
    response.end();
  });
  t.after(upstream.close);
  const attempts = [
    (relay: NativeResponsesRelay) =>
      relayRequest(relay, {
        headers: { "x-client-request-id": `turn-${PROVIDER_BEARER}-suffix` },
      }),
    (relay: NativeResponsesRelay) =>
      relayRequest(relay, { headers: { [TURN_STATE_HEADER]: `é-${PROVIDER_BEARER}` } }),
    (relay: NativeResponsesRelay) =>
      turnStateRequest(relay, ["first", "second"], `turn-${PROVIDER_BEARER}-suffix`),
  ];
  for (const attempt of attempts) {
    const { relay, sidecarPath } = await fixture(t, upstream, {}, false);
    const response = await attempt(relay);
    assert.equal(response.status, 502);
    assert.deepEqual(await response.json(), { error: { code: "upstream_failure" } });
    const summary = await relay.close();
    const journal = await readFile(sidecarPath, "utf8");
    const seal = await readFile(relay.sealPath, "utf8");
    assert.ok(!journal.includes(PROVIDER_BEARER));
    assert.ok(!seal.includes(PROVIDER_BEARER));
    assert.deepEqual(summary.rejectedRequests, { upstream_secret_echo: 1 });
    assert.deepEqual(verifyRelaySeal(journal, seal), summary);
    const [request, headers, closed] = records(journal);
    assert.equal(request?.clientRequestId, null);
    assert.deepEqual(
      [headers?.status, headers?.providerRequestId, headers?.modelHeader],
      [null, null, null],
    );
    assert.deepEqual(
      [closed?.status, closed?.providerRequestId, closed?.responseId],
      [null, null, null],
    );
    assert.equal(closed?.transportState, "failed");
    assert.equal(closed?.errorCategory, "upstream_failure");
    assert.equal(closed?.responseBytes, 0);
  }
  assert.equal(upstreamRequests, 0);
});

test("relay rejects short or non-visible provider credentials before listening", async (t) => {
  const upstream = await listen((_request, response) => response.end());
  t.after(upstream.close);
  const directory = await mkdtemp(join(tmpdir(), "open-agent-lab-relay-secret-test-"));
  t.after(() => rm(directory, { force: true, recursive: true }));
  for (const [index, upstreamBearer] of [
    "event",
    "x".repeat(31),
    `${"x".repeat(32)}\n`,
    `${"x".repeat(16)}\n${"x".repeat(16)}`,
    `${"x".repeat(32)}\u0000`,
    `${"x".repeat(32)}\u007f`,
  ].entries()) {
    await assert.rejects(
      startNativeResponsesRelay({
        runId: `invalid-secret-${index}`,
        providerId: "test",
        buildId: "development",
        expectedModel: MODEL,
        budgetClass: "unmetered_route_probe",
        upstreamResponsesUrl: `${upstream.url}/responses`,
        upstreamBearer,
        clientBearer: CLIENT_BEARER,
        sidecarPath: join(directory, `${index}.jsonl`),
        expiresAtMs: Date.now() + 60_000,
      }),
      /upstreamBearer is invalid/u,
    );
  }
});

test("relay normalizes ambiguous request JSON before forwarding", async (t) => {
  let observedBody: Uint8Array = new Uint8Array();
  const upstream = await listen((request, response) => {
    void body(request).then((value) => {
      observedBody = value;
      response.writeHead(200, { "content-type": "text/event-stream" });
      response.end(
        `data: {"type":"response.completed","response":{"id":"normalized","model":"${MODEL}","usage":{"input_tokens":7,"output_tokens":3,"total_tokens":10}}}\n\n`,
      );
    });
  });
  const { relay } = await fixture(t, upstream);
  const ambiguous = `{"model":"other","model":"${MODEL}","stream":false,"stream":true,"store":true,"store":false,"input":"probe"}`;

  const response = await relayRequest(relay, { body: ambiguous });
  assert.equal(response.status, 200);
  await response.text();
  assert.deepEqual(JSON.parse(Buffer.from(observedBody).toString()), {
    input: "probe",
    model: MODEL,
    store: false,
    stream: true,
  });
  assert.equal(Buffer.from(observedBody).toString().includes("other"), false);
});

test("scored relay debits 30k and injects exactly 20k on the continuation", async (t) => {
  const { relay, sidecarPath, bodies } = await budgetFixture(
    t,
    "scored_slot",
    [
      { terminal: "completed", outputTokens: 30_000 },
      { terminal: "completed", outputTokens: 1 },
    ],
    2,
    async (ordinal, journalPath) => {
      const persisted = records(await readFile(journalPath, "utf8"));
      assert.equal(persisted.length, (ordinal - 1) * 3 + 1);
      assert.equal(persisted.at(-1)?.event, "transport.responses.request");
      assert.equal(persisted.at(-1)?.ordinal, ordinal);
    },
  );
  for (let ordinal = 1; ordinal <= 2; ordinal += 1) {
    const response = await relayRequest(relay, {
      body: requestBody({
        input: `turn-${ordinal}`,
        ...(ordinal === 2 ? { max_output_tokens: 30_000 } : {}),
      }),
    });
    assert.equal(response.status, 200);
    await response.text();
  }
  assert.deepEqual(bodies.map((value) => value.max_output_tokens), [50_000, 20_000]);

  const { journal, summary } = await sealedEvidence(relay, sidecarPath);
  const entries = records(journal);
  assert.deepEqual(
    [
      entries[0]?.requestedMaxOutputTokens,
      entries[0]?.effectiveMaxOutputTokens,
      entries[3]?.requestedMaxOutputTokens,
      entries[3]?.effectiveMaxOutputTokens,
    ],
    [null, 50_000, 30_000, 20_000],
  );
  assert.equal(entries[3]?.requestSha256, sha256(canonicalJson(bodies[1])));
  assert.deepEqual(summary.outputTokenAccounting, {
    state: "complete",
    reportedOutputTokens: 30_001,
    conservativeOutputTokenUpperBound: 30_001,
    unusedOutputTokensBurned: 19_999,
  });
});

test("invalid scored maxima are local 400 rejections with no upstream fetch", async (t) => {
  for (const invalid of [false, 0, -1, 1.5, 2 ** 53]) {
    const { relay, sidecarPath, bodies } = await budgetFixture(t, "scored_slot", []);
    await assertRelayError(
      await relayRequest(relay, { body: requestBody({ max_output_tokens: invalid }) }),
      400,
      "invalid_max_output_tokens",
    );
    assert.equal(bodies.length, 0);
    const { journal, summary } = await sealedEvidence(relay, sidecarPath);
    assert.equal(journal, "");
    assert.deepEqual(summary.rejectedRequests, { invalid_max_output_tokens: 1 });
    assert.deepEqual(summary.outputTokenAccounting, {
      state: "poisoned",
      reportedOutputTokens: null,
      conservativeOutputTokenUpperBound: 0,
      unusedOutputTokensBurned: 50_000,
    });
  }
});

test("post-injection request overflow poisons as known-zero before fetch", async (t) => {
  const upstream = await listen((_request, response) => response.end());
  let fetches = 0;
  const clientBody = requestBody();
  const { relay, sidecarPath } = await fixture(t, upstream, {
    budgetClass: "scored_slot",
    maxRequestBytes: Buffer.byteLength(canonicalJson(JSON.parse(clientBody))),
    fetchImpl: (async () => {
      fetches += 1;
      throw new Error("unexpected upstream fetch");
    }) as typeof fetch,
  });

  await assertRelayError(await relayRequest(relay, { body: clientBody }), 413, "request_too_large");
  assert.equal(fetches, 0);
  const { journal, summary } = await sealedEvidence(relay, sidecarPath);
  assert.equal(journal, "");
  assert.deepEqual(summary.outputTokenAccounting, {
    state: "poisoned",
    reportedOutputTokens: null,
    conservativeOutputTokenUpperBound: 0,
    unusedOutputTokensBurned: 50_000,
  });
});

test("exact scored exhaustion rejects a real tool-output continuation before fetch", async (t) => {
  const { relay, sidecarPath, bodies } = await budgetFixture(
    t,
    "scored_slot",
    [{ terminal: "completed", outputTokens: 50_000, toolCall: true }],
    1,
  );
  const first = await relayRequest(relay);
  assert.equal(first.status, 200);
  assert.match(await first.text(), /"type":"function_call"/u);

  const continuation = await relayRequest(relay, {
    body: requestBody({
      input: [{ type: "function_call_output", call_id: "call-1", output: "ok" }],
    }),
  });
  await assertRelayError(continuation, 429, "slot_output_budget_exhausted");
  assert.equal(bodies.length, 1);

  const { journal, summary } = await sealedEvidence(relay, sidecarPath);
  assert.equal(records(journal).length, 3);
  assert.deepEqual(summary.rejectedRequests, { slot_output_budget_exhausted: 1 });
  assert.deepEqual(summary.outputTokenAccounting, {
    state: "exact_exhaustion",
    reportedOutputTokens: 50_000,
    conservativeOutputTokenUpperBound: 50_000,
    unusedOutputTokensBurned: 0,
  });
});

test("request quota clears its unjournaled scored admission before sealing", async (t) => {
  const { relay, sidecarPath, bodies } = await budgetFixture(
    t,
    "scored_slot",
    [{ terminal: "completed", outputTokens: 3 }],
    1,
  );
  const first = await relayRequest(relay);
  assert.equal(first.status, 200);
  await first.text();
  await assertRelayError(await relayRequest(relay), 429, "request_quota_exceeded");
  assert.equal(bodies.length, 1);

  const { journal, summary } = await sealedEvidence(relay, sidecarPath);
  assert.equal(records(journal).length, 3);
  assert.deepEqual(summary.rejectedRequests, { request_quota_exceeded: 1 });
  assert.deepEqual(summary.outputTokenAccounting, {
    state: "poisoned",
    reportedOutputTokens: null,
    conservativeOutputTokenUpperBound: 3,
    unusedOutputTokensBurned: 49_997,
  });
});

test("a non-tool request after exact usage cannot use the exhaustion exception", async (t) => {
  const { relay, sidecarPath, bodies } = await budgetFixture(
    t,
    "scored_slot",
    [{ terminal: "completed", outputTokens: 50_000 }],
    2,
  );
  const first = await relayRequest(relay);
  assert.equal(first.status, 200);
  await first.text();
  await assertRelayError(await relayRequest(relay), 429, "request_quota_exceeded");
  assert.equal(bodies.length, 1);

  const { summary } = await sealedEvidence(relay, sidecarPath);
  assert.deepEqual(summary.rejectedRequests, { request_quota_exceeded: 1 });
  assert.deepEqual(summary.outputTokenAccounting, {
    state: "poisoned",
    reportedOutputTokens: null,
    conservativeOutputTokenUpperBound: 50_000,
    unusedOutputTokensBurned: 0,
  });
});

test("exact exhaustion rejects forged, mismatched, or mixed tool continuations", async (t) => {
  const cases = [
    {
      toolCall: false,
      input: [{ type: "function_call_output", call_id: "call-1", output: "ok" }],
    },
    {
      toolCall: true,
      input: [{ type: "function_call_output", call_id: "invented", output: "ok" }],
    },
    {
      toolCall: true,
      input: [
        { type: "function_call_output", call_id: "call-1", output: "ok" },
        { role: "user", content: "continue" },
      ],
    },
    {
      toolCall: true,
      input: [{ type: "custom_tool_call_output", call_id: "call-1", output: "ok" }],
    },
    {
      toolCall: true,
      input: [{ type: "function_call_output", call_id: "call-1" }],
    },
  ] as const;
  for (const value of cases) {
    const { relay, sidecarPath, bodies } = await budgetFixture(
      t,
      "scored_slot",
      [{ terminal: "completed", outputTokens: 50_000, toolCall: value.toolCall }],
      2,
    );
    const first = await relayRequest(relay);
    assert.equal(first.status, 200);
    await first.text();
    await assertRelayError(
      await relayRequest(relay, { body: requestBody({ input: value.input }) }),
      429,
      "request_quota_exceeded",
    );
    assert.equal(bodies.length, 1);
    const { summary } = await sealedEvidence(relay, sidecarPath);
    assert.deepEqual(summary.rejectedRequests, { request_quota_exceeded: 1 });
    assert.equal(summary.outputTokenAccounting.state, "poisoned");
  }
});

test("scored relay accepts only the sealed max-output incomplete budget terminal", async (t) => {
  const { relay, sidecarPath, bodies } = await budgetFixture(
    t,
    "scored_slot",
    [{ terminal: "incomplete", outputTokens: 200 }],
    1,
  );
  const response = await relayRequest(relay, {
    body: requestBody({ max_output_tokens: 256 }),
  });
  assert.equal(response.status, 200);
  await response.text();
  assert.equal(bodies[0]?.max_output_tokens, 256);

  const { journal, summary } = await sealedEvidence(relay, sidecarPath);
  const [request, , closed] = records(journal);
  assert.deepEqual(
    [request?.requestedMaxOutputTokens, request?.effectiveMaxOutputTokens],
    [256, 256],
  );
  assert.deepEqual(
    [closed?.terminalEvent, closed?.terminalStatus, closed?.incompleteReason],
    ["response.incomplete", "incomplete", "max_output_tokens"],
  );
  assert.deepEqual(summary.outputTokenAccounting, {
    state: "budget_terminal",
    reportedOutputTokens: 200,
    conservativeOutputTokenUpperBound: 200,
    unusedOutputTokensBurned: 49_800,
  });
});

test("ZAI relay fixes 8192/256 allocations, never transfers round one, and blocks round three", async (t) => {
  const run = async (sendThird: boolean) => {
    const harness = await budgetFixture(
      t,
      "zai_route_probe",
      [
        { terminal: "completed", outputTokens: 1 },
        { terminal: "incomplete", outputTokens: 200 },
      ],
      3,
    );
    const first = await relayRequest(harness.relay);
    assert.equal(first.status, 200);
    await first.text();
    const second = await relayRequest(harness.relay, {
      body: requestBody({ input: "round-two", max_output_tokens: 9_999 }),
    });
    assert.equal(second.status, 200);
    await second.text();
    assert.deepEqual(harness.bodies.map((value) => value.max_output_tokens), [8_192, 256]);
    if (sendThird) {
      await assertRelayError(
        await relayRequest(harness.relay, { body: requestBody({ input: "round-three" }) }),
        429,
        "request_quota_exceeded",
      );
      assert.equal(harness.bodies.length, 2);
    }
    return { ...(await sealedEvidence(harness.relay, harness.sidecarPath)), bodies: harness.bodies };
  };

  const conformant = await run(false);
  assert.deepEqual(conformant.summary.rejectedRequests, {});
  assert.deepEqual(conformant.summary.outputTokenAccounting, {
    state: "probe_conformant",
    reportedOutputTokens: 201,
    conservativeOutputTokenUpperBound: 201,
    unusedOutputTokensBurned: 8_247,
  });
  const third = await run(true);
  assert.deepEqual(third.summary.rejectedRequests, { request_quota_exceeded: 1 });
  assert.equal(third.summary.outputTokenAccounting.state, "poisoned");
});

test("closed evidence is durable before EOF permits the next Codex turn", async (t) => {
  let turn = 0;
  let finishFirst!: () => void;
  const firstMayEnd = new Promise<void>((resolve) => {
    finishFirst = resolve;
  });
  const upstream = await listen((_request, response) => {
    void (async () => {
      turn += 1;
      const current = turn;
      response.writeHead(200, {
        "content-type": "text/event-stream",
        "x-request-id": `provider-turn-${current}`,
      });
      response.write(
        `data: {"type":"response.completed","response":{"id":"response-${current}","model":"${MODEL}","usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2}}}\n\n`,
      );
      if (current === 1) await firstMayEnd;
      response.end();
    })().catch((error: unknown) => response.destroy(error instanceof Error ? error : undefined));
  });
  const { relay, sidecarPath } = await fixture(t, upstream, { maxRequests: 2 });

  const first = await relayRequest(relay);
  assert.equal(first.status, 200);
  const reader = first.body?.getReader();
  assert.ok(reader !== undefined);
  let streamed = "";
  while (!streamed.includes("response.completed")) {
    const chunk = await reader.read();
    assert.equal(chunk.done, false);
    streamed += Buffer.from(chunk.value).toString();
  }

  const secondPending = relayRequest(relay);
  await new Promise<void>((resolve) => setImmediate(resolve));
  finishFirst();
  const second = await secondPending;
  assert.equal(second.status, 200);
  await second.text();
  while (!(await reader.read()).done) {}

  assert.equal(records(await readFile(sidecarPath, "utf8")).length, 6);
  assert.deepEqual((await relay.seal()).rejectedRequests, {});
});

test("invalid requests and exhausted quota never reach the upstream", async (t) => {
  let upstreamRequests = 0;
  const upstream = await listen((_request, response) => {
    upstreamRequests += 1;
    response.writeHead(200, { "content-type": "text/event-stream" });
    response.end('data: {"type":"response.completed","response":{"model":"glm-5.3","usage":{"input_tokens":7,"output_tokens":3,"total_tokens":10}}}\n\n');
  });
  const { relay } = await fixture(t, upstream, {
    maxRequests: 1,
    maxRequestBytes: 128,
  });

  const cases: Array<[Response, number, string]> = [
    [
      await relayRequest(relay, { headers: { authorization: "Bearer wrong-token" } }),
      401,
      "unauthorized",
    ],
    [await relayRequest(relay, {}, "/not-responses"), 404, "not_found"],
    [await relayRequest(relay, { body: requestBody({ model: "other-model" }) }), 400, "model_mismatch"],
    [
      await relayRequest(relay, { headers: { "content-type": "text/plain" } }),
      415,
      "unsupported_content_type",
    ],
    [
      await relayRequest(relay, { headers: { "content-type": "application/jsonfoo" } }),
      415,
      "unsupported_content_type",
    ],
    [await relayRequest(relay, { body: "not-json" }), 400, "invalid_json"],
    [
      await relayRequest(relay, {
        body: `{"model":"${MODEL}","stream":true,"store":false,"input":1e400}`,
      }),
      400,
      "invalid_json",
    ],
    [
      await relayRequest(relay, { body: requestBody({ stream: false }) }),
      400,
      "unsupported_response_mode",
    ],
    [
      await relayRequest(relay, { body: requestBody({ store: true }) }),
      400,
      "unsupported_response_mode",
    ],
    [
      await relayRequest(relay, {
        body: `{"model":"${MODEL}","stream":true,"store":false,"input":[1e20,1e20,1e20,1e20]}`,
      }),
      413,
      "request_too_large",
    ],
    [
      await relayRequest(relay, { body: requestBody({ input: "x".repeat(256) }) }),
      413,
      "request_too_large",
    ],
  ];
  for (const [response, status, code] of cases) {
    await assertRelayError(response, status, code);
    assert.equal(upstreamRequests, 0);
  }

  const accepted = await relayRequest(relay);
  assert.equal(accepted.status, 200);
  await accepted.text();
  assert.equal(upstreamRequests, 1);
  await assertRelayError(await relayRequest(relay), 429, "request_quota_exceeded");
  assert.equal(upstreamRequests, 1);
  assert.deepEqual((await relay.seal()).rejectedRequests, {
    invalid_json: 2,
    model_mismatch: 1,
    request_quota_exceeded: 1,
    request_too_large: 2,
    unsupported_content_type: 2,
    unsupported_response_mode: 2,
  });
});

test("relay never follows provider redirects", async (t) => {
  let responsesRequests = 0;
  let redirectedRequests = 0;
  const upstream = await listen((request, response) => {
    if (request.url === "/redirected") {
      redirectedRequests += 1;
      response.end("unexpected");
      return;
    }
    responsesRequests += 1;
    response.writeHead(307, { location: "/redirected" });
    response.end();
  });
  const { relay } = await fixture(t, upstream);

  await assertRelayError(await relayRequest(relay), 502, "upstream_redirect");
  assert.equal(responsesRequests, 1);
  assert.equal(redirectedRequests, 0);
});

test("metadata conflicts are sticky and incomplete lifecycles are rejected", async (t) => {
  const upstream = await listen((_request, response) => {
    response.writeHead(200, {
      "content-type": "text/event-stream",
      "x-request-id": "provider-conflict",
    });
    response.end(
      [
        `data: {"type":"response.created","response":{"id":"one","model":"${MODEL}","usage":{"input_tokens":1}}}\r\r`,
        'data: {"type":"response.completed","response":{"id":"two","model":"other","usage":{"input_tokens":2}}}\r\r',
      ].join(""),
    );
  });
  const { relay, sidecarPath } = await fixture(t, upstream);
  const response = await relayRequest(relay);
  assert.equal(response.status, 200);
  await response.text();

  const journal = await readFile(sidecarPath, "utf8");
  const closed = records(journal)[2];
  assert.equal(closed?.modelConsistency, "conflict");
  assert.equal(closed?.returnedModel, null);
  assert.equal(closed?.responseId, null);
  assert.deepEqual(closed?.metadataConflicts, ["model", "response_id", "usage"]);
  assert.deepEqual(closed?.usage, { input_tokens: 2 });
  assert.throws(
    () => verifyRelayJournal(`${journal.trimEnd().split("\n").slice(0, 2).join("\n")}\n`),
    /incomplete lifecycle/,
  );
});

test("sealing aborts an in-flight stream and writes one complete lifecycle", async (t) => {
  let upstreamStarted!: () => void;
  const started = new Promise<void>((resolve) => {
    upstreamStarted = resolve;
  });
  const upstream = await listen((_request, response) => {
    response.writeHead(200, {
      "content-type": "text/event-stream",
      "openai-model": MODEL,
      "x-request-id": "provider-hanging",
    });
    response.write(`data: {"type":"response.created","response":{"id":"hang","model":"${MODEL}"}}\n\n`);
    upstreamStarted();
  });
  const { relay, sidecarPath } = await fixture(t, upstream);
  const pending = Promise.allSettled([
    relayRequest(relay).then(async (response) => response.text()),
  ]);
  await started;
  const summary = await relay.seal();
  const [outcome] = await pending;
  assert.equal(outcome?.status, "rejected");

  const journal = await readFile(sidecarPath, "utf8");
  assert.equal(summary.eventCount, 3);
  assert.equal(records(journal)[2]?.transportState, "failed");
  assert.deepEqual(
    verifyRelaySeal(journal, await readFile(relay.sealPath, "utf8")),
    summary,
  );
  await assert.rejects(relayRequest(relay));
});

test("the bounded successor queue rejects a third flight and seal never hangs", async (t) => {
  let upstreamStarted!: () => void;
  const started = new Promise<void>((resolve) => {
    upstreamStarted = resolve;
  });
  const upstream = await listen((_request, response) => {
    response.writeHead(200, {
      "content-type": "text/event-stream",
      "x-request-id": "provider-queued-seal",
    });
    response.write(
      `data: {"type":"response.created","response":{"id":"queued","model":"${MODEL}"}}\n\n`,
    );
    upstreamStarted();
  });
  const { relay } = await fixture(t, upstream);
  const first = Promise.allSettled([
    relayRequest(relay).then(async (response) => response.text()),
  ]);
  await started;
  const contenderA = relayRequest(relay);
  const contenderB = relayRequest(relay);
  const rejected = await Promise.race([
    contenderA.then(async (response) =>
      response.status === 429
        ? { response, queued: contenderB }
        : await new Promise<never>(() => undefined),
    ),
    contenderB.then(async (response) =>
      response.status === 429
        ? { response, queued: contenderA }
        : await new Promise<never>(() => undefined),
    ),
  ]);
  await assertRelayError(rejected.response, 429, "concurrency_exceeded");
  const queued = Promise.allSettled([
    rejected.queued.then(async (response) => response.text()),
  ]);
  const summary = await relay.seal();
  await Promise.all([first, queued]);

  assert.equal(summary.eventCount, 3);
  assert.deepEqual(summary.rejectedRequests, {
    concurrency_exceeded: 1,
    relay_sealed: 1,
  });
});

test("upstream timeouts and response cap fail closed with complete evidence", async (t) => {
  const oversized = await listen((_request, response) => {
    response.writeHead(200, { "content-type": "text/event-stream" });
    response.end("x".repeat(64));
  });
  const capped = await fixture(t, oversized, { maxResponseBytes: 32 });
  await assertRelayError(await relayRequest(capped.relay), 502, "response_too_large");
  assert.equal(records(await readFile(capped.sidecarPath, "utf8"))[2]?.errorCategory, "response_too_large");

  const unused = await listen((_request, response) => response.end());
  const timed = await fixture(t, unused, {
    connectTimeoutMs: 10,
    fetchImpl: ((_input: string | URL | Request, init?: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => reject(new Error("aborted")), {
          once: true,
        });
      })) as typeof fetch,
  });
  await assertRelayError(await relayRequest(timed.relay), 504, "upstream_connect_timeout");
  assert.equal(
    records(await readFile(timed.sidecarPath, "utf8"))[2]?.errorCategory,
    "upstream_connect_timeout",
  );

  const stalled = await listen((_request, response) => {
    response.writeHead(200, { "content-type": "text/event-stream" });
    response.flushHeaders();
  });
  const idle = await fixture(t, stalled, { idleTimeoutMs: 10 });
  await assertRelayError(await relayRequest(idle.relay), 504, "upstream_idle_timeout");
  assert.equal(
    records(await readFile(idle.sidecarPath, "utf8"))[2]?.errorCategory,
    "upstream_idle_timeout",
  );
});
