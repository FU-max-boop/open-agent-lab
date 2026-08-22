# Target Architecture

This document describes the intended architecture. Only components represented
by executable source and tests in this repository should be treated as
implemented; the diagram is not an implementation-status claim.

## System context

```text
                       operator / evaluator
                                |
                       CLI / Harbor / CI
                                |
                experiment + recovery controller
                       /                 \
          open-source Codex              verifier
             /          \                   |
     native Responses   code workspace    evidence
       GLM/DeepSeek           |             bundle
                       durable journal
```

The task—not the chat turn—is the unit of work. Every task has an immutable run
identity, pinned inputs, a state machine, a resource envelope, and a durable
record of observations, decisions, tool effects, verification, and termination.

## Component boundaries

### Clients

The CLI is the first reference client because benchmarks require automation and
clean process boundaries. A later desktop client exposes the same kernel while
adding a shared browser, terminal, diff, evidence timeline, approval UI, and
human takeover. CI uses the same API in non-interactive mode.

Clients may render task state and submit decisions; they do not own task
semantics or persistence.

### Experiment and recovery controller

The outer controller owns:

- lifecycle states such as `created`, `running`, `waiting_for_approval`,
  `needs_review`, `verifying`, `succeeded`, `failed`, and `cancelled`;
- variant selection, resource accounting, and Codex process/session lifecycle;
- write-ahead event recording, checkpoints, cancellation, and resume;
- termination only after the configured verifier returns an outcome.

Codex owns the inner model/tool turn loop. The controller may resume or restart a
pinned Codex session, but it does not reinterpret tool calls or maintain a
second planning state machine. The existing recoverable kernel remains the
authority for outer-run state and uncertain effects.

### Codex engine and open-model profiles

The primary model path is an unmodified, pinned open-source Codex release using
its supported custom-provider configuration and the Responses wire protocol.
DeepSeek and Z.AI connect directly to their documented native Responses
endpoints. A protocol shim is permitted only for a concrete failing probe and
must stay narrower than the missing behavior.

Each model variant records at least:

- provider and route;
- exact model identifier and revision where available;
- weight/digest, precision, and quantization for self-hosted models;
- inference server and version;
- context and output limits;
- sampling/reasoning settings;
- supported tool-call, structured-output, image, and cache capabilities.

Capability profiles adapt mechanics, not benchmark answers. A router may choose
models in product mode, but benchmark harness-isolation runs use the predeclared
route and may not silently fall back to another model.

The legacy Chat Completions driver is retained for provider diagnostics and
future endpoints that genuinely lack Responses. It is not an alternative agent
loop and is not used in the first Codex Terminal-Bench track.

### Policy and tool broker

All actions pass through a typed broker that validates arguments, applies
workspace and network policy, obtains approvals, emits redacted events, and
returns structured results.

Planned tool families are:

- workspace: read, search, patch, file metadata;
- process: spawn, stream, timeout, signal, and collect exit status;
- source control: inspect and create bounded Git changes;
- verification: run declared tests and assertions in an isolated context;
- browser: navigate, observe, interact, inspect console/network, and capture
  evidence;
- operator: request approval, clarification, or takeover.

Shell access remains available because real engineering tasks need it, but it is
mediated by sandbox, timeout, output limits, and policy. High-level tools are
preferred when they offer stronger typing or idempotency.

### Code workspace

The code workspace provides an isolated task checkout and bounded process
environment. It records initial and final repository state, patches, generated
artifacts, commands, exit codes, and declared test results.

Benchmark verifiers execute outside the agent's readable trust boundary unless a
benchmark explicitly makes tests part of the task. The agent cannot turn a
verifier result into success by modifying the verifier.

### Browser runtime

The browser runtime holds one task-scoped browser context and exposes:

- URL, title, DOM/accessibility snapshots, and stable element references;
- targeted screenshots and visual observations when structured state is
  insufficient;
- console, page-error, download, and permitted network metadata;
- deterministic assertions over page state;
- an operator-visible session for takeover and return of control.

Element references are scoped to an observation generation and become invalid
after relevant navigation or DOM change. Coordinate clicks are a fallback, not
the default. Browser secrets and session storage are never copied into model
context or public evidence without explicit allowlisting.

### Verifier

Verification is independent from the model loop. A verifier receives the pinned
task definition and final workspace/environment, runs in its declared isolation
boundary, and emits a structured outcome plus evidence.

Preferred order:

1. official benchmark evaluator;
2. deterministic test or assertion;
3. rule-based artifact comparison;
4. pinned judge model/rubric only when the benchmark requires it;
5. explicit human acceptance for product tasks that cannot be automated.

The selected method and its limitations are always recorded. Model confidence is
telemetry, never a verifier.

### Journal, checkpoints, and evidence

The durable store is append-oriented. Before a tool is invoked, the runtime
records an invocation intent; after completion, it records the result and links
both through stable IDs. Checkpoints reference content-addressed state rather
than duplicating mutable workspaces.

The first local implementation targets SQLite in write-ahead-log mode plus a
content-addressed artifact directory. Persistence remains behind an interface so
server deployments can replace the store without changing event or evidence
contracts.

A portable evidence bundle is expected to contain:

- run and environment manifests;
- model and budget configuration;
- ordered, redacted runtime events;
- tool inputs/outputs or hashed external references;
- initial/final repository identifiers and patch;
- browser trace/screenshots permitted by policy;
- verifier inputs, outputs, and version;
- resource, token, timing, retry, and intervention telemetry;
- terminal status and any unresolved side-effect uncertainty.

Public bundles omit secrets and protected benchmark assets. Redaction is explicit
and machine-readable so absence is not mistaken for missing telemetry.

## Execution and recovery

```text
create -> pin Codex/provider/benchmark variant -> checkpoint -> run Codex
   -> journal trajectory and process outcome -> reconcile interruption
   -> verify official task outcome -> emit evidence -> terminal state
```

Recovery does not promise exactly-once effects across arbitrary external
systems. Tools declare an effect class:

- **read-only:** safe to repeat within the pinned environment;
- **idempotent:** repeatable with a stable idempotency key or verified postcondition;
- **workspace mutation:** recoverable from journal plus content-addressed state;
- **external non-idempotent:** never automatically repeated after an uncertain
  boundary; transition to `needs_review`.

On resume, Gate 1 reconstructs state from the authoritative journal and repairs
the disposable checkpoint when needed. It validates workspace fingerprints,
then either continues, reconciles a known postcondition, or asks for review.
Missing or corrupt journal history fails closed; a corrupt derived checkpoint
is rebuilt.

## Trust boundaries and threat model

Repository contents, webpages, terminal output, downloaded files, model output,
and benchmark task text are all untrusted inputs. They may contain prompt
injection or attempt to exfiltrate credentials.

Initial controls include:

- task-scoped filesystem and browser contexts;
- least-privilege network and credential injection;
- explicit approvals for consequential external actions;
- separation of agent-visible state from verifier-only state;
- output-size, time, token, process, and retry limits;
- secret detection and structured redaction before model context or publication;
- provenance labels that distinguish system policy, operator instruction, task
  content, webpage content, and tool output;
- audit events for policy denials, approvals, and human takeover.

Sandboxing reduces risk but is not a claim of perfect containment. Security
boundaries must be tested independently before unattended operation is promoted.

## Benchmark integration

Each benchmark adapter is a thin boundary that maps the official task lifecycle
to Codex and returns the official evaluator output. Terminal-Bench integration
inherits Harbor's Codex adapter so installation, session capture, ATIF
conversion, and container interaction stay upstream-owned. Our subclass may
select a native provider profile and add variant metadata; it may not add
task-specific reasoning, expose hidden state, or rewrite scores.

Benchmark adapters, product skills, and site-specific automations remain separate
packages. Results from a general harness and a skill-augmented harness are
separate tracks.

## Architectural invariants

- No terminal success state without a verifier record.
- No model or provider identity hidden from a published run.
- No long-lived Codex fork when a supported provider profile is sufficient.
- No protocol shim without a failing, retained compatibility probe.
- No silent model fallback in a controlled evaluation.
- No replay of an uncertain non-idempotent action.
- No browser element reference reused after its observation expires.
- No benchmark evaluator secrets in agent-readable context.
- No published evidence bundle containing raw credentials.
- No event mutation after finalization; corrections append a superseding record.
- No client-specific task state required to resume from another client.

## Expected repository shape

This is a target, not a commitment to create empty packages before they are
needed.

```text
apps/
  cli/
  desktop/
packages/
  contracts/
  kernel/
  model-drivers/
  tool-broker/
  browser-runtime/
  evidence/
benchmarks/
  terminal-bench/
  webarena-verified/
  online-mind2web/
  osworld/
  smoke/
```

Dependencies should be integrated behind narrow adapters rather than maintained
as heavy forks. Replacing a browser driver, model endpoint, or baseline harness
must not require rewriting the journal or verifier contracts.
