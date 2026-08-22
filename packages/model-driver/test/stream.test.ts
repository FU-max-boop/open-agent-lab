import assert from "node:assert/strict";
import test from "node:test";

import {
  ModelContractError,
  collectModelStream,
  type ModelStreamEvent,
} from "../src/index.js";

async function* events(
  values: readonly ModelStreamEvent[],
): AsyncIterable<ModelStreamEvent> {
  yield* values;
}

test("the unified stream reconstructs text, reasoning, tools, and usage", async () => {
  const result = await collectModelStream(
    events([
      {
        type: "response_info",
        info: {
          responseId: "response-1",
          model: "model-2026-08-22",
          systemFingerprint: "fp-1",
        },
      },
      { type: "reasoning_delta", delta: "inspect " },
      { type: "reasoning_delta", delta: "state" },
      { type: "text_delta", delta: "Calling " },
      { type: "text_delta", delta: "tool" },
      {
        type: "tool_call_delta",
        index: 0,
        callId: "call-1",
        name: "read_file",
        argumentsDelta: '{"pa',
      },
      { type: "tool_call_delta", index: 0, argumentsDelta: 'th":"a.txt"}' },
      {
        type: "tool_call_complete",
        index: 0,
        callId: "call-1",
        name: "read_file",
        arguments: { path: "a.txt" },
      },
      {
        type: "usage",
        usage: {
          inputTokens: 20,
          outputTokens: 8,
          totalTokens: 28,
          cachedInputTokens: 5,
          reasoningTokens: 2,
        },
      },
      { type: "finish", reason: "tool_calls" },
    ]),
  );

  assert.equal(result.text, "Calling tool");
  assert.deepEqual(result.responseInfo, {
    responseId: "response-1",
    model: "model-2026-08-22",
    systemFingerprint: "fp-1",
  });
  assert.equal(result.reasoning, "inspect state");
  assert.deepEqual(result.toolCalls, [
    {
      index: 0,
      callId: "call-1",
      name: "read_file",
      arguments: { path: "a.txt" },
    },
  ]);
  assert.deepEqual(result.usage, {
    inputTokens: 20,
    outputTokens: 8,
    totalTokens: 28,
    cachedInputTokens: 5,
    reasoningTokens: 2,
  });
  assert.deepEqual(result.finish, { type: "finish", reason: "tool_calls" });
  assert.equal(result.error, undefined);
});

test("response identity is narrow, non-empty, and emitted at most once", async () => {
  await assert.rejects(
    collectModelStream(
      events([
        { type: "response_info", info: {} },
        { type: "finish", reason: "stop" },
      ]),
    ),
    /at least one identity field/,
  );

  await assert.rejects(
    collectModelStream(
      events([
        { type: "response_info", info: { responseId: "one" } },
        { type: "response_info", info: { responseId: "two" } },
        { type: "finish", reason: "stop" },
      ]),
    ),
    /only once/,
  );

  await assert.rejects(
    collectModelStream(
      events([
        {
          type: "response_info",
          info: { responseId: 42 as unknown as string },
        },
        { type: "finish", reason: "stop" },
      ]),
    ),
    /non-empty strings/,
  );

  const sentinel = "must-not-be-persisted";
  await assert.rejects(
    collectModelStream(
      events([
        {
          type: "response_info",
          info: {
            model: "safe-model",
            raw: { apiKey: sentinel },
          } as unknown as { model: string },
        },
        { type: "finish", reason: "stop" },
      ]),
    ),
    (error: unknown) =>
      error instanceof ModelContractError &&
      error.code === "invalid_stream" &&
      !error.message.includes(sentinel),
  );
});

test("an error is a normalized terminal event", async () => {
  const result = await collectModelStream(
    events([
      { type: "text_delta", delta: "partial" },
      {
        type: "error",
        code: "rate_limited",
        message: "try later",
        retryable: true,
        providerCode: "429",
      },
    ]),
  );

  assert.equal(result.text, "partial");
  assert.deepEqual(result.error, {
    type: "error",
    code: "rate_limited",
    message: "try later",
    retryable: true,
    providerCode: "429",
  });
});

test("conformance rejects events after finish", async () => {
  await assert.rejects(
    collectModelStream(
      events([
        { type: "finish", reason: "stop" },
        { type: "text_delta", delta: "late" },
      ]),
    ),
    (error: unknown) =>
      error instanceof ModelContractError && error.code === "invalid_stream",
  );
});

test("conformance rejects unterminated and inconsistent tool streams", async () => {
  await assert.rejects(
    collectModelStream(events([{ type: "text_delta", delta: "unfinished" }])),
    /without a finish or error/,
  );

  await assert.rejects(
    collectModelStream(
      events([
        {
          type: "tool_call_delta",
          index: 0,
          callId: "c1",
          name: "edit",
          argumentsDelta: '{"path":"a"}',
        },
        {
          type: "tool_call_complete",
          index: 0,
          callId: "c1",
          name: "edit",
          arguments: { path: "b" },
        },
        { type: "finish", reason: "tool_calls" },
      ]),
    ),
    /do not match its deltas/,
  );
});

test("conformance rejects impossible normalized usage", async () => {
  await assert.rejects(
    collectModelStream(
      events([
        {
          type: "usage",
          usage: { inputTokens: 2, outputTokens: 3, totalTokens: 99 },
        },
        { type: "finish", reason: "stop" },
      ]),
    ),
    /totalTokens must equal/,
  );
});
