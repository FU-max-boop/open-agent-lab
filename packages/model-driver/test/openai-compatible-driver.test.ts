import assert from "node:assert/strict";
import test from "node:test";

import {
  ModelContractError,
  OpenAICompatibleDriver,
  collectModelStream,
  type ModelCapabilities,
  type ModelRequest,
} from "../src/index.js";

const CAPABILITIES: ModelCapabilities = {
  text: true,
  image: false,
  tools: true,
  parallelTools: false,
  strictSchema: false,
  reasoning: true,
  context: 1_000_000,
  output: 128_000,
};

interface CapturedCall {
  url: string;
  init: RequestInit;
  body: Record<string, unknown>;
}

function fakeFetch(respond: (call: CapturedCall) => Response): {
  fetch: typeof fetch;
  calls: CapturedCall[];
} {
  const calls: CapturedCall[] = [];
  const fetcher: typeof fetch = async (input, init = {}) => {
    const body =
      typeof init.body === "string"
        ? (JSON.parse(init.body) as Record<string, unknown>)
        : {};
    const call = { url: String(input), init, body };
    calls.push(call);
    return respond(call);
  };
  return { fetch: fetcher, calls };
}

function jsonResponse(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: {
      "content-type": "application/json",
      "x-request-id": "request-1",
    },
  });
}

function completionPayload(
  message: Record<string, unknown>,
  finishReason = "stop",
): Record<string, unknown> {
  return {
    id: "response-1",
    model: "routed-model",
    object: "chat.completion",
    created: 1,
    choices: [
      {
        index: 0,
        logprobs: null,
        finish_reason: finishReason,
        message,
      },
    ],
    usage: { prompt_tokens: 3, completion_tokens: 2, total_tokens: 5 },
  };
}

function driver(
  dialect: "glm" | "deepseek",
  fetcher: typeof fetch,
  apiKey = "top-secret-key",
): OpenAICompatibleDriver {
  return new OpenAICompatibleDriver({
    driverId: `${dialect}-test`,
    dialect,
    baseUrl:
      dialect === "glm"
        ? "https://open.bigmodel.cn/api/paas/v4/"
        : "https://api.deepseek.com",
    model: dialect === "glm" ? "glm-exact" : "deepseek-exact",
    apiKey,
    capabilities: CAPABILITIES,
    fetch: fetcher,
  });
}

test("GLM tool rounds preserve reasoning while omitting local metadata", async () => {
  const server = fakeFetch(() =>
    jsonResponse({
      id: "response-1",
      request_id: "request-1",
      model: "glm-routed-2026-08-22",
      object: "chat.completion",
      created: 1,
      system_fingerprint: "fp-glm",
      choices: [
        {
          index: 0,
          logprobs: null,
          finish_reason: "tool_calls",
          message: {
            role: "assistant",
            content: null,
            reasoning_content: "inspect",
            tool_calls: [
              {
                id: "call-2",
                type: "function",
                function: { name: "read_file", arguments: '{"path":"b.txt"}' },
              },
            ],
          },
        },
      ],
      usage: {
        prompt_tokens: 20,
        completion_tokens: 8,
        total_tokens: 28,
        prompt_tokens_details: { cached_tokens: 5 },
        completion_tokens_details: { reasoning_tokens: 3 },
      },
    }),
  );
  const request: ModelRequest = {
    messages: [
      { role: "user", content: [{ type: "text", text: "inspect" }] },
      {
        role: "assistant",
        content: [
          { type: "reasoning", text: "need file" },
          {
            type: "tool_call",
            callId: "call-1",
            name: "read_file",
            arguments: { path: "a.txt" },
          },
        ],
      },
      {
        role: "tool",
        content: [{ type: "tool_result", callId: "call-1", content: "hello" }],
      },
    ],
    tools: [{ name: "read_file", inputSchema: { type: "object" } }],
    parallelToolCalls: false,
    reasoning: { enabled: true, effort: "high" },
    generation: { maxOutputTokens: 2_000 },
    metadata: { privateTrace: "must-stay-local" },
  };

  const result = await collectModelStream(
    driver("glm", server.fetch).stream(request),
  );

  assert.equal(server.calls.length, 1);
  const call = server.calls[0]!;
  assert.equal(
    call.url,
    "https://open.bigmodel.cn/api/paas/v4/chat/completions",
  );
  assert.equal(
    new Headers(call.init.headers).get("authorization"),
    "Bearer top-secret-key",
  );
  assert.equal(call.init.redirect, "error");
  assert.equal(call.body.stream, false);
  assert.equal(call.body.n, undefined);
  assert.equal(call.body.tool_choice, undefined);
  assert.equal(call.body.metadata, undefined);
  assert.deepEqual(call.body.thinking, {
    type: "enabled",
    clear_thinking: false,
  });
  assert.equal(call.body.reasoning_effort, "high");
  assert.equal(call.body.parallel_tool_calls, undefined);
  assert.deepEqual((call.body.messages as Record<string, unknown>[])[0], {
    role: "user",
    content: "inspect",
  });
  assert.deepEqual((call.body.messages as Record<string, unknown>[])[1], {
    role: "assistant",
    content: "",
    reasoning_content: "need file",
    tool_calls: [
      {
        id: "call-1",
        type: "function",
        function: { name: "read_file", arguments: '{"path":"a.txt"}' },
      },
    ],
  });
  assert.deepEqual(result.responseInfo, {
    responseId: "response-1",
    providerRequestId: "request-1",
    model: "glm-routed-2026-08-22",
    systemFingerprint: "fp-glm",
  });
  assert.equal(result.reasoning, "inspect");
  assert.deepEqual(result.toolCalls[0], {
    index: 0,
    callId: "call-2",
    name: "read_file",
    arguments: { path: "b.txt" },
  });
  assert.deepEqual(result.usage, {
    inputTokens: 20,
    outputTokens: 8,
    totalTokens: 28,
    cachedInputTokens: 5,
    reasoningTokens: 3,
  });
});

test("DeepSeek text completions normalize reasoning, usage, and actual identity", async () => {
  const server = fakeFetch(() =>
    jsonResponse({
      id: "response-1",
      request_id: "request-1",
      model: "deepseek-routed-2026-08-22",
      object: "chat.completion",
      created: 1,
      system_fingerprint: "fp-deepseek",
      choices: [
        {
          index: 0,
          logprobs: null,
          finish_reason: "stop",
          message: {
            role: "assistant",
            content: "done",
            reasoning_content: "think",
          },
        },
      ],
      usage: {
        prompt_tokens: 11,
        completion_tokens: 4,
        total_tokens: 15,
        prompt_cache_hit_tokens: 2,
      },
    }),
  );
  const result = await collectModelStream(
    driver("deepseek", server.fetch).stream({
      messages: [{ role: "user", content: [{ type: "text", text: "answer" }] }],
      reasoning: { enabled: true },
    }),
  );

  assert.equal(server.calls[0]!.body.stream, false);
  assert.equal(server.calls[0]!.body.stream_options, undefined);
  assert.deepEqual(server.calls[0]!.body.thinking, { type: "enabled" });
  assert.deepEqual(
    (server.calls[0]!.body.messages as Record<string, unknown>[])[0],
    { role: "user", content: "answer" },
  );
  assert.equal(result.reasoning, "think");
  assert.equal(result.text, "done");
  assert.deepEqual(result.responseInfo, {
    responseId: "response-1",
    providerRequestId: "request-1",
    model: "deepseek-routed-2026-08-22",
    systemFingerprint: "fp-deepseek",
  });
  assert.deepEqual(result.usage, {
    inputTokens: 11,
    outputTokens: 4,
    totalTokens: 15,
    cachedInputTokens: 2,
  });
});

test("DeepSeek tool history uses its documented thinking integration shape", async () => {
  const server = fakeFetch(() =>
    jsonResponse(
      completionPayload({
        role: "assistant",
        content: "done",
        reasoning_content: "verified",
      }),
    ),
  );
  const result = await collectModelStream(
    driver("deepseek", server.fetch).stream({
      messages: [
        { role: "user", content: [{ type: "text", text: "inspect" }] },
        {
          role: "assistant",
          content: [
            { type: "reasoning", text: "need file" },
            {
              type: "tool_call",
              callId: "call-1",
              name: "read_file",
              arguments: { path: "a.txt" },
            },
          ],
        },
        {
          role: "tool",
          content: [
            { type: "tool_result", callId: "call-1", content: "hello" },
          ],
        },
      ],
      tools: [{ name: "read_file", inputSchema: { type: "object" } }],
    }),
  );

  const body = server.calls[0]!.body;
  assert.equal(body.stream, false);
  assert.equal(body.tool_choice, undefined);
  assert.deepEqual(body.thinking, { type: "enabled" });
  assert.deepEqual((body.messages as Record<string, unknown>[])[1], {
    role: "assistant",
    content: "",
    reasoning_content: "need file",
    tool_calls: [
      {
        id: "call-1",
        type: "function",
        function: { name: "read_file", arguments: '{"path":"a.txt"}' },
      },
    ],
  });
  assert.equal(result.text, "done");
});

test("unsupported portable controls fail before network I/O", async () => {
  const server = fakeFetch(() => jsonResponse({}));
  const live = driver("glm", server.fetch);
  const base: ModelRequest = {
    messages: [{ role: "user", content: [{ type: "text", text: "go" }] }],
  };
  const reasoningHistory: ModelRequest["messages"] = [
    {
      role: "assistant",
      content: [{ type: "reasoning", text: "prior thought" }],
    },
  ];
  const cases: readonly ModelRequest[] = [
    { ...base, toolChoice: "required" },
    {
      ...base,
      toolChoice: "future" as NonNullable<ModelRequest["toolChoice"]>,
    },
    {
      messages: [
        {
          role: "bogus" as ModelRequest["messages"][number]["role"],
          content: [
            { type: "tool_result", callId: "call-1", content: "unsafe" },
          ],
        },
      ],
    },
    {
      messages: [
        {
          role: "assistant",
          content: [
            {
              type: "tool_call",
              callId: "call-1",
              name: "read_file",
              arguments: { path: "a" },
            },
            {
              type: "tool_call",
              callId: "call-2",
              name: "read_file",
              arguments: { path: "b" },
            },
          ],
        },
      ],
    },
    {
      messages: [
        {
          role: "tool",
          content: [
            { type: "tool_result", callId: "call-1", content: "one" },
            { type: "tool_result", callId: "call-2", content: "two" },
          ],
        },
      ],
    },
    {
      ...base,
      responseSchema: { name: "answer", schema: { type: "object" } },
    },
    { ...base, generation: { temperature: 1.01 } },
    { ...base, generation: { topP: 0.009 } },
    { ...base, generation: { stop: ["1", "2", "3", "4", "5"] } },
    { messages: reasoningHistory, reasoning: { enabled: false } },
    { messages: reasoningHistory, generation: { temperature: 0.5 } },
  ];

  for (const request of cases) {
    await assert.rejects(
      collectModelStream(live.stream(request)),
      (error: unknown) =>
        error instanceof ModelContractError && error.code === "invalid_request",
    );
  }
  const imageDriver = new OpenAICompatibleDriver({
    driverId: "image-test",
    dialect: "glm",
    baseUrl: "https://open.bigmodel.cn/api/paas/v4",
    model: "glm-vision-exact",
    apiKey: "top-secret-key",
    capabilities: { ...CAPABILITIES, image: true },
    fetch: server.fetch,
  });
  await assert.rejects(
    collectModelStream(
      imageDriver.stream({
        messages: [
          {
            role: "user",
            content: [
              {
                type: "image",
                source: { type: "url", url: "http://127.0.0.1" },
              },
            ],
          },
        ],
      }),
    ),
    (error: unknown) =>
      error instanceof ModelContractError && error.code === "invalid_request",
  );
  assert.equal(server.calls.length, 0);
});

test("provider failures are sanitized and never retried inside the driver", async () => {
  const secret = "key-that-must-not-leak";
  const server = fakeFetch(() =>
    jsonResponse(
      {
        error: { message: `bad ${secret}`, code: secret },
      },
      500,
    ),
  );
  const result = await collectModelStream(
    driver("deepseek", server.fetch, secret).stream({
      messages: [{ role: "user", content: [{ type: "text", text: "go" }] }],
    }),
  );

  assert.equal(server.calls.length, 1);
  assert.deepEqual(result.error, {
    type: "error",
    code: "http_500",
    message: "Provider request failed with HTTP 500.",
    retryable: true,
  });
  assert.equal(JSON.stringify(result).includes(secret), false);

  for (const [status, code, retryable] of [
    [401, "http_401", false],
    [429, "rate_limited", true],
  ] as const) {
    const rejected = fakeFetch(() =>
      jsonResponse(
        {
          error: { message: secret, code: secret },
        },
        status,
      ),
    );
    const failure = await collectModelStream(
      driver("glm", rejected.fetch, secret).stream({
        messages: [{ role: "user", content: [{ type: "text", text: "go" }] }],
      }),
    );
    assert.equal(rejected.calls.length, 1);
    assert.equal(failure.error?.code, code);
    assert.equal(failure.error?.retryable, retryable);
    assert.equal(JSON.stringify(failure).includes(secret), false);
  }
});

test("only the configured credential crosses the fetch boundary", async () => {
  const names = [
    "OPENAI_CUSTOM_HEADERS",
    "OPENAI_LOG",
    "OPENAI_ORG_ID",
    "OPENAI_PROJECT_ID",
  ] as const;
  const previous = new Map(names.map((name) => [name, process.env[name]]));
  const debug: unknown[] = [];
  const originalDebug = console.debug;
  try {
    process.env.OPENAI_CUSTOM_HEADERS =
      "X-Secret: environment-secret\nAuthorization: Bearer wrong-key\n" +
      "Accept: text/event-stream\nContent-Type: text/plain";
    process.env.OPENAI_LOG = "debug";
    process.env.OPENAI_ORG_ID = "private-org";
    process.env.OPENAI_PROJECT_ID = "private-project";
    console.debug = (...values: unknown[]) => {
      debug.push(values);
    };

    const server = fakeFetch(() =>
      jsonResponse(
        completionPayload({
          role: "assistant",
          content: "ok",
        }),
      ),
    );
    await collectModelStream(
      driver("glm", server.fetch).stream({
        messages: [{ role: "user", content: [{ type: "text", text: "go" }] }],
      }),
    );

    const headers = new Headers(server.calls[0]!.init.headers);
    assert.equal(headers.get("authorization"), "Bearer top-secret-key");
    assert.equal(headers.get("accept"), "application/json");
    assert.equal(headers.get("content-type"), "application/json");
    assert.equal(headers.get("openai-organization"), null);
    assert.equal(headers.get("openai-project"), null);
    assert.equal(headers.get("x-secret"), null);
    assert.deepEqual(debug, []);
  } finally {
    console.debug = originalDebug;
    for (const name of names) {
      const value = previous.get(name);
      if (value === undefined) delete process.env[name];
      else process.env[name] = value;
    }
  }
});

test("invalid credentials fail generically before fetch", () => {
  const secret = "bad-key\nprivate-suffix";
  const server = fakeFetch(() => jsonResponse({}));
  assert.throws(
    () => driver("glm", server.fetch, secret),
    (error: unknown) =>
      error instanceof ModelContractError &&
      error.code === "invalid_configuration" &&
      !error.message.includes(secret),
  );
  assert.equal(server.calls.length, 0);

  const previous = process.env.OPENAI_CUSTOM_HEADERS;
  const envSecret = "private-env-secret";
  try {
    process.env.OPENAI_CUSTOM_HEADERS = `bad header ${envSecret}: value`;
    assert.throws(
      () => driver("glm", server.fetch),
      (error: unknown) =>
        error instanceof ModelContractError &&
        error.code === "invalid_configuration" &&
        !error.message.includes(envSecret),
    );
  } finally {
    if (previous === undefined) delete process.env.OPENAI_CUSTOM_HEADERS;
    else process.env.OPENAI_CUSTOM_HEADERS = previous;
  }
});

test("an in-flight abort remains an abort and is never retried", async () => {
  let markStarted: (() => void) | undefined;
  const started = new Promise<void>((resolve) => {
    markStarted = resolve;
  });
  let calls = 0;
  const fetcher: typeof fetch = async (_input, init = {}) => {
    calls += 1;
    markStarted?.();
    return new Promise<Response>((_resolve, reject) => {
      const abort = () => reject(new DOMException("aborted", "AbortError"));
      if (init.signal?.aborted === true) abort();
      else init.signal?.addEventListener("abort", abort, { once: true });
    });
  };
  const controller = new AbortController();
  const result = collectModelStream(
    driver("deepseek", fetcher).stream(
      {
        messages: [{ role: "user", content: [{ type: "text", text: "go" }] }],
      },
      { signal: controller.signal },
    ),
  );

  await started;
  controller.abort();
  await assert.rejects(
    result,
    (error: unknown) =>
      error instanceof ModelContractError && error.code === "aborted",
  );
  assert.equal(calls, 1);
});

test("tool output is constrained to the definitions actually sent", async () => {
  const base: ModelRequest = {
    messages: [{ role: "user", content: [{ type: "text", text: "inspect" }] }],
    tools: [{ name: "read_file", inputSchema: { type: "object" } }],
    reasoning: { enabled: true },
  };
  const cases = [
    { name: "delete_file", reasoning: "unsafe", request: base },
    {
      name: "read_file",
      reasoning: "unsafe",
      request: { ...base, toolChoice: "none" as const },
    },
    { name: "read_file", reasoning: undefined, request: base },
  ] as const;

  for (const item of cases) {
    const server = fakeFetch(() =>
      jsonResponse(
        completionPayload(
          {
            role: "assistant",
            content: "",
            ...(item.reasoning === undefined
              ? {}
              : { reasoning_content: item.reasoning }),
            tool_calls: [
              {
                id: "call-1",
                type: "function",
                function: { name: item.name, arguments: "{}" },
              },
            ],
          },
          "tool_calls",
        ),
      ),
    );
    await assert.rejects(
      collectModelStream(driver("deepseek", server.fetch).stream(item.request)),
      (error: unknown) =>
        error instanceof ModelContractError && error.code === "invalid_stream",
    );
  }
});

test("malformed success payloads and resource exhaustion fail closed", async () => {
  const request: ModelRequest = {
    messages: [{ role: "user", content: [{ type: "text", text: "go" }] }],
  };
  const malformed = [
    {
      ...completionPayload({ role: "assistant", content: "ok" }),
      choices: [{ index: 0, finish_reason: "stop" }],
    },
    {
      ...completionPayload({ role: "assistant", content: "ok" }),
      usage: undefined,
    },
    completionPayload({ role: "user", content: "not an assistant response" }),
  ];
  for (const payload of malformed) {
    const server = fakeFetch(() => jsonResponse(payload));
    await assert.rejects(
      collectModelStream(driver("glm", server.fetch).stream(request)),
      (error: unknown) =>
        error instanceof ModelContractError && error.code === "invalid_stream",
    );
  }

  const exhausted = fakeFetch(() =>
    jsonResponse(
      completionPayload(
        {
          role: "assistant",
          content: "partial",
        },
        "insufficient_system_resource",
      ),
    ),
  );
  const result = await collectModelStream(
    driver("deepseek", exhausted.fetch).stream(request),
  );
  assert.deepEqual(result.error, {
    type: "error",
    code: "insufficient_system_resource",
    message: "Provider reported insufficient system resources.",
    retryable: true,
  });
  assert.equal(result.text, "");

  const sentinel = "sentinel-private-response";
  const invalidJson: typeof fetch = async () =>
    new Response(`{${sentinel}`, {
      headers: { "content-type": "application/json" },
    });
  const protocolError = await collectModelStream(
    driver("glm", invalidJson).stream(request),
  );
  assert.deepEqual(protocolError.error, {
    type: "error",
    code: "provider_protocol_error",
    message: "Provider response could not be decoded.",
    retryable: false,
  });
  assert.equal(JSON.stringify(protocolError).includes(sentinel), false);
});

test("GLM terminal failures never masquerade as successful output", async () => {
  const request: ModelRequest = {
    messages: [{ role: "user", content: [{ type: "text", text: "go" }] }],
  };
  const completion = (reason: string) =>
    fakeFetch(() =>
      jsonResponse(
        completionPayload(
          { role: "assistant", content: "partial-private-output" },
          reason,
        ),
      ),
    );

  const sensitive = completion("sensitive");
  const filtered = await collectModelStream(
    driver("glm", sensitive.fetch).stream(request),
  );
  assert.equal(filtered.text, "");
  assert.deepEqual(filtered.finish, {
    type: "finish",
    reason: "content_filter",
    providerReason: "sensitive",
  });

  const network = completion("network_error");
  const interrupted = await collectModelStream(
    driver("glm", network.fetch).stream(request),
  );
  assert.equal(interrupted.text, "");
  assert.deepEqual(interrupted.error, {
    type: "error",
    code: "network_error",
    message: "Provider inference failed due to a network error.",
    retryable: true,
  });

  const oversized = completion("model_context_window_exceeded");
  const rejected = await collectModelStream(
    driver("glm", oversized.fetch).stream(request),
  );
  assert.equal(rejected.text, "");
  assert.deepEqual(rejected.error, {
    type: "error",
    code: "context_window_exceeded",
    message: "Provider rejected a request that exceeded its context window.",
    retryable: false,
  });
});

test("abort and malformed provider tool arguments fail closed", async () => {
  const neverCalled = fakeFetch(() => jsonResponse({}));
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(
    collectModelStream(
      driver("glm", neverCalled.fetch).stream(
        {
          messages: [{ role: "user", content: [{ type: "text", text: "go" }] }],
        },
        { signal: controller.signal },
      ),
    ),
    (error: unknown) =>
      error instanceof ModelContractError && error.code === "aborted",
  );
  assert.equal(neverCalled.calls.length, 0);

  const malformed = fakeFetch(() =>
    jsonResponse({
      id: "response-1",
      model: "glm-routed",
      object: "chat.completion",
      created: 1,
      choices: [
        {
          index: 0,
          logprobs: null,
          finish_reason: "tool_calls",
          message: {
            role: "assistant",
            content: null,
            tool_calls: [
              {
                id: "call-1",
                type: "function",
                function: { name: "read_file", arguments: "not-json" },
              },
            ],
          },
        },
      ],
    }),
  );
  await assert.rejects(
    collectModelStream(
      driver("glm", malformed.fetch).stream({
        messages: [{ role: "user", content: [{ type: "text", text: "go" }] }],
        tools: [{ name: "read_file", inputSchema: { type: "object" } }],
      }),
    ),
    (error: unknown) =>
      error instanceof ModelContractError && error.code === "invalid_stream",
  );
});
