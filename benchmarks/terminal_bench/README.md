# Terminal-Bench 2.1 pilot

This directory freezes the first infrastructure pilot before any outcomes are
observed. It is not a leaderboard submission or a full-suite score.

The adapter subclasses Harbor's official `Codex` implementation. Harbor still
owns Codex installation, container execution, session capture, and ATIF
trajectory conversion; Open Agent Lab only freezes the native Responses
provider. Codex is pinned to `0.149.0`, Harbor to `0.22.0`, and the dataset to
the digest in `pilot-v1.selection.json`.

The adapter also closes Harbor's default credential-file path: Codex reads the
provider-native environment variable, Harbor receives an empty compatibility
`auth.json`, and the normal shell-tool environment sees the selected key only
as an empty string.

## Gates before a publishable run

These configs freeze the experiment but are not yet publication-ready. Harbor
0.22 launches Codex with its internal sandbox disabled; on Linux, same-UID code
may be able to inspect the parent Codex environment through `/proc` despite the
shell filter. A credential relay or tested OS isolation boundary, plus a Linux
regression probe, is required before using a durable provider key. Also, Harbor
0.22's ATIF path preserves the requested model but not a provider-returned model
identity or transport request ID. A raw-response metadata sidecar must close
that evidence gap before results are published.

Use a disposable, tightly limited key only for private infrastructure debugging
until both gates pass.

Five tasks were selected only from the pinned directory names by sorting
`sha256(seed + NUL + task_id)` and taking the first five. No tests, solutions,
or outcomes were used to select them. Both providers use the same tasks, one
attempt, serial execution, official resources/timeouts, and the unmodified
official verifier.

After installing Harbor 0.22.0 and a supported container runtime, the private
infrastructure command is:

```bash
export DEEPSEEK_API_KEY="..."
harbor jobs start \
  --config benchmarks/terminal_bench/pilot-v1.deepseek.yaml \
  --yes
```

```bash
export ZAI_API_KEY="..."
harbor jobs start \
  --config benchmarks/terminal_bench/pilot-v1.zai.yaml \
  --yes
```

Do not upload this pilot as a leaderboard result. Terminal-Bench 2.1 currently
requires 89 tasks with at least five attempts each and, as of 2026-08-22, only
accepts maintainer-run agents. Failures remain in the denominator. Never inspect
or expose `tests/` or `solution/` to the agent, add task-name routing, or search
the web for task solutions.
