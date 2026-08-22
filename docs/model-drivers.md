# GLM and DeepSeek model driver

The model gateway currently contains one strict OpenAI-compatible Chat
Completions adapter with two wire dialects: `glm` and `deepseek`. It is designed
for controlled, recoverable runs rather than maximum compatibility with every
parameter a provider might accept.

## Routes

| Dialect          | Base URL                                      | Notes                                                                       |
| ---------------- | --------------------------------------------- | --------------------------------------------------------------------------- |
| GLM standard API | `https://open.bigmodel.cn/api/paas/v4`        | Uses the standard API key and quota.                                        |
| GLM Coding Plan  | `https://open.bigmodel.cn/api/coding/paas/v4` | A distinct route and key scope; record it as a different benchmark variant. |
| DeepSeek         | `https://api.deepseek.com`                    | Record the returned model and system fingerprint because aliases may move.  |

The configured base URL must use HTTPS, except for an explicit loopback test
server. Credentials, query strings, fragments, and a pre-appended
`/chat/completions` path are rejected.

## Supported contract

- text and inline base64 images according to the explicit capability profile;
- OpenAI function-tool definitions and tool results;
- `auto` tool choice, or `none` by omitting current tool definitions;
- thinking enable/disable, reasoning effort, and replay of assistant
  `reasoning_content`;
- normalized usage, cancellation, and sanitized provider errors;
- actual response ID, request ID, returned model, and system fingerprint.

All provider calls are deliberately non-streaming in this first version. The
driver still returns normalized events through the shared `AsyncIterable`
interface. This avoids provider-specific streamed-tool behavior, guarantees a
complete usage record, and keeps the pinned SDK's SSE diagnostics outside the
credential boundary. Provider streaming requires a bounded parser and its own
live conformance evidence.

The portable profile rejects required/named tool choice, parallel tool calls,
response schemas, and strict schemas. A future provider-specific beta profile
may add one of these only after it has its own explicit capability and live
conformance evidence.

## Reliability boundary

The adapter performs no retries (`maxRetries: 0`). A timed-out or disconnected
request may already have consumed tokens, so only the journaled kernel can make
an auditable retry decision. HTTP bodies and provider messages are not copied
into normalized errors, and local `ModelRequest.metadata` is never sent over the
wire.

If a tool failed, callers must encode a structured failure envelope in its text
content. Chat Completions has no portable `isError` field, so silently dropping
that bit is forbidden.

## Live-conformance gate

Offline tests currently prove request and complete-response decoding, reasoning
replay, tool normalization, usage accounting, identity capture, preflight and
in-flight cancellation, error sanitization, and zero internal retries. They do
not prove a live account or model route.

Before a route is used in a benchmark, run and record all four cases against one
exact configured model:

1. text plus usage;
2. thinking plus returned identity;
3. one tool call followed by one tool result, with reasoning replayed;
4. cancellation or interrupted transport without an automatic replay.

The conformance record must include date, requested model, returned model,
system fingerprint when supplied, route label, SDK version, and redacted fixture
hash. It must not contain an API key or raw private reasoning.

Provider references: [GLM OpenAI compatibility](https://docs.bigmodel.cn/cn/guide/develop/openai/introduction),
[GLM Chat Completions](https://docs.bigmodel.cn/api-reference/%E6%A8%A1%E5%9E%8B-api/%E5%AF%B9%E8%AF%9D%E8%A1%A5%E5%85%A8),
[GLM thinking mode](https://docs.bigmodel.cn/cn/guide/capabilities/thinking-mode),
[DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion),
and [DeepSeek thinking mode](https://api-docs.deepseek.com/guides/thinking_mode).
