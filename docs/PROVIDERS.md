# Provider adapters

Status: local JSON and upstream-SSE wire conformance implemented; real-provider smoke requires separately configured credentials and model names.

Run the secret-safe two-turn tool smoke separately for each configured provider:

```powershell
python scripts/provider_smoke.py --provider openai --model YOUR_OPENAI_MODEL
python scripts/provider_smoke.py --provider anthropic --model YOUR_ANTHROPIC_MODEL
```

The script reads only the provider's normal API-key environment variable,
requires exactly one read-only echo call plus the second model turn, and prints
only provider/status/tool-count metadata. It does not print or persist the key,
model name, request, response, or provider state.

## Public models

```python
from sasori import AnthropicMessagesModel, OpenAIResponsesModel

openai_model = OpenAIResponsesModel("configured-model")
anthropic_model = AnthropicMessagesModel("configured-model", max_tokens=4096)

# Optional upstream SSE transport; Model.complete() still returns one complete reply.
streaming_openai = OpenAIResponsesModel("configured-model", stream=True)
streaming_anthropic = AnthropicMessagesModel("configured-model", stream=True)
```

The OpenAI adapter reads `OPENAI_API_KEY`; the Anthropic adapter reads `ANTHROPIC_API_KEY`. A constructor argument may supply a key, but keys are never placed in request JSON, provider state, events, or exception text/cause chains. Custom base URLs default to HTTPS. Plain HTTP is accepted only for an explicitly enabled loopback mock endpoint. Redirects are rejected rather than followed with an authorization header.

`timeout` is a monotonic total transport deadline as well as a socket guard. Cancellation propagates immediately to the caller and asks the blocking reader to stop/close; it does not prove that a remote provider stopped generation or billing.

## Upstream SSE aggregation

`stream=True` is opt-in and changes only the provider's upstream HTTP transport.
It does not add `complete_stream()`, token callbacks, partial `ModelReply` values,
public token events, or partial checkpoints. The existing Harness and event
projection remain unchanged.

The shared bounded SSE reader accepts UTF-8 `text/event-stream`, CR/LF/CRLF,
one leading BOM, comments, arbitrary network chunk boundaries, and standard
multi-line `data:` joining. It rejects unknown or duplicate event fields,
malformed/duplicate-key/non-finite JSON, invalid UTF-8, misplaced BOMs,
conflicting HTTP framing, oversized input, and EOF without a vendor terminal
event. The same monotonic total deadline, request-ID extraction, redirect
rejection, HTTP error taxonomy, socket interruption, and response byte limit
serve JSON and SSE.

OpenAI treats the complete `response` object in `response.completed`,
`response.incomplete`, or `response.failed` as authoritative. Progress deltas
are envelope-checked but never used to construct the final reply or provider
state. Anthropic strictly rebuilds ordered text, thinking/signature,
redacted-thinking, and tool-use blocks through `message_start` → content blocks
→ `message_delta` → `message_stop`. Tool input JSON is parsed only at block
completion. Either adapter fails closed before any tool executes if the stream
is truncated or structurally invalid.

## Tool schema contract

Provider tools use a shared compiler based on `inspect.signature()` and resolved annotations. It supports:

- `str`, `int`, `float`, `bool`;
- a two-member nullable union such as `str | None`;
- homogeneous `Literal[...]` values;
- `list[T]`;
- `dict[str, T]` for Anthropic-compatible dynamic objects.

Handlers must use explicit keyword-callable parameters. Positional-only parameters, `*args`, `**kwargs`, missing/unresolved/unsupported annotations, non-finite defaults, and model-visible `idempotency_key` are rejected before network I/O. OpenAI strict mode rejects dynamic dictionaries that cannot be represented as closed objects. Every advertised property is required; nullable means the model must send either the value or JSON `null`, not omit it to activate a Python default.

Returned call arguments are decoded with duplicate-key and non-finite-number rejection, then validated locally against the advertised schema. Unknown tools, missing/additional fields, wrong exact primitive types (`bool` is not an integer), invalid enums, duplicate call IDs, or more than one call despite parallel-call disabling fail the model boundary before any handler executes.

## Continuation state

The Harness copies `ModelReply.provider_state` to its durable assistant `Message`. SQLite checkpoints persist it verbatim. The field is opaque to the core and is never included in public events or the normal UI projection.

OpenAI state contains a versioned envelope with the exact prior `response.output` list. On the next tool turn the adapter sends the untouched reasoning/message/function-call items before `function_call_output`.

Anthropic state contains a versioned envelope with the exact prior `content` blocks. Thinking, redacted-thinking, text, and tool-use order is replayed unchanged before one immediately following user message containing ordered `tool_result` blocks.

A malformed/unknown state version fails before HTTP. Switching providers is allowed only at a clean conversation boundary. An unresolved tool turn with another provider's state is rejected rather than reconstructed lossily.

## Error taxonomy

All provider errors derive from `ProviderError` and expose stable fields: `provider`, `status`, `code`, `request_id`, `retry_after`, and `retryable`.

| Error | Meaning |
|---|---|
| `ProviderConfigurationError` | invalid local key/model/schema/URL/escape hatch |
| `ProviderConnectionError` | DNS/TLS/connect/premature transport failure |
| `ProviderTimeoutError` | total transport deadline expired |
| `ProviderAuthError` / `ProviderPermissionError` | HTTP 401 / 403 |
| `ProviderRateLimitError` | HTTP 429, with parsed `Retry-After` when valid |
| `ProviderHTTPError` | other HTTP failure |
| `ProviderProtocolError` | successful HTTP response with invalid wire data |
| `ProviderIncompleteError` | truncated/max-token/pause/cancelled provider result |
| `ProviderRefusalError` | explicit provider refusal |
| `ProviderResponseError` | provider-declared failed response |

There is no hidden sleep or automatic retry. This prevents invisible duplicate cost and preserves cancellation. The caller may use `retryable` and `retry_after` to make an explicit policy.

## Sources and verified boundary

- OpenAI: [Function calling](https://developers.openai.com/api/docs/guides/function-calling), [Streaming responses](https://developers.openai.com/api/docs/guides/streaming-responses?api-mode=responses), [Reasoning continuation](https://developers.openai.com/api/docs/guides/reasoning#keeping-reasoning-items-in-context), and [Create a Response](https://developers.openai.com/api/reference/resources/responses/methods/create).
- Anthropic: [Messages API](https://platform.claude.com/docs/en/api/messages), [Tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/how-tool-use-works), and [Handle tool calls](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls).

The repository test server validates JSON and SSE final text, complete two-turn tool use, success/error tool results, reasoning/thinking replay, malformed/incomplete/refused output, strict schema, duplicate calls, event ordering and framing, interrupted streams, HTTP metadata, numeric/date retry-after, timeout/cancel, slow-drip deadline, oversized bodies, redirects, unsafe URLs/escape hatches, and secret-free exception chains. Those fixtures prove protocol handling, not the availability or behavior of every vendor model or proxy.
