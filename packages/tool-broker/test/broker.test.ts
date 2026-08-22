import assert from "node:assert/strict";
import test from "node:test";

import type { JsonObject, Sha256Digest } from "@open-agent-lab/contracts";

import { ToolBroker, ToolBrokerError, type ToolDefinition } from "../src/index.js";

const digest = (character: string): Sha256Digest =>
  `sha256:${character.repeat(64)}` as Sha256Digest;

function definition(overrides: Partial<ToolDefinition>): ToolDefinition {
  return {
    name: "test.tool",
    contractDigest: digest("c"),
    effect: "workspace_mutation",
    stateFingerprint: async () => digest("a"),
    execute: async () => ({ output: null }),
    ...overrides,
  };
}

test("broker freezes fingerprinted arguments before handing them to a tool", async () => {
  const broker = new ToolBroker([{
    name: "workspace.read",
    contractDigest: digest("c"),
    effect: "read_only",
    stateFingerprint: async () => digest("a"),
    execute: async (invocation) => {
      const argumentsValue = invocation.arguments as { nested: { value: number } };
      argumentsValue.nested.value = 2;
      return { output: null };
    },
  }]);
  const context = { runId: "run-1" };
  const invocation = await broker.prepare({
    invocationId: "call-1",
    toolName: "workspace.read",
    arguments: { nested: { value: 1 } },
  }, context);
  await assert.rejects(broker.execute(invocation, context), TypeError);
  assert.deepEqual(invocation.arguments, { nested: { value: 1 } });
});

test("broker rejects runtime metadata which is not a JSON object", async () => {
  const broker = new ToolBroker([{
    name: "workspace.read",
    contractDigest: digest("c"),
    effect: "read_only",
    stateFingerprint: async () => digest("a"),
    execute: async () => ({
      output: null,
      metadata: null as unknown as JsonObject,
    }),
  }]);
  const context = { runId: "run-1" };
  const invocation = await broker.prepare({
    invocationId: "call-1",
    toolName: "workspace.read",
    arguments: {},
  }, context);
  await assert.rejects(
    broker.execute(invocation, context),
    (error: unknown) => error instanceof ToolBrokerError && error.code === "invalid_result",
  );
});

test("broker validates reconciliation contracts at construction and runtime", async () => {
  assert.throws(
    () => new ToolBroker([definition({ reconcile: 42 as never })]),
    (error: unknown) => error instanceof ToolBrokerError && error.code === "invalid_definition",
  );
  const broker = new ToolBroker([definition({ reconcile: async () => null as never })]);
  const invocation = await broker.prepare({
    invocationId: "call-1",
    toolName: "test.tool",
    arguments: {},
  }, { runId: "run-1" });
  await assert.rejects(
    broker.reconcile(invocation, { runId: "run-1" }),
    (error: unknown) => error instanceof ToolBrokerError && error.code === "invalid_result",
  );
});
