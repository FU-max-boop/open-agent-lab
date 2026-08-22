#!/usr/bin/env node

import { runRelayCommand } from "./relay-command.js";

try {
  await runRelayCommand(process.argv.slice(2));
} catch (error) {
  process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
  process.exitCode = 1;
}
