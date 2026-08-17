import { resolve } from "node:path";

import { verifyEvidenceBundle } from "@open-agent-lab/evidence";

import { doctor } from "./doctor.js";
import { replaySmokeEvidence, runSmoke } from "./smoke.js";

export interface CliIo {
  stdout: (message: string) => void;
  stderr: (message: string) => void;
}

const defaultIo: CliIo = {
  stdout: (message) => process.stdout.write(`${message}\n`),
  stderr: (message) => process.stderr.write(`${message}\n`),
};

function usage(): string {
  return [
    "Usage:",
    "  open-agent-lab doctor",
    "  open-agent-lab run-smoke --output <directory> [--created-at <ISO-8601>]",
    "  open-agent-lab verify-evidence <directory>",
    "  open-agent-lab replay-smoke <directory>",
  ].join("\n");
}

function option(args: readonly string[], name: string): string | undefined {
  const index = args.indexOf(name);
  if (index === -1) return undefined;
  const value = args[index + 1];
  if (value === undefined || value.startsWith("--")) {
    throw new Error(`${name} requires a value.`);
  }
  return value;
}

function positionalDirectory(args: readonly string[]): string {
  const value = args[0];
  if (value === undefined || value.startsWith("--")) {
    throw new Error("A bundle directory is required.");
  }
  return resolve(value);
}

export async function runCli(args: readonly string[], io: CliIo = defaultIo): Promise<number> {
  const [command, ...rest] = args;
  try {
    switch (command) {
      case "doctor": {
        const report = doctor();
        io.stdout(JSON.stringify(report, null, 2));
        return report.ok ? 0 : 1;
      }
      case "run-smoke": {
        const output = option(rest, "--output");
        if (output === undefined) throw new Error("--output is required.");
        const createdAt = option(rest, "--created-at");
        const summary = await runSmoke({
          outputDirectory: resolve(output),
          ...(createdAt === undefined ? {} : { createdAt }),
        });
        io.stdout(
          JSON.stringify(
            {
              ok: true,
              outputDirectory: summary.outputDirectory,
              manifestId: summary.manifest.manifestId,
              runId: summary.manifest.runId,
            },
            null,
            2,
          ),
        );
        return 0;
      }
      case "verify-evidence": {
        const directory = positionalDirectory(rest);
        const verified = await verifyEvidenceBundle(directory);
        io.stdout(
          JSON.stringify(
            {
              ok: true,
              manifestId: verified.manifest.manifestId,
              runId: verified.manifest.runId,
              fileCount: verified.fileCount,
              totalBytes: verified.totalBytes,
            },
            null,
            2,
          ),
        );
        return 0;
      }
      case "replay-smoke": {
        const summary = await replaySmokeEvidence(positionalDirectory(rest));
        io.stdout(JSON.stringify({ ok: true, ...summary }, null, 2));
        return 0;
      }
      case "--help":
      case "-h":
      case "help":
        io.stdout(usage());
        return 0;
      default:
        io.stderr(command === undefined ? usage() : `Unknown command: ${command}\n${usage()}`);
        return 2;
    }
  } catch (error) {
    io.stderr(error instanceof Error ? error.message : String(error));
    return 1;
  }
}
