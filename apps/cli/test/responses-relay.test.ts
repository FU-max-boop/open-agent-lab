import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import {
  createServer,
  type IncomingMessage,
  type RequestListener,
} from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { canonicalJson } from "@open-agent-lab/contracts";

import {
  startNativeResponsesRelay,
  verifyRelayJournal,
  verifyRelaySeal,
  type NativeResponsesRelay,
  type NativeResponsesRelayOptions,
} from "../src/responses-relay.js";

const MODEL = "glm-5.3";
const CLIENT_BEARER = "relay-client-token-0000000000000001";
const PROVIDER_BEARER = "provider-secret";

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
  assert.equal(summary.eventCount, 3);
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
    request_too_large: 1,
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
  const pending = relayRequest(relay).then(async (response) => response.text());
  await started;
  const summary = await relay.seal();
  await assert.rejects(pending);

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
  const first = relayRequest(relay).then(async (response) => response.text());
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
  const summary = await relay.seal();
  await Promise.allSettled([
    first,
    rejected.queued.then(async (response) => response.text()),
  ]);

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
  const cappedResponse = await relayRequest(capped.relay);
  await assert.rejects(cappedResponse.text());
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
  const idleResponse = await relayRequest(idle.relay);
  await assert.rejects(idleResponse.text());
  assert.equal(
    records(await readFile(idle.sidecarPath, "utf8"))[2]?.errorCategory,
    "upstream_idle_timeout",
  );
});
