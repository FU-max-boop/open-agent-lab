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

## Current slice — Codex provider conformance

- Pin one exact Codex release/commit and record it in every run variant.
- Exercise Codex against a fake Responses server for text, function/shell calls,
  `apply_patch`, usage, terminal errors, and cut streams.
- Run opt-in live conformance for exact GLM and DeepSeek routes; retain redacted
  raw event fixtures and actual returned model identity.
- Keep provider credentials outside the same-UID Harbor/Codex process tree, then
  prove the boundary with a Linux `/proc` regression probe.
- Preserve provider-returned model and transport request metadata beside ATIF;
  do not substitute Harbor's requested-model fallback.
- Add a minimal Harbor subclass of its official Codex adapter; do not rewrite
  installation, container execution, session capture, or ATIF conversion.
- Freeze a five-task Terminal-Bench 2.1 pilot before observing outcomes.

## Next slice — Terminal-Bench 2.1 pilot

- Pin Harbor `v0.22.0` and the official Terminal-Bench 2.1 dataset digest.
- Run the predeclared five-task infrastructure pilot once per provider, then
  expand to roughly 15 tasks x 3 attempts only after trajectory integrity is
  proven.
- Run the same tasks and resource envelope on exact GLM and DeepSeek routes.
- Publish every attempt and denominator as a reproducible pilot, not an official
  leaderboard score while community submissions remain closed.

## Exit gate

- The exact Codex binary/version, provider route, requested model, returned model,
  reasoning setting, retries, and context limit are retained for every attempt.
- Provider credentials never enter argv, configuration, trajectory, or evidence.
- Fake-server probes cover the Responses events Codex actually consumes.
- Live text, tool, patch, truncation, and error probes pass or produce a narrow,
  documented compatibility gap.
- Harbor's hello-world task completes with a valid ATIF trajectory once a
  container runtime is available.
- No successful run exists without an official verifier record.
- Exact requested and returned model identities are preserved for every live
  model call.
- `pnpm check` and `pnpm build` pass from a clean checkout.

Implementation rule: upstream Codex owns the inner loop. Add a provider-specific
shim only after a retained probe demonstrates the exact missing behavior.
