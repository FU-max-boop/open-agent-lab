# Project Charter

## Mission

Build and publicly release an open-model-first, browser-native agent that can
perform substantial software and computer-use work, recover from interruption,
and prove outcomes with independent verification and auditable evidence.

The project succeeds only when the implementation and public evidence exist. A
design document, selected demo, or unverified self-report is progress—not
completion.

## Principles

1. **Open models are first-class.** GLM, DeepSeek, Qwen, and other openly
   available model families must not be disadvantaged by assumptions tailored to
   one proprietary API. Open-model-first does not require every model to run on
   a laptop or prohibit optional proprietary adapters.
2. **The runtime owns reliability.** Typed tools, bounded retries, durable state,
   idempotency controls, and verification compensate for imperfect model output.
3. **The browser is a workspace.** Browser state, DOM/accessibility structure,
   screenshots, console signals, and network evidence participate in the same
   task lifecycle as code and terminal state.
4. **Verification outranks narration.** An agent's claim of success has no
   scoring value without the applicable external evaluator, test, assertion, or
   human acceptance record.
5. **Recovery must be honest.** The runtime may replay safe operations, but it
   must mark uncertain non-idempotent effects and request a decision rather than
   pretending exactly-once execution.
6. **Benchmark comparisons must isolate the harness.** Primary comparisons use
   the same exact model route and resource envelope. Product-default comparisons
   are useful but belong in a separate track.
7. **Evidence before publicity.** No score, ranking, affiliation, or adoption
   claim is published without a reproducible artifact and a precise scope.
8. **No benchmark-specific shortcuts.** General improvements may learn from
   failure categories; task-ID branches, memorized answers, evaluator access, and
   hidden-test targeting are prohibited.

## Scope

### In scope

- A task kernel with cancellation, checkpoints, resume, and a durable event
  journal.
- Model adapters and capability profiles for open-weight/openly available model
  families and OpenAI-compatible inference endpoints.
- Typed, policy-controlled tools for files, patches, Git, terminal commands,
  tests, and browser interaction.
- A shared browser workspace with structured observations, visual fallback,
  browser debugging signals, and optional human takeover.
- Independent completion verification and portable, redacted evidence bundles.
- CLI-first benchmark integration, followed by a desktop workspace and CI mode.
- Reproducible evaluation on Terminal-Bench 2.1 and WebArena-Verified Hard,
  followed by Online-Mind2Web and later OSWorld 2.0.
- Security controls for credentials, untrusted content, prompt injection, and
  consequential external actions.

### Out of scope for the first public alpha

- Training a foundation model.
- Claiming universal autonomy or safe unattended operation on arbitrary systems.
- A marketplace of site-specific automations.
- Office and security benchmark leadership before the coding/browser kernel is
  stable.
- Multi-agent orchestration merely to increase apparent feature count.
- A polished desktop shell before the CLI runtime can be evaluated and resumed.

### Permanent non-goals

- Binding the product to one model vendor, inference server, or benchmark.
- Optimizing a public score through task-specific prompts, answer lookup, or
  verifier exploitation.
- Hiding failures, excluded tasks, human interventions, or infrastructure errors.
- Treating an LLM judge as deterministic ground truth when a programmatic
  verifier is available.
- Advertising inclusion in a third-party index before that third party lists the
  evaluated agent variant.

## Users

- Developers who want an auditable agent for code-plus-browser workflows.
- Teams that need local/private model deployment and recoverable execution.
- Researchers comparing model/harness combinations under controlled budgets.
- Contributors building model, tool, browser, verifier, and benchmark adapters.

## Definitions of success

Project-level success requires all of the following:

- a publicly usable, OSI-licensed release;
- first-class documented runs with at least two open-model families;
- a reproducible paired Terminal-Bench 2.1 evaluation and an accepted,
  team-verified official leaderboard row;
- a reproducible WebArena-Verified Hard evaluation and an accepted or verified
  Online-Mind2Web leaderboard row;
- a browser/desktop workflow evaluated on a pinned OSWorld release; and
- evidence of external use that is not limited to maintainers' own demos.

The benchmark target is an instrument, not the whole product. Reliability,
security, recovery fidelity, cost, latency, usability, and external adoption are
reported alongside task scores.

## Phase gates

### Gate 0 — Reproducibility contract

Required before implementation claims or external code contributions:

- charter, architecture, and benchmark protocol reviewed and versioned;
- OSI-approved license selected and committed;
- run manifest and evidence bundle schemas specified;
- initial benchmark releases, task manifests, baseline variants, model routes,
  and budgets predeclared;
- security and secret-redaction expectations documented.

### Gate 1 — Minimal recoverable kernel

Required before browser or desktop product work becomes the main focus:

- one command runs a controlled task from a clean workspace;
- model, shell, file, patch, Git, and test boundaries are typed and logged;
- process interruption and resume are tested at multiple tool boundaries;
- duplicate safe actions are suppressed, while ambiguous side effects enter an
  explicit `needs_review` state;
- at least 20 smoke-task attempts produce complete verifier-backed evidence;
- credentials are absent from the committed repository and published evidence.

### Gate 2 — Paired benchmark pilot

Required before optimization claims:

- a predeclared Terminal-Bench 2.1 pilot and a disclosed sample of at least five
  tasks drawn from a pinned WebArena-Verified Hard manifest run end to end;
- our harness and at least one credible, pinned baseline use the same exact model
  route and budget envelope;
- at least two open-model families are represented across the pilot program;
- all attempts, including failures and infrastructure errors, are accounted for;
- failures are classified without adding task-ID-specific behavior.

Pilot subsets are development instruments and cannot be promoted as full-suite
leaderboard results.

### Gate 3 — Reproducible release evaluation

Required before saying the harness has an advantage:

- complete, predeclared benchmark suites are evaluated with three attempts per
  task unless the official protocol requires otherwise;
- the official evaluator is used without agent access to evaluator-only state;
- paired point estimates, 95% uncertainty intervals, task denominators, and
  exclusions are published;
- raw manifests, trajectories, verifier outputs, and aggregate scripts are
  public after redaction;
- the result meets a claim rule in `BENCHMARK_PROTOCOL.md`—not merely a favorable
  best run.

The directional target is at least +5 percentage points in the primary score, or
non-inferior performance with at least 25% improvement in a predeclared
efficiency metric. The statistical and disclosure rules still apply.

### Gate 4 — External leaderboard readiness

Required before requesting independent evaluation:

- adapters support the Terminal-Bench 2.1 and Online-Mind2Web submission formats
  current at submission time;
- the evaluated agent variant, defaults, model route, and telemetry can be
  reproduced from a release tag;
- default behavior does not inspect benchmark identity or evaluator state;
- maintainers have prepared a complete third-party disclosure package.

Passing this gate permits an evaluation submission. Only an accepted official
row proves leaderboard inclusion.

### Gate 5 — Computer-use product

Required before a Codex-class computer-use claim:

- desktop/shared-browser workflow supports inspection, action, human takeover,
  cancellation, resume, and evidence replay;
- at least one realistic code-to-browser repair flow is verified end to end;
- a full evaluation uses one internally consistent, pinned OSWorld 2.0 release;
- consequential actions and prompt-injection cases exercise the policy boundary;
- an external adopter can install and reproduce a documented workflow.

## Decision and change policy

Architecture decisions that affect persisted state, tool permissions, benchmark
comparability, or public claims require a short decision record in the pull
request or a future ADR. Benchmark protocol changes apply prospectively: old and
new results remain separately labeled and are not silently combined.

Maintainers may revise numeric goals as benchmark quality changes, but must
preserve raw prior results and explain why the target moved.
