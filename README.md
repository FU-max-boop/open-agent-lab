# Open Agent Lab

> An open-model-first, browser-native, recoverable, and verifiable agent runtime.

Open Agent Lab is an early-stage effort to build a Codex-class software and
computer-use agent without making a closed model or a single provider a product
dependency. The intended system combines repository work, terminal execution,
and browser interaction in one resumable task runtime, with machine-checkable
outcomes and auditable evidence.

**Status: kernel bootstrap.** The repository now contains versioned run/event
contracts, a strict content-addressed evidence bundle, and a provider-free
`run -> verify -> replay` smoke path. There is still no installable release or
public benchmark result. This repository must not be cited as outperforming
another agent, appearing on an official leaderboard, or completing OSWorld.
Those are targets, not current claims.

"Codex-class" describes the intended breadth of the workflow; it does not imply
affiliation with or equivalence to OpenAI Codex.

## Quick start

Requirements: Node.js 20.19 or newer and pnpm 11.19.0.

```bash
pnpm install --frozen-lockfile
pnpm check
pnpm build

node apps/cli/dist/index.js doctor
smoke_dir="$(mktemp -d)/bundle"
node apps/cli/dist/index.js run-smoke --output "$smoke_dir"
node apps/cli/dist/index.js verify-evidence "$smoke_dir"
node apps/cli/dist/index.js replay-smoke "$smoke_dir"
```

`run-smoke --output` intentionally refuses to overwrite an existing path. The
smoke task is deterministic and provider-free; it is a protocol check, not a
capability benchmark.

## Product thesis

The model should make judgments; the runtime should make execution dependable.

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
CLI / Desktop / CI
        |
Task kernel -- model gateway -- policy and tool broker
        |                 |
Code workspace       Browser workspace
        \                 /
         journal -> verifier -> evidence bundle
```

The implementation is expected to provide typed model and tool interfaces,
shell/file/patch/git/test operations, browser inspection and interaction,
checkpoint/resume, deterministic verification where possible, and benchmark
adapters that do not leak evaluator state into the agent.

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

## License

Open Agent Lab is licensed under the [Apache License 2.0](LICENSE).
