# Terminal-Bench 2.1 pilot

This directory freezes the first infrastructure pilot before any outcomes are
observed. It is not a leaderboard submission or a full-suite score.

The adapter subclasses Harbor's official `Codex` implementation. Harbor still
owns Codex installation, container execution, session capture, and ATIF
trajectory conversion; Open Agent Lab only freezes the native Responses
provider. Codex is pinned to `0.149.0`, Harbor to `0.22.0`, and the dataset to
the digest in `pilot-v1.selection.json`.

The adapter resolves no Harbor model connection and receives no durable provider
credential. Each relay generates its own 256-bit capability; Harbor retrieves
it through the sidecar control plane and the shell policy blanks it for normal
tool children. A separate Compose service reads the provider key once from a
Docker secret, drops to uid 1000, accepts only the exact `/v1/responses` model
route, and writes `/var/lib/open-agent-lab/provider-metadata.ndjson`. After
Codex exits, Harbor seals the relay, validates a host-only copy of the redacted
hash chain, and retains the chain and seal as separate artifacts. The official
ATIF trajectory remains unchanged.

## Gates before a publishable run

The boundary and metadata path are implemented and provider-free tests pass, but
this machine has not run the Compose topology. Before results are publishable, a
retained Linux probe must confirm that the task can inspect its own environment,
files and `/proc` but cannot find the durable key or relay process. Live probes
must also produce consistent returned-model and provider request IDs. Use a
disposable key file and a provider-side spend cap until those gates pass.

Five tasks were selected only from the pinned directory names by sorting
`sha256(seed + NUL + task_id)` and taking the first five. No tests, solutions,
or outcomes were used to select them. Both providers use the same tasks, one
attempt, serial execution, official resources/timeouts, and the unmodified
official verifier. This sealed-relay adapter is deliberately single-step and
does not advertise Codex resume support; a future multi-step run needs one relay
per step or a separate trial-end seal hook.

After installing Harbor 0.22.0 and a supported container runtime, set the
repository root and a provider key file **outside this repository**. Inside the
relay container the file must be owned by a different uid than 1000 and be
unreadable after privilege drop (on rootful Linux, use root:root mode 0400).
The relay fails closed otherwise. Its capability is generated separately for
every trial. Do not export the provider key itself into Harbor:

```bash
export OPEN_AGENT_LAB_REPO_ROOT="$PWD"
export OAL_PROVIDER_API_KEY_FILE="/absolute/path/to/deepseek-key"
harbor jobs start \
  --config benchmarks/terminal_bench/pilot-v1.deepseek.yaml \
  --yes
```

```bash
export OPEN_AGENT_LAB_REPO_ROOT="$PWD"
export OAL_PROVIDER_API_KEY_FILE="/absolute/path/to/zai-key"
harbor jobs start \
  --config benchmarks/terminal_bench/pilot-v1.zai.yaml \
  --yes
```

Do not upload this pilot as a leaderboard result. Terminal-Bench 2.1 currently
requires 89 tasks with at least five attempts each and, as of 2026-08-22, only
accepts maintainer-run agents. Failures remain in the denominator. Never inspect
or expose `tests/` or `solution/` to the agent, add task-name routing, or search
the web for task solutions.
