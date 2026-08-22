# Current Roadmap

Current gate: **Gate 1 — complete the minimal autonomous task loop**.
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

## Current slice — executable agent loop

- Run opt-in live conformance for one exact GLM route and one exact DeepSeek
  route; preserve only redacted fixtures and response identity.
- Add minimal typed file, patch, process, and test tools rather than a generic
  plugin framework.
- Connect model decisions to the broker through the existing recoverable kernel.
- Add headless `run-task` and `resume-task` commands with bounded turns, time,
  tokens, and output.
- Prove each mutating tool's effect-boundary fingerprint check under its real
  lock or compare-and-swap primitive.
- Inject interruption at intent, effect, result, and checkpoint boundaries.
- Retain 20 clean provider-free smoke attempts and independently validate their
  evidence.

## Next slice — Terminal-Bench 2.1 pilot

- Pin Harbor `v0.22.0` and the official Terminal-Bench 2.1 dataset digest.
- Predeclare a deterministic 10-task subset before inspecting outcomes.
- Run the same tasks and resource envelope on exact GLM and DeepSeek routes.
- Publish every attempt and denominator as a reproducible pilot, not an official
  leaderboard score while community submissions remain closed.

## Exit gate

- Concurrent resume permits at most one writer.
- A stale or corrupt checkpoint never overrides journal history.
- No uncertain external effect is replayed automatically.
- Equivalent actions are suppressed only while relevant state is unchanged.
- Every mutating tool checks that state atomically at its effect boundary.
- Every `needs_review` exit is explicit and journaled.
- No successful run exists without a bound independent verifier record.
- All 20 runs produce valid evidence containing no credentials.
- Exact requested and returned model identities are preserved for every live
  model call.
- `pnpm check` and `pnpm build` pass from a clean checkout.

Implementation rule: one reducer, one recovery table, and one authoritative
state. Do not introduce a generic abstraction before a second real
implementation requires it.
