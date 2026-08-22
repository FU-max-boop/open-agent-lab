import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

import { verifyEvidenceBundle } from "@open-agent-lab/evidence";

import { doctor } from "./doctor.js";
import { runCodexProbe } from "./codex-probe.js";
import {
  buildCodexInvocation,
  publicInvocation,
  runCodexInvocation,
  type OpenModelProvider,
  type ReasoningEffort,
} from "./codex-runner.js";
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
    "  open-agent-lab codex-run --provider <deepseek|zai> --workspace <directory>",
    "    [--model <id>] [--reasoning <low|high|max>]",
    "    (--prompt <text> | --prompt-file <path>) [--codex <path>] [--dry-run]",
    "  open-agent-lab codex-probe [--codex <path>]",
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

function flag(args: readonly string[], name: string): boolean {
  return args.includes(name);
}

function choice<T extends string>(
  value: string | undefined,
  name: string,
  choices: readonly T[],
): T {
  if (value === undefined || !choices.includes(value as T)) {
    throw new Error(`${name} must be one of: ${choices.join(", ")}.`);
  }
  return value as T;
}

async function codexPrompt(args: readonly string[]): Promise<string> {
  const inline = option(args, "--prompt");
  const path = option(args, "--prompt-file");
  if ((inline === undefined) === (path === undefined)) {
    throw new Error("Use exactly one of --prompt or --prompt-file.");
  }
  return inline ?? readFile(resolve(path as string), "utf8");
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
      case "codex-run": {
        const provider = choice<OpenModelProvider>(
          option(rest, "--provider"),
          "--provider",
          ["deepseek", "zai"],
        );
        const workspace = option(rest, "--workspace");
        if (workspace === undefined) throw new Error("--workspace is required.");
        const reasoningValue = option(rest, "--reasoning");
        const reasoning =
          reasoningValue === undefined
            ? undefined
            : choice<ReasoningEffort>(reasoningValue, "--reasoning", [
                "low",
                "high",
                "max",
              ]);
        const model = option(rest, "--model");
        const codexPath = option(rest, "--codex");
        const invocation = buildCodexInvocation({
          provider,
          workspace,
          prompt: await codexPrompt(rest),
          ...(model === undefined ? {} : { model }),
          ...(reasoning === undefined ? {} : { reasoning }),
          ...(codexPath === undefined ? {} : { codexPath }),
        });
        if (flag(rest, "--dry-run")) {
          io.stdout(JSON.stringify(publicInvocation(invocation), null, 2));
          return 0;
        }
        return runCodexInvocation(invocation, process.env, {
          stdout: (chunk) => process.stdout.write(chunk),
          stderr: (chunk) => process.stderr.write(chunk),
        });
      }
      case "codex-probe": {
        const codexPath = option(rest, "--codex");
        const result = await runCodexProbe(codexPath);
        io.stdout(JSON.stringify(result, null, 2));
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
