import assert from "node:assert/strict";
import test from "node:test";

import { startResponsesFixture } from "../src/responses-fixture.js";

test("Responses fixture completes one real function-tool round", async (t) => {
  const fixture = await startResponsesFixture({
    bearer: "fixture-secret",
    model: "fixture-model",
    command: "printf fixture",
    callId: "call_fixture_test",
  });
  t.after(() => fixture.close());
  const rejected = await fetch(`${fixture.baseUrl}/responses`, {
    method: "POST",
    headers: { authorization: "Bearer wrong" },
    body: "not-json",
  });
  assert.equal(rejected.status, 401);
  assert.equal(fixture.snapshot().requests.length, 0);
  const request = (input: unknown): Promise<Response> =>
    fetch(`${fixture.baseUrl}/responses`, {
      method: "POST",
      headers: {
        authorization: "Bearer fixture-secret",
        "content-type": "application/json",
      },
      body: JSON.stringify({
        model: "fixture-model",
        stream: true,
        input,
        tools: [{ type: "function", name: "exec_command" }],
      }),
    });

  const first = await request("run the fixture");
  assert.equal(first.status, 200);
  assert.match(await first.text(), /"call_id":"call_fixture_test"/u);
  const second = await request([
    {
      type: "function_call_output",
      call_id: "call_fixture_test",
      output: "fixture output",
    },
  ]);
  assert.equal(second.status, 200);
  assert.match(await second.text(), /response\.completed/u);
  assert.deepEqual(fixture.snapshot(), {
    requests: [
      {
        method: "POST",
        url: "/responses",
        body: {
          model: "fixture-model",
          stream: true,
          input: "run the fixture",
          tools: [{ type: "function", name: "exec_command" }],
        },
      },
      {
        method: "POST",
        url: "/responses",
        body: {
          model: "fixture-model",
          stream: true,
          input: [
            {
              type: "function_call_output",
              call_id: "call_fixture_test",
              output: "fixture output",
            },
          ],
          tools: [{ type: "function", name: "exec_command" }],
        },
      },
    ],
    toolName: "exec_command",
    toolOutput: "fixture output",
  });
});

test("Responses fixture binds an exact developer-instruction marker", async (t) => {
  const marker = "Run one focused verification pass.\n";
  const fixture = await startResponsesFixture({
    bearer: "fixture-secret",
    model: "fixture-model",
    command: "printf fixture",
    callId: "call_instruction_test",
    instructionMarker: marker,
  });
  t.after(() => fixture.close());
  const request = (input: unknown, instructions: string): Promise<Response> =>
    fetch(`${fixture.baseUrl}/responses`, {
      method: "POST",
      headers: {
        authorization: "Bearer fixture-secret",
        "content-type": "application/json",
      },
      body: JSON.stringify({
        model: "fixture-model",
        stream: true,
        input: [
          {
            type: "message",
            role: "developer",
            content: [{ type: "input_text", text: instructions }],
          },
          ...(Array.isArray(input)
            ? input
            : [
                {
                  type: "message",
                  role: "user",
                  content: [{ type: "input_text", text: input }],
                },
              ]),
        ],
        tools: [{ type: "function", name: "exec_command" }],
      }),
    });

  const repeated = await request("run", `${marker}${marker}`);
  assert.equal(repeated.status, 500);
  assert.equal(fixture.snapshot().requests.length, 0);

  const instructions = `Codex base instructions.\n${marker}`;
  const first = await request("run", instructions);
  assert.equal(first.status, 200);
  assert.match(await first.text(), /"id":"resp_fixture_verify_instruction_1"/u);
  const second = await request(
    [
      {
        type: "function_call_output",
        call_id: "call_instruction_test",
        output: "fixture output",
      },
    ],
    instructions,
  );
  assert.equal(second.status, 200);
  assert.match(await second.text(), /"id":"resp_fixture_verify_instruction_2"/u);
});
