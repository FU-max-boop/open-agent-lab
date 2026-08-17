import assert from "node:assert/strict";
import test from "node:test";

import {
  ModelContractError,
  ScriptedModelDriver,
  assertRequestSupported,
  parseModelCapabilities,
  requirementsForRequest,
  startModelDriver,
  type ModelCapabilities,
  type ModelRequest,
} from "../src/index.js";

const BASE_CAPABILITIES: ModelCapabilities = {
  text: true,
  image: false,
  tools: true,
  parallelTools: false,
  strictSchema: true,
  reasoning: false,
  context: 32_768,
  output: 8_192,
};

test("capability probes are parsed, frozen, and checked at startup", async () => {
  const driver = new ScriptedModelDriver({
    capabilities: BASE_CAPABILITIES,
    turns: [],
  });

  const started = await startModelDriver(driver, {
    text: true,
    tools: true,
    minContext: 16_000,
    minOutput: 4_000,
  });

  assert.deepEqual(started.capabilities, BASE_CAPABILITIES);
  assert.equal(Object.isFrozen(started.capabilities), true);
  assert.equal(driver.probeCount, 1);
});

test("startup rejects missing capabilities without inspecting model names", async () => {
  const driver = new ScriptedModelDriver({
    driverId: "any-endpoint",
    capabilities: BASE_CAPABILITIES,
    turns: [],
  });

  await assert.rejects(
    startModelDriver(driver, { image: true }),
    (error: unknown) =>
      error instanceof ModelContractError &&
      error.code === "capability_mismatch" &&
      /image/.test(error.message),
  );
});

test("invalid capability relations fail deterministically", () => {
  assert.throws(
    () =>
      parseModelCapabilities({
        ...BASE_CAPABILITIES,
        tools: false,
        parallelTools: true,
      }),
    (error: unknown) =>
      error instanceof ModelContractError &&
      error.code === "invalid_capabilities",
  );

  assert.throws(
    () => parseModelCapabilities({ ...BASE_CAPABILITIES, output: 65_536 }),
    /cannot exceed 'context'/,
  );
});

test("request requirements are derived from content and controls", () => {
  const request: ModelRequest = {
    messages: [
      {
        role: "user",
        content: [
          { type: "text", text: "Inspect this screenshot" },
          {
            type: "image",
            source: { type: "base64", mediaType: "image/png", data: "AA==" },
          },
        ],
      },
    ],
    tools: [
      {
        name: "click",
        inputSchema: { type: "object" },
        strict: true,
      },
    ],
    parallelToolCalls: true,
    reasoning: { enabled: true },
    generation: { maxOutputTokens: 2_048 },
  };

  assert.deepEqual(requirementsForRequest(request), {
    text: true,
    image: true,
    tools: true,
    parallelTools: true,
    strictSchema: true,
    reasoning: true,
    minOutput: 2_048,
  });

  assert.throws(
    () => assertRequestSupported(request, BASE_CAPABILITIES),
    (error: unknown) =>
      error instanceof ModelContractError &&
      error.code === "capability_mismatch" &&
      /image/.test(error.message),
  );
});

test("request validation catches impossible tool controls before a call", () => {
  assert.throws(
    () =>
      assertRequestSupported(
        {
          messages: [{ role: "user", content: [{ type: "text", text: "go" }] }],
          parallelToolCalls: true,
        },
        BASE_CAPABILITIES,
      ),
    (error: unknown) =>
      error instanceof ModelContractError && error.code === "invalid_request",
  );
});
