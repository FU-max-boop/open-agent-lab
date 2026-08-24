# Current Roadmap

Current gate: **Gate 2 — native Codex/open-model benchmark pilot**.
`CHARTER.md` remains the authority for later phases.

## Completed foundation

- The authoritative journal, disposable checkpoints, fenced writer lease,
  recovery table, duplicate suppression, `needs_review`, verifier, and evidence
  path are implemented and tested.
- One OpenAI-compatible adapter implements the conservative GLM/DeepSeek common
  subset without internal retries or silent model fallback.
- Offline provider fixtures cover reasoning replay, tool calls, complete
  responses, usage, response identity, preflight and in-flight cancellation,
  and sanitized failures.
- The architecture now pins open-source Codex as the inner agent loop instead of
  duplicating it in this repository.
- `codex-run` builds isolated native Responses invocations for DeepSeek and Z.AI,
  keeps credentials in environment variables, validates exact model IDs, and
  provides a secret-free dry-run.
- A native Responses byte relay now fixes one provider/model per process,
  strips caller credentials, disables redirects and retries, enforces bounded
  access, and fsyncs a hash-chained metadata journal.
- The Harbor adapter now resolves no provider credential. It sends only a
  per-trial relay token to Codex, seals the listener after the agent phase, and
  validates a host-only copy while retaining the journal and seal as separate
  Harbor artifacts.
- Linux CI completes Harbor's official `hello-world` task through the real
  Harbor 0.22, Codex 0.149.0, isolated relay, tool, verifier, ATIF, result, and
  lock path. Synthetic evidence remains publication-ineligible by design.
- A provider-free route harness composes the pinned Codex runner, relay,
  strict provider metadata checks, and EvidenceBundle writer. CI executes both
  frozen DeepSeek and GLM profiles with one exact command, zero retries, shell
  networking disabled, hard process bounds, and synthetic-only receipts.

## Current slice — attributable open-model strategy

- Keep Codex 0.149.0 and Harbor 0.22.0 fixed for the first paired experiment.
- Verify that the opt-in `verify-instruction-v1` bytes travel through the real
  Codex developer-message path; it is an Open Agent Lab variant, not a Codex
  feature.
- Before the first provider request, freeze a paired-result analyzer that rejects
  missing pairs and applies the declared reward, cost, and replication gates.
- Run an isolated, non-scoring Harbor route/model-identity probe for exact GLM
  and DeepSeek routes; retain redacted raw event evidence without calling it
  provider conformance.
- Run the relay against live GLM and DeepSeek Responses routes with disposable,
  provider-budget-capped credentials.
- Run the frozen five-task control/treatment pair once per provider as a
  directional screen. Promotion also requires a mirrored within-provider
  replication and acceptable token and wall-time cost.
- Design clean-boundary native resume separately using relay capability epochs;
  do not wrap or duplicate Codex's internal tool loop.

## Deferred slice — Terminal-Bench 3.0 migration

- Preserve the current slice's frozen Terminal-Bench 2.1 five-task experiment as
  the attributable infrastructure pilot; do not relabel it as 3.0.
- Until that pilot has intact trajectories and a positive directional signal,
  keep Terminal-Bench 3.0 work to documentation calibration and read-only
  compatibility auditing. Do not implement a full 3.0 adapter or manifest, run
  all-task oracle or install-only campaigns, or start the 370-trial evaluation.
- After that gate, open a separate implementation slot that pins the exact
  `v3.0.0` tag commit, resolved Harbor Hub dataset digest, all 74 task versions,
  and resource requirements, including the four H100 tasks.
- The eventual complete candidate is predeclared as five attempts per task for
  one agent/model candidate (74 × 5 = 370 planned trials), matching an inspected
  published row. Publish the complete Hub job before seeking official leaderboard
  listing through the maintainer admission path confirmed at that time; upload
  alone is not proof of leaderboard admission.

## Exit gate

- The exact Codex binary/version, provider route, requested model, returned model,
  reasoning setting, retries, and context limit are retained for every attempt.
- Provider credentials never enter argv, configuration, trajectory, or evidence.
- A task-container adversarial probe cannot find the durable provider key or
  access the relay process namespace; the disposable relay token remains
  request-, model-, time-, network-, and spend-bounded.
- Fake-server probes cover the Responses events Codex actually consumes.
- Live text, tool, patch, truncation, and error probes pass or produce a narrow,
  documented compatibility gap.
- Harbor's hello-world task completes with a valid ATIF trajectory for both the
  control and exact-instruction treatment paths in Linux CI.
- No successful run exists without an official verifier record.
- The frozen analyzer rejects incomplete pairs and reports every attempt in the
  denominator before any experiment decision is made.
- Exact requested and returned model identities are preserved for every live
  model call.
- `pnpm check` and `pnpm build` pass from a clean checkout.

Implementation rule: upstream Codex owns the inner loop. Add a provider-specific
shim only after a retained probe demonstrates the exact missing behavior.
