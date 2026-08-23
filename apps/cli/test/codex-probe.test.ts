import assert from "node:assert/strict";
import { chmod, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { runCodexProbe } from "../src/codex-probe.js";

function shellQuote(value: string): string {
  return `'${value.replaceAll("'", `'"'"'`)}'`;
}

test(
  "installed Codex probe rejects an incomplete lifecycle",
  { skip: process.env.OPEN_AGENT_LAB_CODEX_BIN === undefined },
  async (t) => {
    const codex = process.env.OPEN_AGENT_LAB_CODEX_BIN!;
    const directory = await mkdtemp(join(tmpdir(), "open-agent-lab-probe-filter-"));
    t.after(async () => rm(directory, { force: true, recursive: true }));

    for (const { name, renderOutput, expected } of [
      {
        name: "thread.started",
        renderOutput: `grep -Fv ${shellQuote('"type":"thread.started"')} "$output" || :`,
        expected: /thread\.started/u,
      },
      {
        name: "turn.completed",
        renderOutput: `grep -Fv ${shellQuote('"type":"turn.completed"')} "$output" || :`,
        expected: /turn\.completed/u,
      },
      {
        name: "turn.started",
        renderOutput: `grep -Fv ${shellQuote('"type":"turn.started"')} "$output" || :`,
        expected: /turn\.started/u,
      },
      {
        name: "malformed lifecycle spoof",
        renderOutput: `grep -Fv ${shellQuote('"type":"thread.started"')} "$output" || :
printf '%s\\n' ${shellQuote('not-json "type":"thread.started"')}`,
        expected: /invalid JSONL/u,
      },
      {
        name: "nested lifecycle spoof",
        renderOutput: `grep -Fv ${shellQuote('"type":"thread.started"')} "$output" || :
printf '%s\\n' ${shellQuote(
          '{"type":"item.completed","item":{"type":"thread.started"}}',
        )}`,
        expected: /thread\.started/u,
      },
      {
        name: "duplicate thread start",
        renderOutput: `cat "$output"
grep -F ${shellQuote('"type":"thread.started"')} "$output"`,
        expected: /exactly one thread\.started/u,
      },
      {
        name: "duplicate turn completion",
        renderOutput: `cat "$output"
grep -F ${shellQuote('"type":"turn.completed"')} "$output"`,
        expected: /exactly one turn\.completed/u,
      },
      {
        name: "duplicate turn start",
        renderOutput: `cat "$output"
grep -F ${shellQuote('"type":"turn.started"')} "$output"`,
        expected: /exactly one turn\.started/u,
      },
      {
        name: "failed turn after completion",
        renderOutput: `cat "$output"
printf '%s\\n' ${shellQuote('{"type":"turn.failed"}')}`,
        expected: /turn\.failed/u,
      },
      {
        name: "error after completion",
        renderOutput: `cat "$output"
printf '%s\\n' ${shellQuote('{"type":"error"}')}`,
        expected: /error/u,
      },
      {
        name: "reversed lifecycle",
        renderOutput: `grep -Fv ${shellQuote('"type":"thread.started"')} "$output" | grep -Fv ${shellQuote(
          '"type":"turn.completed"',
        )} || :
grep -F ${shellQuote('"type":"turn.completed"')} "$output"
grep -F ${shellQuote('"type":"thread.started"')} "$output"`,
        expected: /thread\.started before turn\.started before turn\.completed/u,
      },
      {
        name: "completion before turn start",
        renderOutput: `grep -Fv ${shellQuote('"type":"turn.started"')} "$output" | grep -Fv ${shellQuote(
          '"type":"turn.completed"',
        )} || :
grep -F ${shellQuote('"type":"turn.completed"')} "$output"
grep -F ${shellQuote('"type":"turn.started"')} "$output"`,
        expected: /thread\.started before turn\.started before turn\.completed/u,
      },
    ]) {
      await t.test(name, async () => {
        const wrapper = join(directory, name);
        await writeFile(
          wrapper,
          `#!/bin/sh
output="$(mktemp)"
trap 'rm -f "$output"' EXIT HUP INT TERM
${shellQuote(codex)} "$@" >"$output"
status=$?
${renderOutput}
exit "$status"
`,
        );
        await chmod(wrapper, 0o700);

        await assert.rejects(runCodexProbe(wrapper), expected);
      });
    }
  },
);
