import assert from "node:assert/strict";
import test from "node:test";

import { SseMetadataObserver } from "../src/responses-metadata.js";
import { ResponsesSseParser, type ParsedSseFrame } from "../src/responses-sse.js";

const MODEL = "glm-5.3";
const USAGE = { input_tokens: 1, output_tokens: 1, total_tokens: 2 };
type TerminalStatus = "completed" | "failed" | "incomplete";

function observedBy(...frames: Array<string | Uint8Array>): SseMetadataObserver {
  const observer = new SseMetadataObserver(null);
  const parser = new ResponsesSseParser();
  const accept = (parsed: ParsedSseFrame[]): void => {
    for (const frame of parsed) {
      if (frame.error === null && frame.event !== null) observer.observe(frame.event);
      else if (frame.error !== null) observer.recordParseError();
    }
  };
  for (const frame of frames) {
    accept(parser.feed(typeof frame === "string" ? Buffer.from(frame) : frame));
  }
  accept(parser.finish());
  return observer;
}

function observe(...frames: Array<string | Uint8Array>) {
  return observedBy(...frames).finish();
}

function terminalFrame(
  terminalStatus: TerminalStatus,
  response: Record<string, unknown> = {},
): string {
  return `data: ${JSON.stringify({
    type: `response.${terminalStatus}`,
    response: { id: "one", model: MODEL, usage: USAGE, ...response },
  })}\n\n`;
}

function assertInvalidTerminal(
  terminalStatus: TerminalStatus,
  response: Record<string, unknown>,
): void {
  const metadata = observe(terminalFrame(terminalStatus, response));
  assert.deepEqual(
    metadata,
    {
      responseId: null,
      returnedModel: null,
      modelConsistency: "missing",
      modelSources: {},
      systemFingerprint: null,
      terminalEvent: null,
      terminalStatus: null,
      incompleteReason: null,
      usage: null,
      metadataConflicts: [],
      parseErrors: 1,
    },
    `${terminalStatus}/${JSON.stringify(response)}`,
  );
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
  assert.equal(metadata.terminalStatus, null);
  assert.equal(metadata.incompleteReason, null);
  assert.deepEqual(metadata.metadataConflicts, ["terminal_event"]);
});

test("completed metadata binds exactly one terminal tool call for its continuation", () => {
  const observer = observedBy(terminalFrame("completed", {
    output: [
      { type: "reasoning", id: "reasoning-1" },
      { type: "function_call", call_id: "call-1", name: "exec_command" },
    ],
  }));
  assert.deepEqual(observer.toolOutputContinuation(), {
    type: "function_call_output",
    callId: "call-1",
  });

  const custom = observedBy(terminalFrame("completed", {
    output: [{ type: "custom_tool_call", call_id: "call-2", name: "apply_patch" }],
  }));
  assert.deepEqual(custom.toolOutputContinuation(), {
    type: "custom_tool_call_output",
    callId: "call-2",
  });

  for (const output of [
    undefined,
    [],
    [{ type: "message", content: [] }, { type: "function_call", call_id: "call-1" }],
    [
      { type: "function_call", call_id: "call-1" },
      { type: "function_call", call_id: "call-2" },
    ],
    [{ type: "function_call", call_id: "" }],
  ]) {
    const invalid = observedBy(terminalFrame("completed", { output }));
    assert.equal(invalid.toolOutputContinuation(), null);
  }
});

test("terminal event, status, error, and incomplete reason bind exactly", () => {
  const valid = [
    ["completed", {}],
    ["completed", { status: null, error: null, incomplete_details: null }],
    ["completed", { status: "completed" }],
    ["failed", {}],
    ["failed", { status: null, error: null, incomplete_details: null }],
    ["failed", { status: "failed", error: { code: "server_error" } }],
    ["incomplete", { incomplete_details: { reason: "max_output_tokens" } }],
    [
      "incomplete",
      { status: null, error: null, incomplete_details: { reason: "content_filter" } },
    ],
    [
      "incomplete",
      { status: "incomplete", incomplete_details: { reason: "max_output_tokens" } },
    ],
  ] as const satisfies ReadonlyArray<readonly [TerminalStatus, Record<string, unknown>]>;

  for (const [eventStatus, response] of valid) {
    const metadata = observe(terminalFrame(eventStatus, response));
    assert.equal(metadata.parseErrors, 0);
    assert.equal(metadata.terminalEvent, `response.${eventStatus}`);
    assert.equal(
      metadata.terminalStatus,
      "status" in response && response.status === eventStatus ? eventStatus : null,
    );
    assert.equal(
      metadata.incompleteReason,
      eventStatus === "incomplete"
        ? (response.incomplete_details as { reason: string }).reason
        : null,
    );
    assert.deepEqual(metadata.usage, USAGE);
  }
});

test("contradictory or malformed terminal tuples fail before recording metadata", () => {
  const invalid = [
    ["completed", { status: "failed" }],
    ["failed", { status: "incomplete" }],
    [
      "incomplete",
      { status: "completed", incomplete_details: { reason: "max_output_tokens" } },
    ],
    ["completed", { status: false }],
    [
      "completed",
      { status: "completed", error: { code: "server_error" } },
    ],
    ["completed", { incomplete_details: { reason: "max_output_tokens" } }],
    ["failed", { error: "server_error" }],
    ["failed", { error: [] }],
    [
      "failed",
      { incomplete_details: { reason: "max_output_tokens" } },
    ],
    ["incomplete", {}],
    ["incomplete", { incomplete_details: null }],
    ["incomplete", { incomplete_details: [] }],
    [
      "incomplete",
      {
        status: "incomplete",
        error: { code: "server_error" },
        incomplete_details: { reason: "max_output_tokens" },
      },
    ],
    ["incomplete", { incomplete_details: { reason: "" } }],
    [
      "incomplete",
      { incomplete_details: { reason: `x${"a".repeat(512)}` } },
    ],
    [
      "incomplete",
      { incomplete_details: { reason: "max_output_tokens\n" } },
    ],
    [
      "incomplete",
      { incomplete_details: { reason: { value: "max_output_tokens" } } },
    ],
  ] as const satisfies ReadonlyArray<readonly [TerminalStatus, Record<string, unknown>]>;

  for (const [terminalStatus, response] of invalid) {
    assertInvalidTerminal(terminalStatus, response);
  }
});

test("non-terminal response fields remain outside the terminal tuple contract", () => {
  const metadata = observe(
    `data: ${JSON.stringify({
      type: "response.created",
      response: {
        id: "created",
        model: MODEL,
        status: { arbitrary: true },
        error: "not-terminal",
        incomplete_details: ["not-terminal"],
      },
    })}\n\n`,
  );

  assert.equal(metadata.parseErrors, 0);
  assert.equal(metadata.modelConsistency, "consistent");
  assert.equal(metadata.modelSources["event.response.created.response.model.1"], MODEL);
  assert.equal(metadata.terminalEvent, null);
  assert.equal(metadata.terminalStatus, null);
  assert.equal(metadata.incompleteReason, null);
});

test("valid escaped strings and nested objects do not confuse duplicate-key scanning", () => {
  const metadata = observe(
    `data: {"type":"response.completed","note":"quote=\\\" comma=, braces={}","items":[{"model":"nested"}],"response":{"id":"one","model":"${MODEL}","usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2}}}\n\n`,
  );

  assert.equal(metadata.parseErrors, 0);
  assert.equal(metadata.returnedModel, MODEL);
  assert.equal(metadata.terminalEvent, "response.completed");
  assert.equal(metadata.terminalStatus, null);
  assert.equal(metadata.incompleteReason, null);
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
  assert.equal(missingTerminalResponse.terminalStatus, null);
  assert.equal(missingTerminalResponse.incompleteReason, null);
  assert.equal(missingTerminalResponse.usage, null);
});

test("conflicting flat and nested token aliases fail closed", () => {
  const metadata = observe(
    `data: {"type":"response.completed","response":{"id":"one","model":"${MODEL}","status":"completed","usage":{"input_tokens":10,"output_tokens":5,"total_tokens":15,"cached_input_tokens":9,"reasoning_output_tokens":4,"input_tokens_details":{"cached_tokens":1},"output_tokens_details":{"reasoning_tokens":2}}}}\n\n`,
  );

  assert.equal(metadata.parseErrors, 1);
  assert.equal(metadata.responseId, null);
  assert.equal(metadata.returnedModel, null);
  assert.equal(metadata.terminalEvent, null);
  assert.equal(metadata.terminalStatus, null);
  assert.equal(metadata.incompleteReason, null);
  assert.equal(metadata.usage, null);
});

test("nullable and omitted token detail objects normalize identically", () => {
  const expected = { input_tokens: 10, output_tokens: 5, total_tokens: 15 };
  for (const optionalDetails of [
    "",
    ',"input_tokens_details":null,"output_tokens_details":null',
  ]) {
    const metadata = observe(
      `data: {"type":"response.completed","response":{"id":"one","model":"${MODEL}","usage":{"input_tokens":10,"output_tokens":5,"total_tokens":15${optionalDetails}}}}\n\n`,
    );
    assert.equal(metadata.parseErrors, 0);
    assert.equal(metadata.terminalEvent, "response.completed");
    assert.equal(metadata.terminalStatus, null);
    assert.equal(metadata.incompleteReason, null);
    assert.deepEqual(metadata.usage, expected);
  }
});

test("invalid or impossible optional token usage fails closed", () => {
  for (const usage of [
    '"input_tokens":10,"output_tokens":5,"total_tokens":15,"cached_input_tokens":-1',
    '"input_tokens":10,"output_tokens":5,"total_tokens":15,"input_tokens_details":"invalid"',
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
  const parser = new ResponsesSseParser();
  const feed = (source: string): void => {
    for (const frame of parser.feed(Buffer.from(source))) {
      if (frame.event !== null) observer.observe(frame.event);
    }
  };
  for (let index = 0; index < 8; index += 1) {
    feed(
      `data: {"type":"response.created","response":{"id":"created-${index}","model":"${MODEL}","headers":{"openai-model":"${MODEL}"}}}\n\n`,
    );
  }
  feed(
    `data: {"type":"response.completed","response":{"id":"one","model":"${MODEL}","usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2}}}\n\n`,
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
  assert.equal(metadata.terminalStatus, null);
  assert.equal(metadata.incompleteReason, null);
});
