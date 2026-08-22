#!/usr/bin/env node

import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";

import { runRelayCommand } from "./relay-command.js";
import { startResponsesFixture } from "./responses-fixture.js";

const MODEL = "deepseek-v4-pro";

function isolationCommand(secretHash: string): string {
  return `expected=${secretHash}
for file in /proc/[0-9]*/environ /proc/[0-9]*/cmdline; do
  [ -r "\$file" ] || continue
  while IFS= read -r candidate; do
    case "\$candidate" in *=*) candidate=\${candidate#*=} ;; esac
    [ -z "\$candidate" ] || [ "\$(printf %s "\$candidate" | sha256sum | cut -d' ' -f1)" != "\$expected" ] || exit 42
  done < <(tr '\\0' '\\n' < "\$file" 2>/dev/null)
done
if [ -d /run/secrets ]; then
  for file in /run/secrets/*; do
    [ -r "\$file" ] || continue
    [ "\$(printf %s "\$(cat "\$file")" | sha256sum | cut -d' ' -f1)" != "\$expected" ] || exit 42
  done
fi
printf 'Hello, world!\\n' > /app/hello.txt`;
}

async function main(): Promise<void> {
  const path = process.env.DEEPSEEK_API_KEY_FILE;
  if (path === undefined || process.env.DEEPSEEK_API_KEY !== undefined) {
    throw new Error("Fixture relay requires only DEEPSEEK_API_KEY_FILE.");
  }
  const bearer = (await readFile(path, "utf8")).trim();
  if (bearer === "" || /[\r\n]/u.test(bearer)) throw new Error("Fixture key is invalid.");
  const fixture = await startResponsesFixture({
    bearer,
    model: MODEL,
    command: isolationCommand(createHash("sha256").update(bearer).digest("hex")),
    finalMessage: "Task complete.",
    callId: "call_open_agent_lab_harbor_e2e",
  });
  try {
    await runRelayCommand(process.argv.slice(2), process.env, undefined, {
      deepseek: {
        envKey: "DEEPSEEK_API_KEY",
        endpoint: `${fixture.baseUrl}/responses`,
        models: [MODEL],
        evidenceProviderId: "synthetic-fixture",
      },
    });
  } finally {
    await fixture.close();
  }
}

try {
  await main();
} catch (error) {
  process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
  process.exitCode = 1;
}
