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

CI separately runs the unmodified prepared DeepSeek production config with
Harbor's `--install-only` switch and an isolated job namespace. All five tasks
and both frozen arms must start through the production binding. Each of the ten
trials must pin the declared task image to its immutable digest, seal the full
Compose graph, mount the byte-frozen Codex tree read-only, complete Codex setup,
and publish a cleanup receipt after proving that no project container remains.
The validator rejects any agent execution, trajectory, verifier output, reward,
token usage, provider evidence, exception, missing task-arm pair, or leaked
dummy credential. This is a compatibility proof, not a scored benchmark run.

That green gate proves the adapter and isolation machinery, not DeepSeek, GLM,
or Terminal-Bench capability. Fixture metadata is labeled `synthetic-fixture`
and fails both publication gates by design. Before any result is publishable,
live probes must produce consistent returned-model and provider request IDs
without exposing the durable key. Use a disposable key file and a provider-side
spend cap until those gates pass.

The provider-free route harness tests both exact Codex profiles in hosted CI,
but it cannot establish live provider identity. A live route/model-identity
observation must execute inside Harbor's isolated task container while the
durable credential remains in the separate relay service. This live gate is
deliberately narrower than conformance: every prepared run contains a dedicated,
non-scoring `live-route-probe` job. The probe relay is the only service on both
the task's internal network and a separate egress network. It remains closed
until the adapter revalidates the run binding, credential identity, and a
short-lived operator attestation for a provider-side cap. A successful probe
still does not authorize the pilot by itself: the verifier must publish a new
mode-0600 receipt at the fixed path for that run. Every pilot trial revalidates
that receipt, the underlying evidence, the unchanged credential, and its expiry
immediately before opening its relay.

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

The frozen policy sets `runtime.hermeticCodexRuntimeReady` to `true` only after
the native provider-free tool round, direct five-image compatibility preflight,
and production-bound Harbor install-only lifecycle all pass on hosted Linux.
This removes only the runtime blocker. It does not prove provider conformance,
model capability, a benchmark score, or permission to publish one. A live pilot
still requires disposable provider credentials, a provider-side spend cap, and
the evidence gates described above.

For a live pilot, prepare both predeclared repetitions **before the first live
request**, while the same commit is still clean. The analyzer enforces output
roots outside this repository; the operator must also keep provider key files
outside it.
Preparation and execution must use the same Linux Docker daemon; immutable local
relay image IDs are intentionally not portable aliases. The following commands
document the operator procedure; do not run it until the disposable credentials
and spend caps are in place:

```bash
python -m benchmarks.terminal_bench.paired_results prepare \
  /absolute/path/to/oal-screen --replication screen-v1
python -m benchmarks.terminal_bench.paired_results prepare \
  /absolute/path/to/oal-mirror --replication mirror-v1
```

The normal Harbor host process must hash the exact key bytes before Docker
starts, while the relay must lose access after dropping to uid/gid 1000. On
rootful Linux, put each key in a dedicated root-owned directory whose group is
Harbor's effective gid: directory mode 0750, key owner `root:<Harbor gid>`, and
key mode 0440. Every ancestor must be root-owned and not group/other-writable.
Harbor rejects effective gid 1000 because it would leave the relay's post-drop
group able to read the key. The relay also clears supplementary groups and
fails closed if the key remains readable. Its capability is generated
separately for every trial. Do not export the provider key itself into Harbor.

For example, stage a disposable DeepSeek key outside the repository before
preparation (replace the source path with the protected key material supplied
for this run):

```bash
harbor_gid="$(id -g)"
test "$harbor_gid" -ne 1000
sudo install -d -o 0 -g "$harbor_gid" -m 0750 /run/open-agent-lab
sudo install -o 0 -g "$harbor_gid" -m 0440 \
  /absolute/protected/path/to/disposable-deepseek-key \
  /run/open-agent-lab/deepseek-key
export OAL_DEEPSEEK_API_KEY_FILE=/run/open-agent-lab/deepseek-key
```

If the operator's normal effective gid is 1000, an administrator must create a
dedicated group with a different gid, add the operator, and run the entire
prepare/probe/pilot/analyze workflow from a shell whose effective group is that
group (for example, `newgrp open-agent-lab`). Do not relax the gid-1000 guard.

For each provider and each prepared repetition, first confirm a provider-side
cap of at most USD 2. Then write one canonical JSON attestation to the fixed
`authorizations/<provider>.cap.json` path. `observedAt` must precede the probe,
`expiresAt` may be at most 24 hours later, and `evidenceSha256` must identify the
operator-retained cap evidence. `preflightSha256` must equal the same field in
that repetition's `run-record.json`, so cap files cannot be copied across runs.
`providerCredentialSha256` must be the SHA-256 of the exact credential file
mounted into the relay. Keep at least 11 minutes
remaining before the probe and 4 hours 1 minute before every pilot trial so the
authorization covers the relay's entire fixed lifetime. The verifier deliberately labels this
`operator_attested`; it is not independent provider-side proof:

```json
{"assertedBy":"<operator>","evidenceSha256":"sha256:<64 lowercase hex>","expiresAt":"<UTC timestamp>","limitUsd":2,"model":"<exact frozen model>","observedAt":"<UTC timestamp>","preflightSha256":"sha256:<run-record preflightSha256>","proofClass":"live-route-probe-spend-cap-v1","provider":"<deepseek or zai>","providerCredentialSha256":"sha256:<SHA-256 of the exact credential file>","schemaVersion":1,"verification":"operator_attested"}
```

The resulting probe receipt is a frozen-gate audit record, not independent
evidence or a proof against a malicious operator.

The only permitted execution order is probe, verification/receipt publication,
then pilot. For DeepSeek, for example:

```bash
export OAL_DEEPSEEK_API_KEY_FILE="/run/open-agent-lab/deepseek-key"
export OPEN_AGENT_LAB_REPO_ROOT="/absolute/path/to/oal-screen/source"
export PYTHONPATH="$OPEN_AGENT_LAB_REPO_ROOT"
export PYTHONSAFEPATH=1
unset PYTHONHOME DEEPSEEK_API_KEY ZAI_API_KEY
cd "$OPEN_AGENT_LAB_REPO_ROOT"
harbor jobs start \
  --config /absolute/path/to/oal-screen/live-route-probes/deepseek.yaml \
  --yes
python -m benchmarks.terminal_bench.live_route_probe \
  /absolute/path/to/oal-screen \
  --provider deepseek \
  --credential-file "$OAL_DEEPSEEK_API_KEY_FILE" \
  --cap-attestation-file \
    /absolute/path/to/oal-screen/authorizations/deepseek.cap.json \
  --output /absolute/path/to/oal-screen/authorizations/deepseek.json
harbor jobs start \
  --config /absolute/path/to/oal-screen/configs/deepseek.yaml \
  --yes
```

Use `OAL_ZAI_API_KEY_FILE`, `zai`, and the corresponding ZAI paths for GLM.
Repeat the complete sequence for the separately prepared mirror; receipts and
cap attestations cannot be reused across runs. Missing, stale, misplaced,
rewritten, or cross-run receipts fail before the first scored pilot provider
request. The gate also creates a private, one-shot claim for each planned
task/arm slot before opening its relay. An interrupted claimed slot stays
closed; prepare a fresh output root instead of deleting or rewriting a claim.
The probe observes a bounded live route and model identity only; its receipt
sets `liveProviderConformance` to `false` and is not a benchmark score.

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
