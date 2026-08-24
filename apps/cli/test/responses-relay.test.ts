import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import {
  createServer,
  type IncomingMessage,
  type RequestListener,
} from "node:http";
import { createConnection } from "node:net";
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

async function rawRelayRequest(
  relay: NativeResponsesRelay,
  extraHeaders: string[],
): Promise<{ body: string; status: number }> {
  const endpoint = new URL(relay.baseUrl);
  const payload = requestBody();
  const path = `${endpoint.pathname.replace(/\/$/u, "")}/responses`;
  const request = [
    `POST ${path} HTTP/1.1`,
    `Host: ${endpoint.host}`,
    `Authorization: Bearer ${CLIENT_BEARER}`,
    "Content-Type: application/json",
    `Content-Length: ${Buffer.byteLength(payload)}`,
    "Connection: close",
    ...extraHeaders,
    "",
    payload,
  ].join("\r\n");

  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    const socket = createConnection({
      host: endpoint.hostname,
      port: Number(endpoint.port),
    });
    socket.on("connect", () => socket.write(request));
    socket.on("data", (chunk: Buffer) => chunks.push(chunk));
    socket.on("error", reject);
    socket.on("end", () => {
      const response = Buffer.concat(chunks).toString("utf8");
      const status = /^HTTP\/1\.1 (\d{3})/u.exec(response)?.[1];
      if (status === undefined) {
        reject(new Error("Relay returned a malformed HTTP response."));
        return;
      }
      resolve({
        body: response.slice(response.indexOf("\r\n\r\n") + 4),
        status: Number(status),
      });
    });
  });
}

async function fixture(
  t: { after: (callback: () => void | Promise<void>) => void },
  upstream: TestServer,
  overrides: Partial<NativeResponsesRelayOptions> = {},
): Promise<{ relay: NativeResponsesRelay; sidecarPath: string }> {
  const directory = await mkdtemp(join(tmpdir(), "open-agent-lab-relay-test-"));
  const sidecarPath = join(directory, "relay.jsonl");
  const relay = await startNativeResponsesRelay({
    runId: "relay-test",
    providerId: "test",
    buildId: "development",
    expectedModel: MODEL,
    upstreamResponsesUrl: `${upstream.url}/responses`,
    upstreamBearer: PROVIDER_BEARER,
    clientBearer: CLIENT_BEARER,
    sidecarPath,
    expiresAtMs: Date.now() + 60_000,
    ...overrides,
  });
  t.after(async () => {
    await relay.close();
    await upstream.close();
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
  assert.equal(observedHeaders[TURN_STATE_HEADER], undefined);
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

test("relay replays Codex turn state only when the client returns it", async (t) => {
  const turnState = "s".repeat(512);
  const observedTurnStates: Array<string | string[] | undefined> = [];
  const observedUnlistedHeaders: Array<string | string[] | undefined> = [];
  let requestCount = 0;
  const upstream = await listen((request, response) => {
    const ordinal = requestCount + 1;
    requestCount = ordinal;
    observedTurnStates.push(request.headers[TURN_STATE_HEADER]);
    observedUnlistedHeaders.push(request.headers["x-unlisted-client-header"]);
    const responseId = `resp-turn-${ordinal}`;
    response.writeHead(200, {
      "content-type": "text/event-stream",
      "openai-model": MODEL,
      "x-request-id": `provider-turn-${ordinal}`,
      ...(ordinal === 1 ? { [TURN_STATE_HEADER]: turnState } : {}),
    });
    response.end(
      [
        `event: response.created\ndata: {"type":"response.created","response":{"id":"${responseId}","model":"${MODEL}"}}\n\n`,
        `event: response.completed\ndata: {"type":"response.completed","response":{"id":"${responseId}","model":"${MODEL}","usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2}}}\n\n`,
      ].join(""),
    );
  });
  const { relay, sidecarPath } = await fixture(t, upstream);

  const first = await relayRequest(relay);
  assert.equal(first.status, 200);
  assert.equal(first.headers.get(TURN_STATE_HEADER), turnState);
  await first.arrayBuffer();

  const second = await relayRequest(relay, {
    headers: {
      [TURN_STATE_HEADER]: turnState,
      "x-unlisted-client-header": "must-not-cross",
    },
  });
  assert.equal(second.status, 200);
  await second.arrayBuffer();

  const third = await relayRequest(relay);
  assert.equal(third.status, 200);
  await third.arrayBuffer();

  assert.deepEqual(observedTurnStates, [undefined, turnState, undefined]);
  assert.deepEqual(observedUnlistedHeaders, [undefined, undefined, undefined]);
  await relay.close();
  assert.ok(!(await readFile(sidecarPath, "utf8")).includes(turnState));
  assert.ok(!(await readFile(relay.sealPath, "utf8")).includes(turnState));
});

test("queued requests keep their own Codex turn state", async (t) => {
  const firstTurnState = "first-turn-state";
  const secondTurnState = "second-turn-state";
  const observedTurnStates: Array<string | string[] | undefined> = [];
  let releaseFirst!: () => void;
  const firstMayFinish = new Promise<void>((resolve) => {
    releaseFirst = resolve;
  });
  let firstStarted!: () => void;
  const firstDidStart = new Promise<void>((resolve) => {
    firstStarted = resolve;
  });
  const upstream = await listen((request, response) => {
    const ordinal = observedTurnStates.length + 1;
    observedTurnStates.push(request.headers[TURN_STATE_HEADER]);
    void (async () => {
      if (ordinal === 1) {
        firstStarted();
        await firstMayFinish;
      }
      response.writeHead(200, {
        "content-type": "text/event-stream",
        "openai-model": MODEL,
        "x-request-id": `provider-queued-turn-${ordinal}`,
      });
      response.end(
        `data: {"type":"response.completed","response":{"id":"resp-queued-${ordinal}","model":"${MODEL}"}}\n\n`,
      );
    })().catch((error: unknown) =>
      response.destroy(error instanceof Error ? error : undefined),
    );
  });
  const { relay } = await fixture(t, upstream);

  const first = relayRequest(relay, {
    headers: { [TURN_STATE_HEADER]: firstTurnState },
  });
  await firstDidStart;
  const second = relayRequest(relay, {
    headers: { [TURN_STATE_HEADER]: secondTurnState },
  });
  releaseFirst();

  const [firstResponse, secondResponse] = await Promise.all([first, second]);
  assert.equal(firstResponse.status, 200);
  assert.equal(secondResponse.status, 200);
  await Promise.all([firstResponse.arrayBuffer(), secondResponse.arrayBuffer()]);
  assert.deepEqual(observedTurnStates, [firstTurnState, secondTurnState]);
});

test("relay rejects malformed client turn state instead of treating it as absent", async (t) => {
  const validTurnState = "s".repeat(512);
  let upstreamRequests = 0;
  let observedTurnState: string | string[] | undefined;
  const upstream = await listen((request, response) => {
    upstreamRequests += 1;
    observedTurnState = request.headers[TURN_STATE_HEADER];
    response.writeHead(200, {
      "content-type": "text/event-stream",
      "openai-model": MODEL,
      "x-request-id": "provider-turn-boundary",
    });
    response.end(
      `data: {"type":"response.completed","response":{"id":"resp-boundary","model":"${MODEL}"}}\n\n`,
    );
  });
  const { relay, sidecarPath } = await fixture(t, upstream, { maxRequests: 1 });

  const malformed = [
    { name: "empty", headers: [`${TURN_STATE_HEADER}:`] },
    {
      name: "duplicate",
      headers: [`${TURN_STATE_HEADER}: first`, `${TURN_STATE_HEADER}: second`],
    },
    { name: "control", headers: [`${TURN_STATE_HEADER}: bad\u0001state`] },
    { name: "oversized", headers: [`${TURN_STATE_HEADER}: ${"x".repeat(513)}`] },
  ];
  for (const { name, headers } of malformed) {
    const response = await rawRelayRequest(relay, headers);
    assert.equal(response.status, 400, name);
    if (name !== "control") assert.match(response.body, /invalid_turn_state/u, name);
  }
  assert.equal(upstreamRequests, 0);

  const boundary = await rawRelayRequest(relay, [
    `${TURN_STATE_HEADER}: ${validTurnState}`,
  ]);
  assert.equal(boundary.status, 200);
  assert.equal(upstreamRequests, 1);
  assert.equal(observedTurnState, validTurnState);
  const summary = await relay.close();
  assert.deepEqual(summary.rejectedRequests, { invalid_turn_state: 3 });
  assert.deepEqual(
    verifyRelaySeal(
      await readFile(sidecarPath, "utf8"),
      await readFile(relay.sealPath, "utf8"),
    ),
    summary,
  );
});

test("relay rejects malformed upstream turn state before writing client headers", async (t) => {
  const invalidTurnStates = ["", "bad\u0001state", "x".repeat(513)];
  let responseIndex = 0;
  const upstream = await listen((_request, response) => response.end());
  const { relay, sidecarPath } = await fixture(t, upstream, {
    maxRequests: invalidTurnStates.length,
    fetchImpl: async () => {
      const turnState = invalidTurnStates[responseIndex];
      responseIndex += 1;
      assert.ok(turnState !== undefined);
      return new Response(
        `data: {"type":"response.completed","response":{"id":"resp-invalid-turn","model":"${MODEL}"}}\n\n`,
        {
          status: 200,
          headers: {
            "content-type": "text/event-stream",
            "openai-model": MODEL,
            [TURN_STATE_HEADER]: turnState,
            "x-request-id": `provider-invalid-turn-${responseIndex}`,
          },
        },
      );
    },
  });

  for (const _turnState of invalidTurnStates) {
    const response = await relayRequest(relay);
    assert.equal(response.headers.get(TURN_STATE_HEADER), null);
    await assertRelayError(response, 502, "upstream_failure");
  }
  assert.equal(responseIndex, invalidTurnStates.length);

  await relay.close();
  const entries = records(await readFile(sidecarPath, "utf8"));
  for (let offset = 0; offset < entries.length; offset += 3) {
    const headers = entries[offset + 1];
    const closed = entries[offset + 2];
    assert.equal(headers?.status, 200);
    assert.equal(closed?.transportState, "failed");
    assert.equal(closed?.errorCategory, "upstream_failure");
    assert.equal(closed?.responseBytes, 0);
  }
});

test("relay rejects upstream metadata that echoes its provider credential", async (t) => {
  const headerCases: Array<{
    status: number;
    error: string;
    headers: Record<string, string>;
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
        [TURN_STATE_HEADER]: `prefix-${PROVIDER_BEARER}-suffix`,
        "x-request-id": "provider-request-safe",
      },
    },
    {
      status: 204,
      error: "upstream_body_missing",
      headers: { "openai-model": MODEL, "x-request-id": PROVIDER_BEARER },
    },
    {
      status: 302,
      error: "upstream_redirect",
      headers: { "openai-model": MODEL, "x-request-id": PROVIDER_BEARER },
    },
  ];
  const safeResponse = {
    id: "safe-response",
    model: MODEL,
    system_fingerprint: "safe-fingerprint",
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
    response.end(`data: ${JSON.stringify(event)}\n\n`);
  });
  const { relay, sidecarPath } = await fixture(t, upstream, {
    maxRequests: headerCases.length + metadataCases.length,
  });

  for (const headerCase of headerCases) {
    const response = await relayRequest(relay);
    assert.equal(response.status, 502);
    const errorBody = await response.text();
    assert.ok(!errorBody.includes(PROVIDER_BEARER));
    assert.deepEqual(JSON.parse(errorBody), { error: { code: headerCase.error } });
    assert.ok([...response.headers.values()].every((value) => !value.includes(PROVIDER_BEARER)));
  }
  for (const _case of metadataCases) {
    const parsedEcho = await relayRequest(relay);
    await parsedEcho.arrayBuffer().catch(() => new ArrayBuffer(0));
    assert.ok([...parsedEcho.headers.values()].every((value) => !value.includes(PROVIDER_BEARER)));
  }

  const summary = await relay.close();
  const journal = await readFile(sidecarPath, "utf8");
  const seal = await readFile(relay.sealPath, "utf8");
  assert.ok(!journal.includes(PROVIDER_BEARER));
  assert.ok(!seal.includes(PROVIDER_BEARER));
  assert.deepEqual(summary.rejectedRequests, {
    upstream_secret_echo: headerCases.length + metadataCases.length,
  });
  assert.deepEqual(verifyRelaySeal(journal, seal), summary);
  const entries = records(journal);
  for (let index = 0; index < headerCases.length; index += 1) {
    const headerCase = headerCases[index]!;
    const headers = entries[index * 3 + 1];
    assert.equal(headers?.status, headerCase.status);
    assert.equal(headers?.providerRequestId, null);
    assert.equal(headers?.modelHeader, null);
    const closed = entries[index * 3 + 2];
    assert.equal(closed?.responseBytes, 0);
    assert.equal(closed?.errorCategory, headerCase.error);
  }
  const expectedErrors = [
    ...headerCases.map(({ error }) => error),
    ...metadataCases.map(() => "upstream_failure"),
  ];
  for (let index = 0; index < headerCases.length + metadataCases.length; index += 1) {
    const closed = entries[index * 3 + 2];
    assert.equal(closed?.transportState, "failed");
    assert.equal(closed?.errorCategory, expectedErrors[index]);
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
});

test("relay redacts a provider credential from client metadata", async (t) => {
  let upstreamRequests = 0;
  const upstream = await listen((_request, response) => {
    upstreamRequests += 1;
    response.writeHead(200, { "content-type": "text/event-stream" });
    response.end();
  });
  const { relay, sidecarPath } = await fixture(t, upstream);

  for (const header of ["x-client-request-id", TURN_STATE_HEADER]) {
    const response = await relayRequest(relay, {
      headers: { [header]: `turn-${PROVIDER_BEARER}-suffix` },
    });
    assert.equal(response.status, 502);
    assert.deepEqual(await response.json(), { error: { code: "upstream_failure" } });
  }
  assert.equal(upstreamRequests, 0);

  const summary = await relay.close();
  const journal = await readFile(sidecarPath, "utf8");
  const seal = await readFile(relay.sealPath, "utf8");
  assert.ok(!journal.includes(PROVIDER_BEARER));
  assert.ok(!seal.includes(PROVIDER_BEARER));
  assert.deepEqual(summary.rejectedRequests, { upstream_secret_echo: 2 });
  assert.deepEqual(verifyRelaySeal(journal, seal), summary);
  const entries = records(journal);
  assert.equal(entries.length, 6);
  for (let offset = 0; offset < entries.length; offset += 3) {
    const [request, headers, closed] = entries.slice(offset, offset + 3);
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
        `data: {"type":"response.completed","response":{"id":"normalized","model":"${MODEL}"}}\n\n`,
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
    response.end('data: {"type":"response.completed","response":{"model":"glm-5.3"}}\n\n');
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
  await assert.rejects(
    relayRequest(capped.relay).then(async (response) => response.text()),
  );
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
  await assert.rejects(
    relayRequest(idle.relay).then(async (response) => response.text()),
  );
  assert.equal(
    records(await readFile(idle.sidecarPath, "utf8"))[2]?.errorCategory,
    "upstream_idle_timeout",
  );
});
