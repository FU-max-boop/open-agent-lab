# Deterministic smoke task

This fixture is deliberately small and provider-free. It exists to exercise the
complete `run -> evidence -> verify` path before any model or benchmark adapter
is trusted.

The scripted driver must:

1. materialize `input.txt` from `task.json`;
2. observe the input through the same event protocol used by real runs;
3. write the requested `output.txt`;
4. verify the exact expected bytes;
5. emit a versioned run specification and append-only event log; and
6. create a content-addressed evidence bundle that the standalone verifier
   accepts.

The test is not a capability benchmark. Its score is binary and its purpose is
to catch protocol, persistence, replay, and attestation regressions without
network access or nondeterministic model behavior.
