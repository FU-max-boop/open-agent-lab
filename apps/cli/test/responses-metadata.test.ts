import assert from "node:assert/strict";
import test from "node:test";

import { SseMetadataObserver } from "../src/responses-metadata.js";

const MODEL = "glm-5.3";

function observe(...frames: Array<string | Uint8Array>) {
  const observer = new SseMetadataObserver(null);
  for (const frame of frames) {
    observer.feed(typeof frame === "string" ? Buffer.from(frame) : frame);
  }
  return observer.finish();
}

test("metadata parsing fails closed on invalid UTF-8 and duplicate JSON keys", () => {
  const invalidUtf8 = observe(Uint8Array.of(0xff));
  assert.equal(invalidUtf8.parseErrors, 1);
  assert.equal(invalidUtf8.terminalEvent, null);

  const duplicateModel = observe(
    `data: {"type":"response.completed","response":{"id":"one","model":"other","model":"${MODEL}","usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2}}}\n\n`,
  );
  assert.equal(duplicateModel.parseErrors, 1);
  assert.equal(duplicateModel.returnedModel, null);
  assert.equal(duplicateModel.terminalEvent, null);

  const escapedDuplicate = observe(
    `data: {"type":"response.completed","response":{"id":"one","model":"other","\\u006dodel":"${MODEL}","usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2}}}\n\n`,
  );
  assert.equal(escapedDuplicate.parseErrors, 1);
});

test("an identical terminal frame repeated twice remains a conflict", () => {
  const terminal = `data: {"type":"response.completed","response":{"id":"one","model":"${MODEL}","usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2}}}\n\n`;
  const metadata = observe(terminal, terminal);

  assert.equal(metadata.parseErrors, 0);
  assert.equal(metadata.terminalEvent, null);
  assert.deepEqual(metadata.metadataConflicts, ["terminal_event"]);
});

test("valid escaped strings and nested objects do not confuse duplicate-key scanning", () => {
  const metadata = observe(
    `data: {"type":"response.completed","note":"quote=\\\" comma=, braces={}","items":[{"model":"nested"}],"response":{"id":"one","model":"${MODEL}","usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2}}}\n\n`,
  );

  assert.equal(metadata.parseErrors, 0);
  assert.equal(metadata.returnedModel, MODEL);
  assert.equal(metadata.terminalEvent, "response.completed");
});

test("non-canonical ignored fields and terminal frames without response fail closed", () => {
  const nonFinite = observe(
    `data: {"type":"response.output_text.delta","delta":1e400}\n\n`,
  );
  assert.equal(nonFinite.parseErrors, 1);

  const nonEvent = observe(
    "data: []\n\n",
    `data: {"type":"response.completed","response":{"id":"one","model":"${MODEL}","usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2}}}\n\n`,
  );
  assert.equal(nonEvent.parseErrors, 1);

  const missingTerminalResponse = observe(
    `data: {"type":"response.created","response":{"id":"one","model":"${MODEL}","usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2}}}\n\n`,
    `data: {"type":"response.completed"}\n\n`,
  );
  assert.equal(missingTerminalResponse.parseErrors, 1);
  assert.equal(missingTerminalResponse.responseId, null);
  assert.equal(missingTerminalResponse.returnedModel, null);
  assert.equal(missingTerminalResponse.terminalEvent, null);
  assert.equal(missingTerminalResponse.usage, null);
});

test("conflicting flat and nested token aliases fail closed", () => {
  const metadata = observe(
    `data: {"type":"response.completed","response":{"id":"one","model":"${MODEL}","usage":{"input_tokens":10,"output_tokens":5,"total_tokens":15,"cached_input_tokens":9,"reasoning_output_tokens":4,"input_tokens_details":{"cached_tokens":1},"output_tokens_details":{"reasoning_tokens":2}}}}\n\n`,
  );

  assert.equal(metadata.parseErrors, 1);
  assert.equal(metadata.usage, null);
});

test("invalid or impossible optional token usage fails closed", () => {
  for (const usage of [
    '"input_tokens":10,"output_tokens":5,"total_tokens":15,"cached_input_tokens":-1',
    '"input_tokens":10,"output_tokens":5,"total_tokens":15,"input_tokens_details":{"cached_tokens":11}',
    '"input_tokens":10,"output_tokens":5,"total_tokens":15,"output_tokens_details":{"reasoning_tokens":6}',
    '"input_tokens":10,"output_tokens":5,"total_tokens":16',
  ]) {
    const metadata = observe(
      `data: {"type":"response.completed","response":{"id":"one","model":"${MODEL}","usage":{${usage}}}}\n\n`,
    );
    assert.equal(metadata.parseErrors, 1);
    assert.equal(metadata.usage, null);
  }
});

test("model source saturation always reserves a terminal source", () => {
  const observer = new SseMetadataObserver(MODEL);
  for (let index = 0; index < 8; index += 1) {
    observer.feed(
      Buffer.from(
        `data: {"type":"response.created","response":{"id":"created-${index}","model":"${MODEL}","headers":{"openai-model":"${MODEL}"}}}\n\n`,
      ),
    );
  }
  observer.feed(
    Buffer.from(
      `data: {"type":"response.completed","response":{"id":"one","model":"${MODEL}","usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2}}}\n\n`,
    ),
  );

  const metadata = observer.finish();
  assert.equal(Object.keys(metadata.modelSources).length, 16);
  assert.ok(
    Object.keys(metadata.modelSources).some((source) =>
      source.startsWith("event.response.completed.response.model."),
    ),
  );
  assert.equal(metadata.returnedModel, MODEL);
  assert.equal(metadata.terminalEvent, "response.completed");
});
