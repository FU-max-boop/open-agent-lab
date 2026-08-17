import assert from "node:assert/strict";
import test from "node:test";

import {
  ModelContractError,
  ScriptedModelDriver,
  collectModelStream,
  startModelDriver,
  type ModelCapabilities,
  type ModelRequest,
} from "../src/index.js";

const CAPABILITIES: ModelCapabilities = {
  text: true,
  image: false,
  tools: true,
  parallelTools: false,
  strictSchema: false,
  reasoning: false,
  context: 16_384,
  output: 4_096,
};

const REQUEST: ModelRequest = {
  messages: [
    { role: "system", content: [{ type: "text", text: "Be precise." }] },
    { role: "user", content: [{ type: "text", text: "Say hello." }] },
  ],
  generation: { maxOutputTokens: 64, temperature: 0 },
};

test("the scripted driver provides a deterministic offline conformance run", async () => {
  const driver = new ScriptedModelDriver({
    driverId: "fixture-1",
    capabilities: CAPABILITIES,
    turns: [
      {
        events: [
          { type: "text_delta", delta: "hel" },
          { type: "text_delta", delta: "lo" },
          {
            type: "usage",
            usage: { inputTokens: 7, outputTokens: 1, totalTokens: 8 },
          },
          { type: "finish", reason: "stop" },
        ],
      },
    ],
  });

  const started = await startModelDriver(driver, { text: true });
  const first = await collectModelStream(started.driver.stream(REQUEST));

  assert.equal(first.text, "hello");
  assert.equal(driver.consumedTurns, 1);
  assert.equal(driver.remainingTurns, 0);
  assert.deepEqual(driver.requests, [REQUEST]);
  driver.assertExhausted();
});

test("unsupported requests do not consume a scripted turn", async () => {
  const driver = new ScriptedModelDriver({
    capabilities: CAPABILITIES,
    turns: [{ events: [{ type: "finish", reason: "stop" }] }],
  });
  const unsupported: ModelRequest = {
    messages: [
      {
        role: "user",
        content: [
          {
            type: "image",
            source: { type: "base64", mediaType: "image/png", data: "AA==" },
          },
        ],
      },
    ],
  };

  await assert.rejects(
    collectModelStream(driver.stream(unsupported)),
    (error: unknown) =>
      error instanceof ModelContractError &&
      error.code === "capability_mismatch",
  );
  assert.equal(driver.consumedTurns, 0);
  assert.equal(driver.remainingTurns, 1);
});

test("script exhaustion is explicit and reproducible", async () => {
  const driver = new ScriptedModelDriver({
    capabilities: CAPABILITIES,
    turns: [],
  });

  await assert.rejects(
    collectModelStream(driver.stream(REQUEST)),
    (error: unknown) =>
      error instanceof ModelContractError && error.code === "script_exhausted",
  );
});

test("an aborted startup probe never increments probe count", async () => {
  const controller = new AbortController();
  controller.abort();
  const driver = new ScriptedModelDriver({
    capabilities: CAPABILITIES,
    turns: [],
  });

  await assert.rejects(
    startModelDriver(driver, {}, { signal: controller.signal }),
    (error: unknown) =>
      error instanceof ModelContractError && error.code === "aborted",
  );
  assert.equal(driver.probeCount, 0);
});
