import assert from "node:assert/strict";
import test from "node:test";

import { startResponsesFixture } from "../src/responses-fixture.js";

const TOOLS = [
  { type: "function", name: "exec_command" },
  {
    type: "custom",
    name: "apply_patch",
    format: { type: "grammar", syntax: "lark", definition: "start: /.+/" },
  },
];

test("Responses fixture rejects ambiguous turn state configuration", async () => {
  await assert.rejects(
    startResponsesFixture({
      bearer: "fixture-secret",
      model: "fixture-model",
      command: "printf fixture",
      turnState: "first,second",
    }),
    /Fixture turn state is invalid/u,
  );
});

test("Responses fixture detects an empty apply_patch grammar", async (t) => {
  const fixture = await startResponsesFixture({
    bearer: "fixture-secret",
    model: "fixture-model",
    command: "printf fixture",
  });
  t.after(() => fixture.close());
  const response = await fetch(`${fixture.baseUrl}/responses`, {
    method: "POST",
    headers: {
      authorization: "Bearer fixture-secret",
      "content-type": "application/json",
    },
    body: JSON.stringify({
      model: "fixture-model",
      stream: true,
      input: "run",
      tools: [
        TOOLS[0],
        { ...TOOLS[1], format: { type: "grammar", syntax: "lark", definition: " " } },
      ],
    }),
  });
  assert.match(await response.text(), /apply_patch grammar/u);
});

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
        tools: TOOLS,
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
          tools: TOOLS,
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
          tools: TOOLS,
        },
      },
    ],
    toolName: "exec_command",
    toolOutput: "fixture output",
  });
});

test("Responses fixture completes one real custom-tool round", async (t) => {
  const fixture = await startResponsesFixture({
    bearer: "fixture-secret",
    model: "fixture-model",
    patch: "*** Begin Patch\n*** End Patch\n",
    callId: "call_patch_test",
  });
  t.after(() => fixture.close());
  const request = (input: unknown): Promise<Response> =>
    fetch(`${fixture.baseUrl}/responses`, {
      method: "POST",
      headers: {
        authorization: "Bearer fixture-secret",
        "content-type": "application/json",
      },
      body: JSON.stringify({ model: "fixture-model", stream: true, input, tools: TOOLS }),
    });

  const first = await request("apply the patch");
  assert.equal(first.status, 200);
  const firstBody = await first.text();
  assert.match(firstBody, /"type":"custom_tool_call"/u);
  assert.match(firstBody, /"call_id":"call_patch_test"/u);
  assert.match(firstBody, /"name":"apply_patch"/u);

  const validOutput = {
    type: "custom_tool_call_output",
    call_id: "call_patch_test",
    output: "Exit code: 0",
  };
  for (const invalid of [
    [{ ...validOutput, type: "function_call_output" }],
    [{ ...validOutput, call_id: "call_wrong" }],
    [validOutput, validOutput],
  ]) {
    const rejected = await request(invalid);
    const rejectedBody = await rejected.text();
    assert.match(rejectedBody, /invalid fixture tool output/u);
    assert.doesNotMatch(rejectedBody, /response\.completed/u);
    assert.equal(fixture.snapshot().toolOutput, "");
  }

  const completed = await request([validOutput]);
  assert.equal(completed.status, 200);
  assert.match(await completed.text(), /response\.completed/u);
  const snapshot = fixture.snapshot();
  assert.equal(snapshot.requests.length, 5);
  assert.equal(snapshot.toolName, "apply_patch");
  assert.equal(snapshot.toolOutput, "Exit code: 0");
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
        tools: TOOLS,
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
