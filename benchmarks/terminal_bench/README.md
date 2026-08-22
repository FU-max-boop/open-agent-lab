# Terminal-Bench 2.1 pilot

This directory freezes the first infrastructure pilot before any outcomes are
observed. It is not a leaderboard submission or a full-suite score.

The adapter subclasses Harbor's official `Codex` implementation. Harbor still
owns container execution, session capture, and ATIF trajectory conversion.
Open Agent Lab replaces Harbor's mutable NVM/npm install step with one
byte-pinned native Codex tree and freezes the native Responses provider. Codex
is pinned to `0.149.0`, Harbor to `0.22.0`, and the dataset to the digest in
`pilot-v1.selection.json`.

The adapter resolves no Harbor model connection and receives no durable provider
credential. Each relay generates its own 256-bit capability; Harbor retrieves
it through the sidecar control plane and the shell policy blanks it for normal
tool children. The relay starts in a bootstrap-only state without reading the
Docker secret. Harbor rechecks the clean source and manifest, then verifies the
running image's embedded build ID, provider, and exact model against the
prepared trial. Only then may the relay read the secret. The Harbor adapter is
imported from the prepared detached source clone rather than the mutable
development checkout. It drops to uid 1000,
accepts only the exact `/v1/responses` model route, and writes
`/var/lib/open-agent-lab/provider-metadata.ndjson`. After
Codex exits, Harbor seals the relay, validates a host-only copy of the redacted
hash chain, and retains the chain and seal as separate artifacts. The official
ATIF trajectory remains unchanged.

## Gates before a publishable run

The provider-free templates in `harbor-e2e.yaml` and
`harbor-verify-instruction-e2e.yaml` cannot run unbound. CI first uses the same
clean-source preflight as a live run, then runs the generated fixture configs on
Linux with Harbor 0.22 and Codex 0.149.0. A deterministic native Responses fixture
makes Codex issue one real `exec_command` inside Harbor's official `hello-world`
task. The treatment gate also fails unless the exact frozen instruction appears
once in Codex's developer-message request envelope. The JobLock preserves the
named arm and strict opt-in switch; the instruction hash is bound separately by
the frozen experiment manifest, result metadata, and Harbor binding. The command
probes task-visible files, `/proc`, and mounted secrets for the fixture
credential's hash before completing the task. Both gates require the official
reward, two linked relay requests, a valid seal, retained metadata, matching
Harbor result/lock records, and a matching ATIF trajectory. The production relay
image is also scanned as an OCI archive to ensure fixture code is absent from
every image layer.

That green gate proves the adapter and isolation machinery, not DeepSeek, GLM,
or Terminal-Bench capability. Fixture metadata is labeled `synthetic-fixture`
and fails both publication gates by design. Before any result is publishable,
live probes must produce consistent returned-model and provider request IDs
without exposing the durable key. Use a disposable key file and a provider-side
spend cap until those gates pass.

Five tasks were selected only from the pinned directory names by sorting
`sha256(seed + NUL + task_id)` and taking the first five. No tests, solutions,
or outcomes were used to select them. Both providers use the same tasks, one
attempt, serial execution, official resources/timeouts, and the unmodified
official verifier. The configs freeze Harbor 0.22's package-qualified
`terminal-bench/<task>` names and resolved registry order so filtering and the
serial arm-order audit agree with the actual runtime. This sealed-relay adapter
is deliberately single-step and
does not advertise Codex resume support; a future multi-step run needs one relay
per step or a separate trial-end seal hook.

`pilot-v2.deepseek.yaml` and `pilot-v2.zai.yaml` are immutable templates for the
same five tasks with adjacent control and `verify-instruction-v1` arms. The only
intended difference is the frozen `developer_instructions` value. The switch
only requests that instruction; it does not assert that Codex performed a
verification pass or expose a Codex feature with that name.

`paired_results.py prepare` is the only production-run preparation entry point.
It requires a clean commit, creates a self-contained detached clone, validates
every frozen file there, materializes each exact task package into a private
non-overwriting run directory, and checks both its Harbor content digest and
directory hash. It also requires `OPEN_AGENT_LAB_CODEX_ARCHIVE` to name the
absolute path of the pinned Linux x64 Codex archive, verifies every archive and
member byte, and prepares one exact runtime tree shared read-only by all trials.
It then builds the production and fixture relay images from the exact source
snapshot. Each generated relay Compose document removes the build
context, pins the immutable Docker image ID, and sets `pull_policy: never`. A
deterministic run-owned local tag retains each exact relay image against ordinary
Docker pruning without becoming the runtime authority. The preflight hash,
source revision, manifest hash, task-snapshot authority, relay build ID,
immutable image ID, and replication ID are carried through each TrialLock and
sealed Harbor binding.

A frozen Harbor 0.22 environment reads the prepared overlay once, then copies
every effective Harbor Compose input into sealed Linux memory. It resolves and
validates the complete graph once: the relay must retain its exact image and
security profile, the provider secret may be attached only to that relay, and a
production trial must use one of the private task snapshots. Before container
start, the task bytes are rehashed, the declared task tag must match, and its
image is replaced with the frozen registry manifest digest after validating the
exact config digest and `linux/amd64` platform. The resolved graph is sealed
again and every build, start, exec, copy, and cleanup command addresses that one
parent-held memory file. Compose therefore cannot reread a swapped task or
overlay path between validation and container start. Cleanup retries the exact
project three times, proves that no project-labelled container remains, and
publishes a non-overwriting receipt consumed by the analyzer. Before provider
work, the adapter also rechecks that both custom modules came from the prepared
clone, as well as the manifest, running relay build ID, provider, and model.

The analyzer reads every child trial rather than Harbor's arm-mixing job
aggregates. It rejects missing or duplicate pairs, absent official rewards,
model or arm drift, invalid token arithmetic, non-serial execution, and corrupt
or non-redacted relay evidence. Scored exceptions remain in the official
denominator; unavailable provider-token or trajectory telemetry is emitted as
`null`, reduces explicit coverage, and blocks a complete directional analysis.
Harbor's environment, setup, agent, and verifier phase timings are mandatory
and must form one ordered lifecycle.
`screen-v1` and `mirror-v1`
freeze opposite within-provider orders. A screen alone is always
`not_promotable`; even both repetitions remain a five-task directional
development result, never a significance, official, or leaderboard claim.

The current frozen policy deliberately keeps
`runtime.hermeticCodexRuntimeReady` at `false` while the new byte-frozen runtime
is proved in hosted provider-free control and treatment trials. Every production
agent therefore still fails closed before a provider request. Only a reviewed
follow-up policy change may flip the gate after that proof is green. Do not
create or mount provider key files for this experiment yet, and do not report a
live score from it.

After a reviewed policy revision flips that gate, prepare both predeclared
repetitions **before the first live request**, while the same commit is still
clean. The output roots and provider key files must be outside this repository.
Preparation and execution must use the same Linux Docker daemon; immutable local
relay image IDs are intentionally not portable aliases. The following commands
document that future operator procedure and are not currently authorized:

```bash
python -m benchmarks.terminal_bench.paired_results prepare \
  /absolute/path/to/oal-screen --replication screen-v1
python -m benchmarks.terminal_bench.paired_results prepare \
  /absolute/path/to/oal-mirror --replication mirror-v1
```

Inside the relay container the key file must be owned by a different uid than
1000 and be unreadable after privilege drop (on rootful Linux, use root:root
mode 0400). The relay fails closed otherwise. Its capability is generated
separately for every trial. Do not export the provider key itself into Harbor,
and never run the immutable template directly:

```bash
export OAL_DEEPSEEK_API_KEY_FILE="/absolute/path/to/deepseek-key"
export OPEN_AGENT_LAB_REPO_ROOT="/absolute/path/to/oal-screen/source"
export PYTHONPATH="$OPEN_AGENT_LAB_REPO_ROOT"
export PYTHONSAFEPATH=1
unset PYTHONHOME DEEPSEEK_API_KEY ZAI_API_KEY
cd "$OPEN_AGENT_LAB_REPO_ROOT"
harbor jobs start \
  --config /absolute/path/to/oal-screen/configs/deepseek.yaml \
  --yes
```

```bash
export OAL_ZAI_API_KEY_FILE="/absolute/path/to/zai-key"
export OPEN_AGENT_LAB_REPO_ROOT="/absolute/path/to/oal-screen/source"
export PYTHONPATH="$OPEN_AGENT_LAB_REPO_ROOT"
export PYTHONSAFEPATH=1
unset PYTHONHOME DEEPSEEK_API_KEY ZAI_API_KEY
cd "$OPEN_AGENT_LAB_REPO_ROOT"
harbor jobs start \
  --config /absolute/path/to/oal-screen/configs/zai.yaml \
  --yes
```

Then produce a deterministic redacted screen summary. It exits 0 only when the
evidence is valid; the result is still `not_promotable` until the separately
prepared mirror is run and included, and this development experiment never
becomes a leaderboard result:

```bash
python -m benchmarks.terminal_bench.paired_results summarize \
  /absolute/path/to/oal-screen \
  --output /absolute/path/to/oal-screen-summary.json
```

After all trials, validation, and any intended reruns are finished, remove only
the two verified tags owned by each prepared directory:

```bash
python -m benchmarks.terminal_bench.paired_results cleanup-images \
  /absolute/path/to/oal-screen
python -m benchmarks.terminal_bench.paired_results cleanup-images \
  /absolute/path/to/oal-mirror
```

Do not upload this pilot as a leaderboard result. Terminal-Bench 2.1 currently
requires 89 tasks with at least five attempts each and, as of 2026-08-22, only
accepts maintainer-run agents. Failures remain in the denominator. Never inspect
or expose `tests/` or `solution/` to the agent, add task-name routing, or search
the web for task solutions.
