# ADR-005: Open-source Codex is the inner agent engine

Status: accepted on 2026-08-22; makes ADR-004's Chat driver diagnostic rather
than the primary model path.

## Context

OpenAI released Codex as an open-source coding agent with a supported custom
model-provider interface. DeepSeek now documents a native Responses API and a
Codex setup. Z.AI lists Codex as a supported Coding Plan tool and publishes a
Responses endpoint. Reimplementing Codex's planning, shell, patch, and session
loop would add maintenance cost without creating the differentiator this
project needs.

## Decision

- A pinned upstream Codex release is the inner coding-agent implementation.
- Open Agent Lab owns native open-model profiles, compatibility probes, narrow
  provider fixes, controlled experiments, outer-run recovery, verification,
  and evidence.
- Provider credentials are supplied only through named environment variables.
  Generated config and process arguments never contain a key.
- DeepSeek and Z.AI use native Responses endpoints first. A translation gateway
  is added only when a retained probe identifies an unavoidable protocol gap.
- Terminal-Bench integration inherits Harbor's official Codex adapter so its
  session capture and ATIF conversion remain authoritative.
- Benchmark variants pin the Codex commit/version, provider route, exact model,
  reasoning settings, retry policy, context limit, and returned model identity.
- We do not maintain a broad Codex fork. Generally useful fixes go upstream;
  local patches must be small, measured, and removable.

## Consequences

The project reaches meaningful evaluation sooner and can compare GLM and
DeepSeek without conflating model quality with a new agent loop. The existing
kernel, broker, and evidence packages remain valuable at the outer task and
publication boundary. The Chat Completions driver remains a diagnostic fallback
for providers that lack Responses, but it no longer drives the primary roadmap.

Compatibility with upstream Codex becomes a versioned dependency. Provider
documentation may overstate compatibility or silently ignore parameters, so
successful HTTP requests are insufficient evidence: text, tools, patching,
usage, errors, interruption, and a real repository task must all be probed.
