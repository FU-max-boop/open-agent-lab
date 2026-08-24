import assert from "node:assert/strict";
import test from "node:test";

import {
  Codex149SecretGuard,
  MAX_SSE_EVENT_BYTES,
  ResponsesSseParser,
  inspectNonSuccessBody,
  type ParsedSseFrame,
} from "../src/responses-sse.js";

const SECRET = "provider-secret-1234567890abcdef";

function parse(bytes: Uint8Array, cuts: number[] = []): ParsedSseFrame[] {
  const parser = new ResponsesSseParser();
  const frames: ParsedSseFrame[] = [];
  let offset = 0;
  for (const cut of [...cuts, bytes.byteLength]) {
    frames.push(...parser.feed(bytes.subarray(offset, cut)));
    offset = cut;
  }
  frames.push(...parser.finish());
  return frames;
}

function eventFrame(event: Record<string, unknown>): ParsedSseFrame {
  const [frame] = parse(Buffer.from(`data: ${JSON.stringify(event)}\n\n`));
  assert.ok(frame !== undefined);
  return frame;
}

function commit(guard: Codex149SecretGuard, event: Record<string, unknown>): void {
  const stage = guard.stage(eventFrame(event));
  assert.equal(stage.secret, false);
  assert.equal(stage.invalid, false);
  stage.commit();
}

test("SSE parser preserves raw bytes across line endings, BOM, and every cut", () => {
  for (const ending of ["\n", "\r", "\r\n"]) {
    const bytes = Buffer.from(
      `\ufeffevent: ignored${ending}data: {"type":"response.output_text.delta",${ending}` +
        `data: "delta":"中"}${ending}${ending}`,
    );
    for (let cut = 0; cut <= bytes.length; cut += 1) {
      const frames = parse(bytes, [cut]);
      assert.equal(frames.length, 1);
      assert.deepEqual(Buffer.from(frames[0]!.raw), bytes);
      assert.deepEqual(frames[0]!.event, {
        type: "response.output_text.delta",
        delta: "中",
      });
      assert.equal(frames[0]!.error, null);
    }
  }
});

test("SSE parser preserves but does not consume a valid EOF tail", () => {
  const bytes = Buffer.from(
    'data: {"type":"response.created","response":{"id":"one"}}',
  );
  const frames = parse(
    bytes,
    Array.from({ length: bytes.length }, (_value, index) => index),
  );
  assert.equal(frames.length, 1);
  assert.deepEqual(Buffer.from(frames[0]!.raw), bytes);
  assert.equal(frames[0]!.event, null);
  assert.equal(frames[0]!.error, null);

  const [secretTail] = parse(Buffer.from(`data: ${SECRET}`));
  assert.equal(new Codex149SecretGuard(SECRET).stage(secretTail!).secret, true);
});

test("SSE parser fails closed on invalid UTF-8, JSON, and semantic duplicate keys", () => {
  for (const bytes of [
    Buffer.from([0x64, 0x61, 0x74, 0x61, 0x3a, 0x20, 0xff, 0x0a, 0x0a]),
    Buffer.from("data: {not-json}\n\n"),
    Buffer.from(
      'data: {"type":"response.created","t\\u0079pe":"response.completed"}\n\n',
    ),
  ]) {
    const [frame] = parse(bytes);
    assert.equal(frame?.error, "invalid_sse");
    assert.deepEqual(Buffer.from(frame!.raw), bytes);
  }
});

test("SSE parser applies the event byte limit at exactly 1 MiB", () => {
  const prefix = 'data: {"type":"response.output_text.delta","delta":"';
  const suffix = '"}';
  const exactBody = Buffer.from(`${prefix}${"x".repeat(MAX_SSE_EVENT_BYTES - prefix.length - suffix.length)}${suffix}`);
  const exact = Buffer.concat([exactBody, Buffer.from("\n\n")]);
  const tooLarge = Buffer.concat([exactBody, Buffer.from("x\n\n")]);

  assert.equal(parse(exact)[0]?.error, null);
  assert.equal(parse(tooLarge)[0]?.error, "sse_event_too_large");
});

test("secret guard catches raw and escaped secrets across representative cuts", () => {
  const raw = Buffer.from(`: ${SECRET}\n\n`);
  const escapedJson = JSON.stringify({
    type: "response.output_text.delta",
    delta: SECRET,
  }).replaceAll("-", "\\u002d");
  const comma = escapedJson.indexOf(",") + 1;
  const escaped = Buffer.from(
    `data: ${escapedJson.slice(0, comma)}\ndata: ${escapedJson.slice(comma)}\n\n`,
  );
  for (const bytes of [raw, escaped]) {
    for (let cut = 0; cut <= bytes.length; cut += 1) {
      const [frame] = parse(bytes, [cut]);
      assert.ok(frame !== undefined);
      assert.equal(new Codex149SecretGuard(SECRET).stage(frame).secret, true);
    }
  }
});

test("secret guard joins only Codex 0.149 consumed delta channels", () => {
  const prefix = SECRET.slice(0, 17);
  const suffix = SECRET.slice(17);
  const cases: Array<{
    added: Record<string, unknown>;
    first: Record<string, unknown>;
    second: Record<string, unknown>;
  }> = [
    {
      added: { type: "message", role: "assistant", id: "msg_a", content: [] },
      first: { type: "response.output_text.delta", delta: prefix },
      second: { type: "response.output_text.delta", delta: suffix },
    },
    {
      added: { type: "reasoning", id: "rs_a", summary: [], content: [] },
      first: {
        type: "response.reasoning_summary_text.delta",
        summary_index: 0,
        delta: prefix,
      },
      second: {
        type: "response.reasoning_summary_text.delta",
        summary_index: 0,
        delta: suffix,
      },
    },
    {
      added: { type: "reasoning", id: "rs_b", summary: [], content: [] },
      first: { type: "response.reasoning_text.delta", content_index: 0, delta: prefix },
      second: { type: "response.reasoning_text.delta", content_index: 0, delta: suffix },
    },
    {
      added: {
        type: "custom_tool_call",
        id: "ctc_a",
        call_id: "call_a",
        name: "apply_patch",
        input: "",
      },
      first: {
        type: "response.custom_tool_call_input.delta",
        item_id: "ctc_a",
        call_id: "call_a",
        delta: prefix,
      },
      second: {
        type: "response.custom_tool_call_input.delta",
        item_id: "ctc_a",
        call_id: "call_a",
        delta: suffix,
      },
    },
  ];
  for (const { added, first, second } of cases) {
    const guard = new Codex149SecretGuard(SECRET);
    commit(guard, { type: "response.output_item.added", item: added });
    commit(guard, first);
    commit(guard, { type: "response.metadata", metadata: { safe: prefix } });
    assert.equal(guard.stage(eventFrame(second)).secret, true);
  }

  const seeded = new Codex149SecretGuard(SECRET);
  commit(seeded, {
    type: "response.output_item.added",
    item: {
      type: "message",
      role: "assistant",
      id: "seeded",
      content: [{ type: "output_text", text: prefix }],
    },
  });
  assert.equal(
    seeded.stage(eventFrame({ type: "response.output_text.delta", delta: suffix })).secret,
    true,
  );
});

test("added items update only the Codex 0.149 state they actually consume", () => {
  const prefix = SECRET.slice(0, 17);
  const suffix = SECRET.slice(17);
  for (const inserted of [
    { type: "response.output_item.added", item: { type: "future_item" } },
    {
      type: "response.output_item.added",
      item: { type: "custom_tool_call", id: "tool", call_id: "call", name: "tool", input: "" },
    },
    {
      type: "response.output_item.added",
      item: { type: "image_generation_call", id: "image", status: "completed", result: "safe" },
    },
  ]) {
    const guard = new Codex149SecretGuard(SECRET);
    commit(guard, {
      type: "response.output_item.added",
      item: { type: "message", role: "assistant", id: "message", content: [] },
    });
    commit(guard, { type: "response.output_text.delta", delta: prefix });
    commit(guard, inserted);
    assert.equal(
      guard.stage(eventFrame({ type: "response.output_text.delta", delta: suffix })).secret,
      true,
    );
  }

  const custom = new Codex149SecretGuard(SECRET);
  commit(custom, {
    type: "response.output_item.added",
    item: { type: "custom_tool_call", id: "tool", call_id: "call", name: "tool", input: prefix },
  });
  commit(custom, {
    type: "response.output_item.added",
    item: { type: "message", role: "assistant", id: "message", content: [] },
  });
  assert.equal(
    custom.stage(eventFrame({
      type: "response.custom_tool_call_input.delta",
      item_id: "tool",
      call_id: "call",
      delta: suffix,
    })).secret,
    true,
  );

  for (const item of [
    { type: "message", role: "assistant" },
    { type: "message", role: "system", content: [] },
    { type: "message", role: "assistant", phase: 123, content: [] },
    { type: "message", role: "assistant", content: [], internal_chat_message_metadata_passthrough: 123 },
    { type: "custom_tool_call", call_id: "call", name: "tool" },
    { type: "custom_tool_call", call_id: "call", name: "tool", input: "", status: 123 },
    { type: "web_search_call", action: {} },
    { type: "web_search_call", action: { type: "search", query: 123 } },
  ]) {
    const invalid = new Codex149SecretGuard(SECRET).stage(
      eventFrame({ type: "response.output_item.added", item }),
    );
    assert.equal(invalid.invalid, true);
  }

  for (const item of [
    { type: "message", role: "system", content: [] },
    { type: "message", role: "assistant", phase: 123, content: [] },
    { type: "web_search_call", action: {} },
  ]) {
    const guard = new Codex149SecretGuard(SECRET);
    commit(guard, {
      type: "response.output_item.added",
      item: { type: "message", role: "assistant", id: "message", content: [] },
    });
    commit(guard, { type: "response.output_text.delta", delta: prefix });
    const invalid = guard.stage(eventFrame({ type: "response.output_item.added", item }));
    assert.equal(invalid.invalid, true);
    assert.equal(
      guard.stage(eventFrame({ type: "response.output_text.delta", delta: suffix })).secret,
      true,
    );
  }

  for (const malformed of [
    { type: "response.output_text.delta", item_id: 123, delta: "ignored" },
    { type: "response.output_text.delta", delta: 123 },
    { type: "response.output_text.delta", call_id: false, delta: "ignored" },
    { type: "response.reasoning_summary_text.delta", summary_index: 0.5, delta: "ignored" },
    { type: "response.reasoning_text.delta", content_index: 9_007_199_254_740_992, delta: "ignored" },
  ]) {
    const guard = new Codex149SecretGuard(SECRET);
    commit(guard, {
      type: "response.output_item.added",
      item: { type: "message", role: "assistant", id: "message", content: [] },
    });
    commit(guard, { type: "response.output_text.delta", delta: prefix });
    assert.equal(guard.stage(eventFrame(malformed)).invalid, true);
    assert.equal(
      guard.stage(eventFrame({ type: "response.output_text.delta", delta: suffix })).secret,
      true,
    );
  }

  for (const [action, expectedSecret] of [
    [{ type: "search", query: prefix, queries: ["safe-overwrite"] }, true],
    [{ type: "search", query: "", queries: [prefix] }, true],
    [{ type: "search", query: "", queries: [prefix, "second"] }, false],
    [{ type: "open_page", url: prefix }, true],
    [{ type: "find_in_page", url: prefix, pattern: "needle" }, true],
    [{ type: "find_in_page", pattern: prefix }, false],
  ] as const) {
    const guard = new Codex149SecretGuard(SECRET);
    commit(guard, {
      type: "response.output_item.added",
      item: { type: "web_search_call", id: "web", action },
    });
    assert.equal(
      guard.stage(eventFrame({ type: "response.output_text.delta", delta: suffix })).secret,
      expectedSecret,
    );
  }

  const unknownWebAction = new Codex149SecretGuard(SECRET);
  commit(unknownWebAction, {
    type: "response.output_item.added",
    item: { type: "web_search_call", id: "web", action: { type: "future", query: prefix } },
  });
  assert.equal(
    unknownWebAction.stage(eventFrame({ type: "response.output_text.delta", delta: suffix })).secret,
    false,
  );

  const citation = new Codex149SecretGuard(SECRET);
  commit(citation, {
    type: "response.output_item.added",
    item: { type: "message", role: "assistant", id: "citation", content: [] },
  });
  commit(citation, {
    type: "response.output_text.delta",
    delta: `${prefix}<oai-mem-`,
  });
  commit(citation, {
    type: "response.output_text.delta",
    delta: "citation>safe-source</oai-mem-citation>",
  });
  assert.equal(
    citation.stage(eventFrame({ type: "response.output_text.delta", delta: suffix })).secret,
    true,
  );
});

test("metadata is inert, item identities isolate suffixes, and done resets them", () => {
  const prefix = SECRET.slice(0, 17);
  const suffix = SECRET.slice(17);
  const guard = new Codex149SecretGuard(SECRET);
  commit(guard, {
    type: "response.output_item.added",
    item: { type: "message", role: "assistant", id: "one", content: [] },
  });
  commit(guard, { type: "response.output_text.delta", delta: prefix });
  commit(guard, {
    type: "response.output_item.added",
    item: { type: "message", role: "assistant", id: "two", content: [] },
  });
  commit(guard, { type: "response.output_text.delta", delta: suffix });
  commit(guard, { type: "response.output_text.delta", delta: prefix });
  commit(guard, {
    type: "response.output_item.done",
    item: { type: "message", role: "assistant", id: "two", content: [] },
  });
  assert.equal(
    guard.stage(eventFrame({ type: "response.output_text.delta", delta: suffix })).secret,
    false,
  );
  const unbound = new Codex149SecretGuard(SECRET);
  commit(unbound, { type: "response.output_text.delta", delta: prefix });
  assert.equal(
    unbound.stage(eventFrame({ type: "response.output_text.delta", delta: suffix })).secret,
    false,
  );
  const indexed = new Codex149SecretGuard(SECRET);
  commit(indexed, { type: "response.output_item.added", item: { type: "reasoning", id: "rs", summary: [] } });
  commit(indexed, { type: "response.reasoning_summary_text.delta", summary_index: 0, delta: prefix });
  commit(indexed, { type: "response.reasoning_summary_text.delta", summary_index: 1, delta: "safe" });
  assert.equal(indexed.stage(eventFrame({ type: "response.reasoning_summary_text.delta", summary_index: 0, delta: suffix })).secret, true);
});

test("direct item and metadata payloads plus once-nested function arguments are scanned", () => {
  for (const event of [
    {
      type: "response.output_item.added",
      item: { type: "message", role: "assistant", id: "one", content: [{ type: "output_text", text: SECRET }] },
    },
    {
      type: "response.output_item.done",
      item: { type: "custom_tool_call", call_id: "one", name: "tool", input: SECRET },
    },
    {
      type: "response.output_item.added",
      item: { type: "message", role: "assistant", id: "joined", content: [
        { type: "output_text", text: SECRET.slice(0, 17) },
        { type: "output_text", text: SECRET.slice(17) },
      ] },
    },
    {
      type: "response.output_item.done",
      item: { type: "message", role: "assistant", id: "joined-done", content: [
        { type: "output_text", text: SECRET.slice(0, 17) },
        { type: "output_text", text: SECRET.slice(17) },
      ] },
    },
    {
      type: "response.output_item.done",
      item: { type: "message", role: "assistant", id: "citation-done", content: [
        { type: "output_text", text: SECRET.slice(0, 17) },
        { type: "output_text", text: "<oai-mem-citation>safe</oai-mem-citation>" },
        { type: "output_text", text: SECRET.slice(17) },
      ] },
    },
    { type: "response.metadata", metadata: { [SECRET]: "value" } },
  ]) {
    assert.equal(new Codex149SecretGuard(SECRET).stage(eventFrame(event)).secret, true);
  }

  const escapedSecret = [...SECRET]
    .map((character) => `\\u${character.charCodeAt(0).toString(16).padStart(4, "0")}`)
    .join("");
  const nested = eventFrame({
    type: "response.output_item.done",
    item: { type: "function_call", call_id: "one", name: "tool", arguments: `{"key":"${escapedSecret}"}` },
  });
  assert.equal(new Codex149SecretGuard(SECRET).stage(nested).secret, true);

  const duplicate = eventFrame({
    type: "response.output_item.done",
    item: { type: "function_call", call_id: "one", name: "tool", arguments: '{"a":1,"\\u0061":2}' },
  });
  const invalid = new Codex149SecretGuard(SECRET).stage(duplicate);
  assert.equal(invalid.secret, false);
  assert.equal(invalid.invalid, true);
});

test("non-success body scan catches secrets and rejects invalid UTF-8", () => {
  const escaped = JSON.stringify({ error: { message: SECRET } }).replaceAll("-", "\\u002d");
  assert.equal(inspectNonSuccessBody(Buffer.from(SECRET), SECRET), "secret");
  assert.equal(inspectNonSuccessBody(Buffer.from(escaped), SECRET), "secret");
  assert.equal(inspectNonSuccessBody(Buffer.from([0xff, 0x00]), SECRET), "invalid");
  assert.equal(inspectNonSuccessBody(Buffer.from('{"error":"safe"}'), SECRET), "safe");
  assert.equal(inspectNonSuccessBody(Buffer.from("safe plain text"), SECRET), "safe");
});
