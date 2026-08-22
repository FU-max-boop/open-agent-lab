# Codex profiles for GLM and DeepSeek

The primary agent path is a pinned upstream Codex binary connected directly to
provider-native Responses endpoints. The repository's Chat Completions driver
is retained for diagnostics and future providers that genuinely lack Responses;
it is not the Terminal-Bench agent loop.

## Native routes

| Profile | Responses base URL | Credential | Scope |
| --- | --- | --- | --- |
| DeepSeek | `https://api.deepseek.com/` | `DEEPSEEK_API_KEY` | Standard pay-as-you-go API |
| Z.AI | `https://api.z.ai/api/v1` | `ZAI_API_KEY` | GLM Coding Plan only |

DeepSeek documents Codex, function tools, web search, and the custom
`apply_patch` tool on its Responses API. Z.AI documents Codex and the Coding
Plan Responses endpoint but does not yet publish a complete Responses feature
matrix. The ordinary Z.AI pay-as-you-go endpoint is documented for Chat
Completions, not Responses, and is not silently substituted.

`codex-run` uses Codex's supported custom-provider configuration, disables
WebSocket transport, and sends the prompt on stdin. It ignores personal Codex
configuration and exec-policy rules, runs ephemerally, and emits Codex JSONL.
Credentials stay in their environment variable: neither argv nor a generated
file contains the value. The child process receives a minimal environment and
uses one temporary empty home/`CODEX_HOME`; Codex's shell policy removes
key/secret/token variables and blanks the selected provider key before tool
execution.

Examples:

```bash
export DEEPSEEK_API_KEY="..."
node apps/cli/dist/index.js codex-run \
  --provider deepseek \
  --model deepseek-v4-pro \
  --reasoning high \
  --workspace /path/to/repository \
  --prompt-file /path/to/instruction.md
```

```bash
export ZAI_API_KEY="..."
node apps/cli/dist/index.js codex-run \
  --provider zai \
  --model glm-5.3 \
  --reasoning max \
  --workspace /path/to/repository \
  --prompt-file /path/to/instruction.md
```

Use `--dry-run` first to inspect the exact route, model, reasoning setting,
context limit, and required environment variable without making a request.

## Conformance gate

An HTTP success is not proof that a parameter works: providers may ignore
unsupported Responses fields. Before a route enters a benchmark, retain
redacted evidence for:

1. text streaming and exactly one terminal event;
2. function/shell call, tool result, and final answer with stable `call_id`;
3. freeform `apply_patch` input and result;
4. multiple tool calls and their ordering;
5. each advertised reasoning effort across a tool round;
6. output truncation and `incomplete`/`failed` handling;
7. usage, cached-input, and reasoning-token fields when supplied;
8. 400, 401, 429, 5xx, disconnect, and retry behavior;
9. one real repository task through the actual Codex binary.

Every record includes the Codex version/commit, requested and returned model,
provider route, UTC time, retry policy, context setting, response/request IDs,
system fingerprint when supplied, and a hash of the redacted raw event stream.
Missing usage stays missing rather than becoming zero. Private reasoning and API
keys are never published.

The Harbor path adds an isolated native-Responses byte relay rather than a
protocol adapter. Each relay generates one short-lived, fixed-model capability,
injects the durable provider key, and stores response identity, request ID,
status, usage, timing, and byte hashes in a redacted hash chain. Harbor seals
the listener and validates every three-event lifecycle from a host-only copy
before attaching this additional metadata; the chain and seal are also retained
as separate artifacts. Its official ATIF conversion remains unchanged.

This implementation is not yet a result claim. A retained Linux container
probe must show that the task cannot inspect the relay PID or durable key, and
live conformance must show that both providers return consistent model identity
and request metadata. Missing values remain missing and block publication.

## Diagnostic Chat driver

`@open-agent-lab/model-driver` still implements a conservative, non-streaming
Chat Completions subset for GLM and DeepSeek. It normalizes reasoning replay,
tool calls, usage, identity, cancellation, and sanitized errors with SDK retries
disabled. This path is useful for differential probes or a future narrow shim.
It must not be described as Codex compatibility or substituted into a benchmark
variant without a new preregistration.

Provider references: [DeepSeek Codex integration](https://api-docs.deepseek.com/quick_start/agent_integrations/codex/),
[DeepSeek Responses API](https://api-docs.deepseek.com/guides/responses_api/),
[Z.AI tool integration](https://docs.z.ai/devpack/tool/others), and
[Z.AI GLM-5.3](https://docs.z.ai/guides/llm/glm-5.3).
