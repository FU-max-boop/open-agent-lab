# Benchmark Protocol

**Protocol version:** 0.1

**Effective date:** 2026-08-17

This protocol governs internal decisions and public claims. It is intentionally
stricter than running a benchmark command once: the object under evaluation is a
specific **agent variant**—model route, harness, settings, tools, environment,
and budget—not a brand name or a model name alone.

## Evaluation program

### Engineering tracks

1. **Terminal-Bench 2.1:** the first terminal and systems-workflow suite. The
   official release describes 2.1 as a revision that fixes 28 of the 89
   Terminal-Bench 2.0 tasks and adds continuous validation.
2. **WebArena-Verified Hard:** the browser-runtime engineering suite. Development
   uses disclosed samples from a pinned Hard manifest; only a complete eligible
   run is described as a Hard result.

### External headline target

The first headline target is a valid submission to the official
[Terminal-Bench 2.1 leaderboard](https://www.tbench.ai/leaderboard/terminal-bench/2.1).
The browser headline target is a v2 trajectory submission to
[Online-Mind2Web](https://github.com/OSU-NLP-Group/Online-Mind2Web), with its
automatic or human evaluation status disclosed. We do not compute an unofficial
score and present it as an accepted official result.

Terminal-Bench, WebArena-Verified, and Online-Mind2Web scores remain separate.
They have different environments, outcome definitions, and submission rules and
are not averaged into a project-wide headline number.

### Computer-use target

OSWorld follows after the recoverable shared-browser/desktop product exists. An
OSWorld 2.0 run must pin one official benchmark release and use its code, task
classes, assets, websites, and provider images as specified by the same release
manifest. Components from different releases may not be mixed.

## Run classes

Every run is labeled as one of:

| Class | Permitted use |
| --- | --- |
| `development` | Prompt/tool debugging on an explicitly disclosed development subset. Never a headline result. |
| `pilot` | Predeclared, hash-pinned subset used to validate infrastructure and estimate variance. Reported only with its exact denominator. |
| `release` | Full eligible suite or official third-party task set, frozen configuration, complete attempts, and publication artifacts. |
| `external` | Run and scored by an independent benchmark operator under its rules. |

A selected subset is never described as the full benchmark. A best-of-N run is
never described as pass@1.

## Pre-registration

Before a pilot or release run starts, commit a machine-readable manifest and a
human-readable note containing:

- repository release/commit for the agent and baseline harnesses;
- benchmark name, release, task manifest, checksums, and declared exclusions;
- model route and full variant identity;
- inference settings, context limit, total generated-token cap, and cache policy;
- task/agent/verifier timeouts, maximum turns, retry policy, and concurrency;
- tool set, network policy, filesystem/CPU/memory/GPU limits, and browser image;
- system prompt hashes and any general skills available to each harness;
- number of attempts, random seeds where supported, and task order policy;
- primary outcome, secondary metrics, uncertainty method, and claim rule;
- baseline choice and the reason it is credible;
- infrastructure-error and rerun policy;
- expected judge models/versions for evaluator-required model judging;
- artifact publication and redaction plan.

Configuration changes after seeing outcomes create a new experiment ID. The old
run remains in the record.

## Fair comparison tracks

### Primary: harness-isolation track

To attribute a difference to the agent harness, compared variants use:

- the same API/provider route or the same self-hosted weights and inference stack;
- the same exact model revision, precision/quantization, and region where relevant;
- the same sampling/reasoning settings and prompt-visible context limit;
- the same total generated-token cap, wall-clock timeout, turn cap, retry cap,
  network access, and machine class;
- the same task order policy, benchmark assets, verifier, and number of attempts;
- equivalent tool capabilities, except when the declared subject of the test is a
  tool or browser design difference.

If a baseline cannot use the same route or envelope, it is not included in the
primary paired claim. It may appear in a clearly labeled reference table.

### Secondary: product-default track

Each product may use its documented defaults, preferred model, routing, tools,
and budget. This measures user-facing packages, not harness quality in isolation.
It must be labeled `product-default` and cannot be used to claim that one harness
is better under the same model.

### Skill-augmented track

General, predeclared skills available for every task may be evaluated separately.
Task-ID routing, task-specific prompts, benchmark answer files, and post hoc skill
selection are prohibited. Skill-augmented scores never replace the general-track
score.

## Model identity and open-model reporting

`Open model` is an imprecise label, so reports state the concrete access mode:

- open weights, license, revision/digest, and quantization for self-hosted runs;
- third-party API serving an open-weight model, with exact provider route;
- proprietary weights/API where applicable.

GLM, DeepSeek, Qwen, or any other family name is insufficient identification.
Silent fallbacks, model cascades, speculative routing, or server-side model
changes invalidate a harness-isolation run unless they are the predeclared
variant being tested.

## Task integrity and contamination

- Release task IDs may not influence prompts, tools, routing, budgets, or stop
  conditions.
- The agent may not search the public internet for benchmark task text or
  solutions while running, unless the benchmark explicitly requires and permits
  that behavior. General dependency access follows the pinned network policy.
- Hidden tests, evaluator code, oracle artifacts, and setup secrets stay outside
  the agent-readable environment unless the official task exposes them.
- Contributors disclose prior access to task solutions, benchmark development,
  private test data, or fine-tuning contamination that could affect a result.
- Development failures are fixed by general failure class. A task-specific fix
  moves the variant to a disclosed specialized track and disqualifies it from the
  general headline.
- Prompt and tool changes freeze before the release task set is run. If a public
  benchmark cannot support a genuinely blind split, that limitation is stated.

## Attempts, failures, and reruns

Release evaluations use three independent attempts per task by default. If an
official protocol requires another count, the official count wins and is
recorded. Each attempt starts from the same clean task state and receives a
unique run ID.

An agent timeout, crash, invalid final artifact, verifier failure, or unhandled
model error scores according to the official evaluator—normally zero—and remains
in the denominator. Missing telemetry is marked missing, never replaced with
zero or silently omitted.

An attempt may be declared an infrastructure incident only under a predeclared,
harness-independent rule such as host loss or confirmed benchmark service
outage. The decision is logged before inspecting task quality. Reruns apply
symmetrically to compared variants; original incident records remain public. API
rate limits and agent bugs are not automatically infrastructure incidents.

Human intervention is zero in the primary autonomous track. If a person edits,
clicks, approves an otherwise blocked action, supplies new information, or
chooses a recovery path, the run is labeled assisted and reported separately.

## Scoring and uncertainty

The primary outcome for each suite is its unmodified official evaluator score.
We publish the per-task, per-attempt official outcome and compute task-normalized
means so each task has equal weight unless the official protocol says otherwise.

For paired comparisons:

1. pair variants by task and attempt index;
2. compute the task-level mean for each variant;
3. compute paired task-level differences;
4. report the mean difference in percentage points and a two-sided 95% paired
   bootstrap confidence interval over tasks (at least 10,000 resamples, with the
   seed recorded).

For evaluator pipelines with stochastic model judges, preserve the official
judge configuration and report judge-level variance or repeat policy when the
benchmark provides it. Do not describe a model-judged score as deterministic.

### Public claim rules

`Outperforms under the paired protocol` requires:

- a release or external run;
- a positive paired mean difference;
- a 95% interval whose lower bound is above zero; and
- a practically meaningful gain of at least five percentage points in the
  predeclared primary score.

`Performance non-inferior and more efficient` requires:

- a release or external run;
- a predeclared non-inferiority margin no larger than two percentage points;
- a 95% lower bound above the negative margin; and
- at least a 25% improvement in one predeclared efficiency metric, with a paired
  95% interval excluding no improvement.

If these thresholds are not met, publish the estimate without a superiority or
non-inferiority claim. Results on one model route do not imply results on all
open models, all providers, or the complete product.

## Efficiency and reliability metrics

Alongside official task outcomes, report per task-attempt:

- uncached input, cached input/write, reasoning, and output tokens where exposed;
- actual API charge when available, otherwise a timestamped price estimate with
  its formula;
- agent-active time and end-to-end wall time as separate measures;
- model requests, tool invocations, retries, and peak context size;
- process crashes, checkpoint resumes, ambiguous effects, and recovery outcome;
- human interventions and approval requests;
- final evidence-bundle completeness and redaction count.

Aggregate metrics use complete, named denominators. Cost, token, or time savings
are not claimed from successful tasks alone unless explicitly labeled
`conditional-on-success`.

## Required publication artifact

Every public release result includes:

```text
results/<experiment-id>/
  preregistration.md
  run-manifest.json
  environment-lock.json
  task-manifest.json
  prompts-and-tools/
  attempts/<run-id>/
    trajectory.redacted.jsonl
    verifier.json
    usage.json
    final.patch-or-artifact-metadata
  aggregate.json
  report.md
  checksums.txt
```

Protected datasets, hidden tests, credentials, and private user data are not
published. Their pinned upstream identifiers and checksums are recorded where
licensing permits. Redactions include a reason code and preserve event ordering.

The report must show:

- exact agent/model/benchmark variants and dates;
- complete task and attempt counts;
- all exclusions and incidents;
- primary and secondary outcomes with intervals;
- paired baseline results;
- efficiency/reliability metrics;
- known contamination and judge limitations;
- commands or documented procedure to reproduce the run;
- links to the immutable source tag and artifact checksums.

## Benchmark-specific version rules

- **Terminal-Bench 2.1:** pin the official dataset repository revision, task
  manifest, Harbor/framework version, container digests, and evaluator. Preserve
  all 89 task outcomes unless the official selected track defines a different
  set; name any selected set exactly. Do not change official timeout or resource
  settings for a leaderboard run; only a team-verified, published row is called
  an official result.
- **WebArena-Verified Hard:** pin the benchmark release or commit, Hard task
  manifest, site-state and container-image digests, evaluator version, and
  captured network traces. A selected development sample is not a Hard result.
- **Online-Mind2Web:** pin the repository commit, exact task manifest and
  checksums, execution dates, browser version, region, official starting site
  for each task, and v2 submission schema. Preserve every submitted trajectory,
  screenshot, action, and review status; distinguish automatic, human, and
  self-reported scores.
- **OSWorld 2.0:** use a single official release manifest across code, gated task
  classes, assets, mocked websites, and provider image. Record provider, screen
  resolution, action space, observation mode, and all human intervention.

## Protocol evolution

This file is versioned. Material changes to budgets, task sets, scoring, reruns,
or claim thresholds increment the protocol version. Results retain the protocol
under which they were produced and are not silently recomputed into a continuous
leaderboard.
