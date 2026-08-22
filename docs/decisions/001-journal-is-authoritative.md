# ADR-001: The journal is authoritative

Status: accepted for Gate 1.

## Decision

- The committed, hash-chained journal is the sole authority for run state.
- State, checkpoints, indexes, and evidence are deterministic derivatives.
- The local store uses SQLite WAL with an expiring single-writer lease.
- Event and checkpoint updates commit in one transaction after reducer
  preflight and a compare-and-swap of the journal head.
- An invalid checkpoint is ignored and rebuilt from the journal; an invalid
  journal fails closed.
- Gate 1 fully replays the journal. The checkpoint is only a disposable derived
  snapshot: a current snapshot is left untouched, while invalid data or schema
  is replaced on open.

## Consequences

Live execution and replay share one reducer. History is never edited; a
correction is a later event. Server storage may replace SQLite only behind the
same persisted event contract.
