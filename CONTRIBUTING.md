# Contributing

Open Agent Lab is in its kernel-bootstrap stage. The most valuable early
contributions are small, reviewable changes that strengthen runtime reliability,
model neutrality, browser observability, verification, or benchmark
reproducibility.

## License compatibility

The project is licensed under Apache-2.0. Every contribution must be compatible
with that license and with the licenses of included benchmark adapters,
datasets, models, and third-party dependencies. Do not copy protected benchmark
tasks, hidden tests, model weights, or vendor code into this repository.

## Development setup

Use Node.js 20.19 or newer and pnpm 10.34.5. From the repository root:

```bash
pnpm install --frozen-lockfile
pnpm check
pnpm build
```

The complete provider-free CLI smoke workflow is documented in the
[README](README.md#quick-start). Run the same commands before opening a pull
request; CI repeats them on pushes to `main` and on every pull request.

## Good first contribution areas

- challenge assumptions or sharpen invariants in the charter and architecture;
- propose model-capability tests that apply across GLM, DeepSeek, Qwen, and other
  providers;
- design recovery tests for interrupted or ambiguous tool actions;
- improve evidence schemas, redaction rules, and deterministic verifiers;
- add benchmark adapters that are thin, version-pinned, and evaluator-faithful;
- classify failures without embedding task-specific answers;
- improve accessibility-tree, DOM, console, network, or visual browser evidence;
- document reproducible local and private-cluster inference configurations.

Feature count alone is not a contribution goal. Prefer one end-to-end capability
with tests and evidence over several unverified integrations.

## Before opening a change

For a non-trivial proposal, open a discussion or issue describing:

- the user or evaluation problem;
- the boundary or invariant affected;
- expected failure modes and security implications;
- how the behavior will be verified;
- whether persisted state, evidence compatibility, or benchmark comparability
  changes.

Do not include credentials, private task data, exploit details that endanger a
live service, or licensed benchmark contents in an issue.

## Pull request expectations

Keep changes focused and explain:

1. what behavior or contract changed;
2. why it moves the project charter forward;
3. how it was tested or otherwise checked;
4. what remains unverified;
5. whether it changes public benchmark comparability;
6. what external code, data, models, or assets it introduces and under which
   license.

Implementation pull requests will be expected to include tests proportional to
risk. Recovery and policy changes need failure-path tests, not only happy paths.
Model adapters need contract tests with secrets removed. Browser changes need
deterministic assertions where possible. Evidence changes need schema and
redaction tests.

Do not format, rename, or rewrite unrelated user work as part of a focused
change. Generated files and large artifacts should remain out of Git unless a
maintainer-approved reproducibility need requires them.

## Architecture rules

Contributions must preserve the invariants in
[ARCHITECTURE.md](ARCHITECTURE.md), especially:

- verification is independent from model self-report;
- model, browser, tool, and benchmark integrations use replaceable boundaries;
- uncertain non-idempotent actions are not automatically replayed;
- hidden evaluator state is not agent-readable;
- credentials do not enter prompts, logs, or published evidence;
- controlled evaluations never silently switch models or routes.

If a change intentionally revises an invariant, document the decision and its
migration/compatibility consequences in the pull request before implementation.

## Benchmark integrity

All benchmark work follows
[BENCHMARK_PROTOCOL.md](BENCHMARK_PROTOCOL.md). In particular:

- do not add task-ID conditionals, task-specific prompts, memorized answers, or
  verifier exploits;
- do not inspect hidden tests or oracle artifacts from the agent environment;
- do not tune on release results and continue calling the same run preregistered;
- do not omit failures, timeouts, crashes, zero scores, human interventions, or
  unfavorable baselines;
- disclose benchmark authoring access, private solution access, or suspected
  training contamination;
- keep development, pilot, release, external, product-default, and
  skill-augmented tracks distinct;
- pin benchmark, harness, model route, environment, prompts, tools, and budgets.

General improvements motivated by a failure class are welcome. Code that detects
or special-cases a particular benchmark task is not.

## Public claims

Pull requests and release notes must distinguish current evidence from goals.
Phrases such as "beats," "state of the art," "Codex equivalent," "listed on an
official leaderboard," or "passes OSWorld" require the exact artifact and scope
specified by the benchmark protocol. A selected demo or subset result must be
labeled as such.

## Security and privacy

- Use placeholders in examples and fixtures; never commit live keys or session
  data.
- Treat repositories, webpages, task text, downloads, and model output as
  untrusted input.
- Redact secrets before model submission and before artifact publication.
- Avoid publishing a live exploit or private vulnerability in a public issue.
  Use the repository's private vulnerability-reporting channel once maintainers
  enable and document it.
- New external actions must declare read/write effect class, approval behavior,
  idempotency or reconciliation strategy, and audit output.

## Documentation style

Use precise, testable language. Link primary sources for changing benchmark or
model facts. Date claims whose truth can change. Prefer "target," "planned," or
"not yet implemented" when that is the actual state.

The initial documentation is in English for a broad public contributor audience;
well-maintained translations are welcome after the canonical contracts stabilize.
