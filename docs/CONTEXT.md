# Bounded context projection

`sasori_context` is an optional, standard-library-only model-adapter layer. It
limits the history sent to a model without changing the durable Sasori
transcript, checkpoint, event stream, approval ledger, or recovery behavior.

## Structural projection is the default

```python
from sasori import Harness, OpenAIResponsesModel
from sasori_context import BoundedContextModel, ContextBudget, ContextProjector

provider = OpenAIResponsesModel("YOUR_CONFIGURED_MODEL")
projector = ContextProjector(
    ContextBudget(
        max_units=120_000,
        reserve_units=20_000,
        hot_turns=2,
    )
)
model = BoundedContextModel(provider, projector)
harness = Harness(model, tools)
```

The default unit is the byte length of a canonical UTF-8 JSON representation of
each `Message`, including provider continuation state and tool arguments. This
is deterministic, but it is **not a token estimate**. Reserve enough space for
tool schemas, provider framing, and output. If exact model-token budgeting is
required, inject a named estimator:

```python
def model_tokens(message):
    return tokenizer.count(message)  # must return a non-negative int

projector = ContextProjector(
    ContextBudget(max_units=64_000, reserve_units=8_000, hot_turns=3),
    estimator=model_tokens,
    estimator_name="provider_x_model_y_tokens_v1",
)
```

Structural projection guarantees:

- leading system messages are never silently discarded;
- a host may place a contiguous `ProtectedContextMessage` prelude immediately
  after those system messages. It remains ordinary assistant data and is fully
  charged to the same budget, but cannot be silently treated as old history;
- protected data in the middle/tail, with a non-assistant role, or carrying
  tool-call/result/error/provider metadata fails closed. An ordinary assistant
  message never gains this protection automatically;
- at least `hot_turns` recent user turns are kept;
- assistant tool calls and their complete immediate result set are atomic;
- orphan, duplicate, mismatched, or incomplete tool results fail closed;
- a malformed or incomplete call already refused by the Harness becomes
  provider-safe ordinary text with its exact `error_code`, never a repaired call
  or executable provider tool protocol;
- the same messages, budget, and estimator produce the same projection;
- a **structural omission marker** reports removed message/tool counts and a
  SHA-256 digest of the public projection. Opaque `provider_state` affects the
  local audit digest and byte budget but not the digest sent to another model.

The marker preserves no omitted facts and tells the model not to infer them.

This narrow marker is used by the optional Memory adapter. It does not elevate
recalled text to system authority. If protected data plus the current hot turn
cannot fit, projection raises `ContextBudgetExceeded`; callers may first remove
whole lower-ranked records, but the projector never clips a record or the
current request.

## Semantic compaction is explicit opt-in

**Shorter context. Durable source record.** A named semantic summarizer can
derive a lossy note from only the cold, structurally complete turns selected by
the same projector:

```python
from sasori import Harness, OpenAIResponsesModel
from sasori_context import (
    ContextBudget,
    ContextProjector,
    SemanticCompactionModel,
    SemanticCompactionPolicy,
)

primary_timeout = 60.0
summary_stage_timeout = 30.0
summary_transport_timeout = summary_stage_timeout + max(
    1.0, min(5.0, summary_stage_timeout / 10)
)
local_margin = 5.0

primary = OpenAIResponsesModel("YOUR_PRIMARY_MODEL", timeout=primary_timeout)
summarizer = OpenAIResponsesModel(
    "YOUR_SUMMARY_MODEL", timeout=summary_transport_timeout
)
projector = ContextProjector(
    ContextBudget(max_units=120_000, reserve_units=20_000, hot_turns=2)
)
model = SemanticCompactionModel(
    primary,
    projector,
    summarizer,
    # This caller-owned identity must change with model, endpoint, or prompt semantics.
    summarizer_name="openai:YOUR_SUMMARY_MODEL@endpoint-A",
    policy=SemanticCompactionPolicy(
        max_source_bytes=2 * 1024 * 1024,
        max_summary_bytes=16 * 1024,
        timeout_seconds=summary_stage_timeout,
        cache_entries=128,
    ),
)
harness = Harness(
    model,
    tools,
    model_timeout=primary_timeout + summary_stage_timeout + local_margin,
)
```

Direct composition owns all three deadlines. Keep the semantic-stage deadline
below the summarizer transport guard, and give the Harness enough time for the
primary transport budget plus the semantic-stage budget and a local scheduling
margin. A `succeeded` semantic diagnostic means compaction finished; it does not
mean that the later primary call or Harness run completed. First-party Research
and Developer factories calculate this combined Harness deadline automatically.

Enabling semantic compaction may make an additional model request and sends the
selected historical user, assistant, and tool content to the configured
summarizer. Sasori never enables it implicitly.

The summarizer receives a fixed system instruction, canonical JSON containing
untrusted model-visible history, no runtime tools, and no vendor-private
`provider_state`. It must return one strict JSON object that echoes the complete
public-source SHA-256. Any tool call, duplicate/unknown field, invalid
JSON/Unicode, source mismatch, empty text, or oversized result fails closed.
Sasori does not retry automatically or continue to the primary model after
compaction failure.

The primary model receives:

1. the original leading system messages;
2. any validated protected ordinary-data prelude, still in assistant role;
3. a host-authored system guard with source and summary digests;
4. the model-generated summary as a lossy, unverified ordinary assistant note;
5. the protected recent turns, with every retained tool atom complete.

The digest echo proves whole-request association, not factual entailment. Source
content can still influence the summarizer, and its free-text note can influence
the primary model or its proposed tool calls. Sasori does not promote the note
to system policy, approval, tool result, event, checkpoint, or effect evidence;
ordinary Harness approval and effect rules still apply. The complete primary
input is measured again; if guard, note, and protected tail do not fit, the
primary provider is not called.

## Diagnostics, size proxies, and cache lineage

`SemanticCompactionModel.diagnostics()` returns a bounded process-local tuple of
stable records. Each record identifies the named summarizer, prompt version,
actual prompt-policy digest, non-secret configuration digest, estimator, public
source digest, local full-state source digest, summary digest, source
message/tool counts, canonical source/prompt/summary byte counts, summarizer
call count, cache state, outcome, and stable error code. It contains no
transcript content, provider prose, API key, endpoint, or private continuation
state.

These byte counts are observable input-size proxies. They are not provider
tokens, money, or proof of a provider cache hit. The common `ModelReply`
currently has no provider-reported usage contract.

The standard cache is bounded and process-local. Its key binds the complete
public cold-source digest, public and local structural-projection digests,
projected units, caller-owned summarizer identity, prompt version and actual
policy digest, estimator, context budget, and source/summary limits. An exact
hit reuses the validated summary/marker segment, not a claim that the complete
primary request or provider cache is identical. Python callers must change
`summarizer_name` whenever model, endpoint, operator trust domain, or other
summary semantics change; first-party apps derive a non-secret identity digest
from provider, model, and effective endpoint.

Concurrent first misses may each call the summarizer. The first validated
candidate that also fits the final primary projection wins for overlapping
same-key callers; an in-flight reservation keeps that winner available even if
another key evicts its LRU entry. A projection/budget failure is never inserted.
The cache is not durable Memory, distributed state, or an exactly-once execution
mechanism.

## Failure, cancellation, and durability

- Semantic failure is explicit and fail-closed; there is no silent tail-only or
  structural-marker fallback.
- Cancellation while waiting for the summarizer is recorded for the semantic
  stage and re-raised unchanged, even if the child suppresses cancellation. The
  late child value is discarded. This remains cooperative and does not prove
  that a remote provider stopped or stopped billing. Under-budget direct calls
  produce no compaction record; a `succeeded` compaction record does not claim
  that the later primary call or Harness run completed.
- The full transcript is durable. The summary, cache, and diagnostics are
  derived process-local context in this slice and do not appear in the public
  event stream or Workbench timeline.
- Retained valid tool turns may still contain their original provider
  continuation format. This layer does not promise cross-provider switching
  while such a turn remains.

## First-party application configuration

Research and Developer applications use the same model composition for Python,
CLI, HTTP, and Workbench routes. Set `SASORI_CONTEXT_MAX_UNITS` to enable the
structural projector. Add `SASORI_COMPACTION_MODEL` to explicitly enable the
semantic adapter. The summarizer provider defaults to `SASORI_PROVIDER`, but a
provider name alone is not a complete trust-domain description: changing a base
URL can change the data recipient too. First-party identity and cache binding
therefore includes a digest of the effective endpoint. Full settings and the
composed Harness deadline are documented in [Providers](PROVIDERS.md).

## Non-goals and quality claims

Semantic compaction is not long-term Memory, lossless compression, an unlimited
context window, a fact database, a prompt-injection sandbox, or a guarantee of
lower cost or latency. Deterministic fakes prove selection, lineage, schema,
budget, typed failure, and summarizer-stage cancellation contracts. Real-provider factual quality
needs a separately versioned evaluation corpus for fact recall, unsupported
facts, contradictions, denied-effect correctness, and citation retention.

See [ADR-0009](ADR-0009-CONTEXT-PROJECTION-BOUNDARY.md) for structural
projection and
[ADR-0011](ADR-0011-SEMANTIC-COMPACTION-BOUNDARY.md) for the semantic trust,
cache, failure, and claim boundary.
