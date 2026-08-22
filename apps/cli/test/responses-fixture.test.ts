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
