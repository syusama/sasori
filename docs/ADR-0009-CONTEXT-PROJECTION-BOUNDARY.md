# ADR-0009: Context projection stays outside the runtime core

- Status: accepted
- Date: 2026-08-09

## Decision

Sasori provides dependency-free context budgeting as the optional
`sasori_context` extension. It wraps any `Model` before that model is passed to
the existing `Harness`; it does not add another loop, store, checkpoint format,
or public event type.

The first projection policy is deliberately structural rather than semantic:

1. leading system messages are protected;
2. a contiguous host-authored `ProtectedContextMessage` prelude may follow the
   leading system prefix. It remains an ordinary assistant data message, counts
   against the same budget, and gains no system/tool authority, but it cannot be
   silently removed as old conversation history. The marker is rejected outside
   that exact position or when it carries tool/result/error/provider metadata;
3. a user turn and its following assistant/tool messages form one removable
   unit;
4. an assistant tool-call message and every matching tool result are an
   indivisible atom;
5. a malformed or incomplete call that the core has already refused, followed
   by its exact structural-error result, is valid durable history. Because that
   history cannot be represented on provider tool wires, the adapter replaces
   the rejected atom with deterministic assistant/user text for the next model
   call. The text preserves each `error_code` but cannot authorize or repair a
   tool call;
6. at least the configured number of hot turns is protected;
7. removed messages are replaced by a deterministic system marker carrying
   counts and a SHA-256 digest over the public message projection. The local
   `ContextProjection.removed_sha256` separately covers the exact canonical
   history including opaque provider state;
8. if the system/data prefix, marker, and hot tail do not fit, projection fails
   closed instead of clipping content or splitting a tool protocol;
9. the standard-library estimator measures canonical UTF-8 JSON bytes. It is
   not described as a token counter. A provider-specific tokenizer may be
   supplied explicitly.

The durable run history is never rewritten. Projection changes only the tuple
sent to the next model call. The core `Harness`, SQLite checkpoint, public event
projection, and Workbench timeline retain the complete accepted history.

## Why

Context limits are a model-bound concern. Putting provider tokenizers, semantic
summarizers, retrieval stores, or memory policy into the core would make the
single-agent loop harder to inspect and couple recovery to optional products.
An ordinary `Model` adapter keeps one runtime path while allowing applications
to choose a policy.

Tool pairs require a stricter boundary than ordinary chat messages. Dropping an
assistant tool call without its result, or retaining an orphan result, produces
invalid OpenAI/Anthropic wire history. Sasori therefore validates structure
even when the unprojected input is already under budget.

The core's explicit rejection results are a deliberate exception, not an
orphan repair heuristic. The adapter recognizes only the core's structural
error codes and matching call/result shapes. Unknown or mismatched orphan
results still fail closed. Both OpenAI and Anthropic wire encoders accept the
normalized rejection record as ordinary text; neither receives the rejected
tool envelope or its vendor-private continuation state.

The protected data prelude exists for bounded host adapters such as durable
Memory recall. A fresh recall is part of the current model input, not an old
conversation turn. Treating it as an ordinary oldest assistant turn made recall
disappear exactly when long-history projection activated. Making it a system
message would incorrectly raise its authority. A typed ordinary-data marker is
the smallest shared boundary: providers still see `role=assistant`, while the
projector validates placement, metadata, and final-budget accounting.

## Consequences

- Applications opt in by wrapping a model with `BoundedContextModel`.
- `reserve_units` is an operator allocation for provider/tool-schema/output
  overhead; the default estimator cannot infer those costs.
- Projection and the estimator are synchronous, dependency-free work performed
  before the wrapped async model call. Cancellation propagates unchanged once
  control can return to the event loop; it cannot preempt an arbitrary slow or
  blocking custom estimator. Estimators must therefore be local and bounded.
- The wire-visible deterministic marker proves which public projection was
  removed, but it preserves no omitted facts. It excludes `provider_state` so
  switching models does not disclose a stable fingerprint of another vendor's
  opaque continuation or reasoning state. The full-state digest remains local.
- A retained, valid tool turn keeps its genuine provider continuation state and
  therefore remains wire-specific. This adapter does not claim that a run can
  switch providers while such a turn is retained. Once that old turn is
  removed, only the provider-neutral public marker is sent onward.
- The opt-in semantic compactor accepted by
  [ADR-0011](ADR-0011-SEMANTIC-COMPACTION-BOUNDARY.md) is another model adapter.
  It keeps tool atoms intact, exposes its nondeterminism through bounded local
  diagnostics, and does not rewrite this first structural-projection decision.
- Durable Memory remains outside core. Its bounded host-authored projection may
  use the protected ordinary-data prelude, drop complete low-ranked records to
  fit, and then share this exact final budget with structural or semantic
  projection. It does not mutate the stored transcript.

## Rejected alternatives

### Truncate strings in place

Rejected because it can corrupt JSON arguments, provider continuation blocks,
citations, and the distinction between an executed tool call and prose.

### Keep only the last N messages

Rejected because message count is not a size budget and can retain an orphan
tool result.

### Add compaction to `Harness._drive()`

Rejected because every Python/CLI/HTTP/UI path already converges on the same
Harness. Context policy can reuse that path through the existing `Model`
contract without enlarging the loop.

### Claim token accuracy without a provider tokenizer

Rejected. Canonical byte units are stable and testable; token counts depend on
the selected provider/model/tokenizer and must be named explicitly.
