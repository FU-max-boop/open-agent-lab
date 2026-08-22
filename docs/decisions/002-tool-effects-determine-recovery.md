# ADR-002: Tool effects determine recovery

Status: accepted for Gate 1.

## Decision

Every tool declares exactly one effect class. Intent is durable before the
effect boundary and the full invocation, including its runtime idempotency key,
is passed to the tool. The key is scoped to the run. Each definition supplies a
contract digest and must change it whenever its schema or recovery semantics
change; automatic schema-derived digests are future work.

| Effect | Interrupted recovery |
| --- | --- |
| `read_only` | Replay only when relevant state still matches. |
| `idempotent` | Replay with the runtime-derived stable key when state matches. |
| `workspace_mutation` | Reconcile as applied, not applied, or unknown. |
| `external_non_idempotent` | Never auto-replay; enter `needs_review`. |

If recovery had to take over an expired writer lease, a pending mutable effect
enters `needs_review` before reconciliation or replay. This prevents a paused
old process and its successor from performing the same mutation concurrently.

Human decisions are limited to `confirmed_applied`,
`confirmed_not_applied_then_retry`, and `abort`, and are journaled. Duplicate
suppression uses the tool, arguments, contract, effect, and relevant-state
fingerprints; invocation IDs alone are not identity.

Cancellation may discard an interrupted `read_only` intent. Any other pending
effect enters `needs_review`, because a terminal cancellation must not hide a
side effect that may already have happened.

The broker's state probe is an early drift check, not a lock: a generic broker
cannot make a separate fingerprint read and side effect atomic. Each mutating
tool must compare the persisted fingerprint at its effect boundary under the
workspace lock or remote compare-and-swap primitive. That rule is part of the
tool contract digest and must have a conformance test.

## Consequences

Exactly-once execution is not claimed for arbitrary external systems. Contract
or state drift fails closed instead of silently replaying an uncertain action.
