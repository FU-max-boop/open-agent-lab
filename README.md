# Open Agent Lab

> An open-model optimization, recovery, and evidence layer for open-source Codex.

Open Agent Lab is an early-stage effort to make the open-source Codex agent work
exceptionally well with open-model providers. Codex supplies the inner coding
agent and tool loop; this project supplies native GLM/DeepSeek profiles,
compatibility probes, controlled benchmark variants, outer-run recovery, and
auditable evidence. The target is a competitive, reproducible Terminal-Bench
agent rather than a second implementation of the same loop.

**Status: native Codex/open-model bootstrap.** The repository contains a
recoverable SQLite kernel, strict evidence bundles, and a provider-free
`run -> verify -> replay` smoke path. It also has a credential-hardened Codex
runner for DeepSeek's standard Responses API and the Z.AI Coding Plan Responses
endpoint. The benchmark path adds a single-endpoint byte relay so durable
provider credentials stay outside the task container while returned-model and
request metadata remain auditable.
The older Chat Completions driver remains a diagnostic fallback, not the primary
agent path. A provider-free Linux CI gate now exercises the complete Harbor
0.22/Codex 0.149.0 relay path against Harbor's official `hello-world` task,
including real tool execution, credential-isolation probes, retained relay
evidence, ATIF validation, and Harbor result/lock validation. This is an
infrastructure proof, not a model score. The next frozen experiment compares a
control with one opt-in verification instruction whose exact bytes and hash are
bound into the run evidence; real Codex request tests ensure it is delivered as
a developer message. Hosted CI now runs both frozen DeepSeek and GLM Codex
profiles through an exact provider-free tool round with zero Codex retries,
bounded execution, a sealed relay journal, and a retained safe event projection.
Those bundles are always marked synthetic and never authorize benchmark start.
No DeepSeek or GLM live probe has run, so live conformance and the
Terminal-Bench pilot remain pending. There is no installable release or public
benchmark result yet.

Open Agent Lab is independent from OpenAI and is not an official Codex
distribution.

## Quick start

Requirements: Node.js 20.19 or later within Node 20, pnpm 10.34.5, and Codex
0.149.0 on `PATH` for the Codex commands.

```bash
pnpm install --frozen-lockfile
pnpm check
pnpm build

node apps/cli/dist/index.js doctor
smoke_dir="$(mktemp -d)/bundle"
node apps/cli/dist/index.js run-smoke --output "$smoke_dir"
node apps/cli/dist/index.js verify-evidence "$smoke_dir"
node apps/cli/dist/index.js replay-smoke "$smoke_dir"

# Verify an installed Codex against a provider-free native Responses tool round.
node apps/cli/dist/index.js codex-probe

# Inspect the exact secret-free Codex route before a live call.
node apps/cli/dist/index.js codex-run \
  --provider deepseek \
  --workspace . \
  --prompt "Inspect the repository and report one concrete issue." \
  --dry-run
```

`run-smoke --output` intentionally refuses to overwrite an existing path. The
smoke task is deterministic and provider-free; it is a protocol check, not a
capability benchmark. A live `codex-run` reads `DEEPSEEK_API_KEY` or
`ZAI_API_KEY` from the environment. The key is never placed in argv or generated
configuration. The Z.AI profile uses Coding Plan quota, not the ordinary
pay-as-you-go Chat Completions route.

The frozen, provider-paired Terminal-Bench pilot and its integrity rules live in
[benchmarks/terminal_bench](benchmarks/terminal_bench/README.md). It is an
infrastructure pilot, not a leaderboard score.

## Product thesis

Codex should drive the task; the lab should make open-model behavior measurable
and dependable.

- **Open-model-first:** GLM, DeepSeek, Qwen, and other openly available model
  families are first-class targets. The runtime remains provider-neutral, and
  proprietary models may be optional adapters or comparison ceilings.
- **Browser-native:** browser state is part of the task workspace, not an
  unrelated automation sidecar. Structured DOM/accessibility observations are
  preferred, with visual perception used when structure is insufficient.
- **Recoverable:** a durable journal and checkpoints make long-running tasks
  resumable. Ambiguous external side effects are surfaced rather than silently
  replayed.
- **Verifiable:** completion comes from benchmark evaluators, tests, assertions,
  or explicit human acceptance—not from the model saying that it is done.
- **Auditable:** every published run includes enough configuration, telemetry,
  and redacted trajectory evidence for an independent reader to understand what
  was evaluated.

Open-model-first is not the same as laptop-only. Large open-weight models may be
served by a private cluster or API; smaller or quantized models may run locally.
Results from different weights, quantizations, inference stacks, or provider
routes will be reported as different model variants.

## Evaluation strategy

Our public benchmark strategy separates public leaderboard targets from the
internal engineering suites used to build toward them.

| Stage | Evaluation | Purpose |
| --- | --- | --- |
| First public leaderboard | [Terminal-Bench 2.1](https://www.tbench.ai/leaderboard/terminal-bench/2.1) | Compare agent harnesses under the same model route and budget on terminal and systems workflows. |
| Browser regression | [WebArena-Verified](https://github.com/ServiceNow/webarena-verified) Hard | Detect browser-runtime regressions with an audited, programmatically scored environment. |
| Browser leaderboard | [Online-Mind2Web](https://github.com/OSU-NLP-Group/Online-Mind2Web) | Evaluate live-site browser behavior through the benchmark's submission and review process. |
| Second-stage computer use | [OSWorld 2.0](https://github.com/xlang-ai/OSWorld-V2) | Evaluate long-horizon GUI and cross-application behavior after the core runtime is stable. |

These suites remain separate score columns. We do not average unlike official
metrics into an unofficial headline number. The public scorecard will distinguish
a harness-isolation track, where agent systems share the same model route and
budget, from a model track, where the agent runtime is fixed and model backends
change.

The first directional engineering target is either:

1. at least a five percentage-point gain in the primary official score over a
   pinned, reproducible baseline under the same model route and budget; or
2. statistically supported non-inferior task performance with at least a 25%
   reduction in a predeclared efficiency metric.

This target is not itself proof of superiority. Claims also require the paired
protocol, uncertainty analysis, complete denominators, and artifacts defined in
[BENCHMARK_PROTOCOL.md](BENCHMARK_PROTOCOL.md).

## Intended system

```text
CLI / Harbor / CI
        |
experiment + recovery controller
        |
open-source Codex -- isolated Responses relay -- GLM / DeepSeek
        |
code workspace -> official verifier -> evidence bundle
```

Codex remains upstream rather than becoming a long-lived fork. Provider-specific
code is added only for a measured compatibility gap. The existing kernel and
broker remain useful at the outer run boundary for resume, ambiguous effects,
verification, and publication; they do not duplicate Codex's inner tool loop.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the proposed boundaries. The design is
deliberately modular: no model family, inference server, browser driver, or
third-party agent harness should become an irreplaceable core dependency.

## Delivery gates

1. **Protocol:** charter, architecture, benchmark protocol, license choice, and
   evidence schema are frozen for the first experiment.
2. **Kernel:** a minimal CLI can complete controlled smoke tasks, survive a
   process interruption, and produce verifier-backed evidence.
3. **Pilot:** pinned Terminal-Bench 2.1 and disclosed WebArena-Verified Hard
   development samples run against at least one pinned baseline using the paired
   protocol.
4. **Release evaluation:** full, predeclared suites run with complete attempts,
   uncertainty, cost/time/token telemetry, and public redacted artifacts.
5. **External evaluation:** seek a team-verified Terminal-Bench 2.1 row, then
   submit Online-Mind2Web v2 trajectories under its current review rules, only
   after their disclosure packages are ready.
6. **Computer use:** ship the shared browser/desktop workspace, then evaluate on
   one pinned OSWorld 2.0 release without mixing release components.

Detailed exit criteria live in [CHARTER.md](CHARTER.md).

## Repository documents

- [CHARTER.md](CHARTER.md): mission, scope, principles, and phase gates
- [ARCHITECTURE.md](ARCHITECTURE.md): target components, trust boundaries, and
  recovery semantics
- [BENCHMARK_PROTOCOL.md](BENCHMARK_PROTOCOL.md): fair-comparison and publication
  rules
- [CONTRIBUTING.md](CONTRIBUTING.md): contribution and benchmark-integrity rules
- [ROADMAP.md](ROADMAP.md): current two-week gate and its exit criteria
- [docs/model-drivers.md](docs/model-drivers.md): GLM/DeepSeek adapter contract,
  limitations, and live-conformance gate
- [benchmarks/terminal_bench](benchmarks/terminal_bench/README.md): pinned Harbor
  adapter and predeclared Terminal-Bench 2.1 pilot
- [docs/decisions](docs/decisions): accepted persistence and recovery decisions

## License

Open Agent Lab is licensed under the [Apache License 2.0](LICENSE).
