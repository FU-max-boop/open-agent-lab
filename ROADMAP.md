# Current Roadmap

Current gate: **Gate 1 — minimal recoverable kernel**. `CHARTER.md` remains the
authority for later phases.

## Week 1 — recovery core

- Land ADR-001 through ADR-003.
- Complete journal replay, disposable checkpoint rebuilding, and a fenced
  single-writer lease.
- Complete the four-case tool recovery table and state-aware duplicate
  suppression.
- Route the provider-free smoke task through kernel → broker → verifier →
  evidence.

## Week 2 — prove the boundary

- Add minimal typed file, patch, process, and test tools.
- Prove each mutating tool's effect-boundary fingerprint check under its real
  lock or compare-and-swap primitive.
- Inject interruption at intent, effect, result, and checkpoint boundaries.
- Retain 20 clean provider-free smoke attempts and independently validate their
  evidence.
- Run checks and builds from a clean checkout.

## Exit gate

- Concurrent resume permits at most one writer.
- A stale or corrupt checkpoint never overrides journal history.
- No uncertain external effect is replayed automatically.
- Equivalent actions are suppressed only while relevant state is unchanged.
- Every mutating tool checks that state atomically at its effect boundary.
- Every `needs_review` exit is explicit and journaled.
- No successful run exists without a bound independent verifier record.
- All 20 runs produce valid evidence containing no credentials.
- `pnpm check` and `pnpm build` pass from a clean checkout.

Implementation rule: one reducer, one recovery table, and one authoritative
state. Do not introduce a generic abstraction before a second real
implementation requires it.
