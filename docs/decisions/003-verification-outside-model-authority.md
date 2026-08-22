# ADR-003: Verification is outside model authority

Status: accepted for Gate 1.

## Decision

- The kernel calls a trusted verifier port; model output cannot directly set a
  terminal status.
- A successful record binds the run ID, task digest, final workspace digest,
  verifier identity, and verifier version.
- A verifier crash writes no partial `verifying` state and is safe to retry.
- Evidence is a deterministic, versioned, redacted projection of the journal,
  artifacts, and verifier record. It is not a second state authority.

## Consequences

No run may become `succeeded` without a passing bound verifier record. Evidence
generation or validation failure blocks publication and any success claim.
