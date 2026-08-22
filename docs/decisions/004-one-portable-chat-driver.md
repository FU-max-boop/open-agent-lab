# ADR-004: One portable Chat Completions driver

Status: accepted for Gate 1; retained as a diagnostic fallback after ADR-005.

## Decision

- GLM and DeepSeek share one `OpenAICompatibleDriver`; `dialect` handles only
  documented wire differences.
- The transport is the official OpenAI JavaScript SDK pinned in the lockfile.
  SDK retries are disabled. The recoverable kernel owns any replay decision.
- Capabilities and the exact requested model are explicit configuration. Model
  names never imply capabilities, and there is no silent fallback.
- Provider calls are initially non-streaming. The driver still exposes the
  shared normalized `AsyncIterable` contract, so provider streaming can be added
  later without changing orchestration. It remains disabled until a bounded
  parser and live conformance evidence remove cutoff, usage, and raw-log
  ambiguity across both providers.
- The portable tool-choice subset is `auto` and `none`. Parallel tool calls and
  strict structured output are rejected rather than approximated.
- Assistant `reasoning_content` is a typed message part so thinking-plus-tool
  conversations can replay it. Public evidence may redact its text but must not
  silently remove the fact that reasoning occurred.
- Returned response ID, provider request ID, model, and system fingerprint are
  normalized into a narrow audit event. Arbitrary raw response metadata is not
  persisted.
- Provider errors are sanitized. Request metadata is local-only, API keys never
  enter a run spec, and redirects fail rather than forwarding credentials.

## Consequences

Provider support grows by proving a small shared contract, not by accumulating
subclasses. Provider-specific beta routes require separate capability profiles
and conformance evidence. Offline fixtures prove serialization and failure
handling; they are not evidence that a live model route works.
This first version trades incremental token latency for a smaller, auditable
transport boundary and complete response accounting.
