# Bounded context projection

`sasori_context` is an optional, standard-library-only model adapter. It limits
the history sent to a model without changing the durable Sasori transcript.

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

## Guarantees

- Leading system messages are never silently discarded.
- At least `hot_turns` recent user turns are kept.
- Assistant tool calls and their complete immediate result set are atomic.
- Orphan, duplicate, mismatched, or incomplete tool results fail closed.
- A malformed or incomplete call already refused by the Harness is projected
  as provider-safe assistant/user text with its exact `error_code`; it remains
  inert and the model can issue a fresh valid call on the next step.
- Projection is deterministic for the same messages, budget, and estimator.
- A compaction marker reports removed message/tool counts and a SHA-256 digest
  of the public projection. Opaque `provider_state` affects the local audit
  digest and byte budget but never the digest sent to another model.
- Stored messages, checkpoints, events, approvals, and recovery semantics are
  unchanged.

## Non-goals

This first stage does not summarize omitted facts, retrieve long-term memories,
measure provider tokens by default, or mutate history. Projection and custom
estimation are synchronous; cancellation is cooperative and cannot preempt an
arbitrary blocking estimator, so estimators must remain local and bounded. The
marker explicitly tells the model not to infer omitted content. Semantic
compaction and durable Memory are separate roadmap items with separate trust
and evaluation gates. Retained valid tool turns remain bound to their original
provider continuation format; this adapter does not promise cross-provider
switching while such a turn is still present.

See [ADR-0009](ADR-0009-CONTEXT-PROJECTION-BOUNDARY.md) for the boundary and
trade-offs.
