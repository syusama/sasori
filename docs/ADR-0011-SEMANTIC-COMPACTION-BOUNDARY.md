# ADR-0011: Semantic compaction is a low-trust model adapter

- Status: accepted
- Date: 2026-08-09

## Decision

Sasori provides model-assisted semantic compaction as an explicit opt-in inside
the existing `sasori_context` extension. `SemanticCompactionModel` wraps a
primary `Model`, a structural `ContextProjector`, and a separately named
summarizer `Model`. The same `Harness` and `_drive()` path still serves Python,
CLI, HTTP, and Workbench application adapters. This feature adds no second
loop, checkpoint, store, provider wire format, or public event.

Deterministic structural projection remains the default. When the projected
history is already within budget, the summarizer is not called. When cold
turns must be removed, semantic compaction follows this sequence:

1. run the existing structural projector first, including all tool-atom and
   malformed-call rejection checks;
2. select exactly the cold messages that the projector would replace with its
   structural omission marker;
3. serialize their public projection as canonical JSON containing untrusted,
   model-visible data, excluding all opaque `provider_state`;
4. send that bounded payload to the named summarizer with an empty tool set;
5. accept only one strict JSON result that echoes the whole-source digest and
   carries a free-text note;
6. insert a host-authored system marker followed by the model-generated text as
   an ordinary assistant history note;
7. remeasure the complete primary-model input and fail if it exceeds the same
   configured context budget;
8. call the primary model through its unchanged `Model.complete()` contract.

The host marker identifies the prompt policy version, public source SHA-256,
summary SHA-256, and source-message count. It explicitly says the following
assistant note is lossy, unverified, model-generated, and non-authoritative.
Generated summary text is never promoted to a system message, tool result,
approval, event, checkpoint, or executable tool call. It can still influence
the primary model and its proposed calls; those calls receive no special
authority and still cross the normal Harness approval/effect boundary.

## Input, output, and lineage contract

The summarizer receives two messages and `tools=()`:

- a fixed host system instruction that treats the transcript payload as
  untrusted data and requires one exact JSON object;
- a canonical JSON user payload containing the prompt version, public source
  digest, output limit, and the selected public message projections.

The summarizer response must be a `ModelReply` with no tool calls. Its content
must decode as an object with exactly:

```json
{
  "version": 1,
  "source_sha256": "<64 lowercase hex characters>",
  "summary": "<non-empty bounded Unicode text>"
}
```

Duplicate JSON keys, non-finite constants, a mismatched source digest, unknown
fields, invalid Unicode/control characters, empty text, tool calls, non-model
responses, and oversized output are rejected. Digest equality proves which
whole request produced the note; it does not prove that a sentence is entailed
by any source message or that a tool outcome was reported faithfully. The
summarizer's private continuation state is ignored and is never forwarded to
the primary model.

Every attempted compaction produces a bounded, process-local diagnostic record
containing stable semantic fields: summarizer identity, prompt version, actual
prompt-policy digest, non-secret configuration digest, public source digest,
local full-state source digest, summary digest, source message/tool-call counts,
canonical source/prompt/summary bytes, summarizer call count, cache status,
outcome, and a stable failure code. These byte counts are observable size
proxies, not provider token or monetary billing claims. There is no duration,
endpoint, or provider prose in the stable record.

## Failure and cancellation

Semantic compaction is fail-closed. Sasori does not silently replace a failed
semantic compaction with the structural omission marker and does not call the
primary model after compaction failure. Source overflow, local deadline,
provider timeout/rate limit/refusal/incomplete/protocol and other typed provider
failures, malformed output, tool output, and final budget overflow map to stable
diagnostic codes. Provider exception prose and cause chains are not copied into
diagnostics, the primary prompt, or the raised semantic error.

Caller cancellation while awaiting the summarizer is recorded locally and
re-raised unchanged. The child request runs in a separate task; a local deadline
or caller cancellation cancels and discards that task without accepting a late
value, even if the child suppresses `CancelledError`. Cancellation remains
cooperative: Sasori does not claim that an arbitrary remote provider stopped
merely because the local await was cancelled or timed out. There is no automatic
retry, so the adapter does not claim exactly-once summarizer execution. An
under-budget direct call creates no compaction record, and a `succeeded` record
describes only compaction—not the later primary call or full Harness run.

## Cache boundary

The optional standard cache is a bounded process-local memo of validated,
final-fit summary text. Its key binds the public cold-source digest, public and
local structural-projection digests, projected units, caller-owned summarizer
identity, prompt version and actual policy digest, estimator name, context
budget, source limit, and summary limit. A successful exact hit can reuse the
same summary/marker segment without another summarizer call. It is not a claim
that the complete primary request or provider cache prefix is identical.

Python callers must change `summarizer_name` when model, endpoint, operator trust
domain, or other summary semantics change. First-party applications derive a
non-secret identity digest from provider, model, and effective endpoint. This
still does not identify an account, credential owner, or provider data-handling
policy.

The cache is not durable, distributed, run-owned, or a Memory store. Concurrent
first misses may each call the summarizer. The first validated candidate that
also fits the final primary projection wins for overlapping same-key callers;
an in-flight reservation pins that winner across unrelated LRU eviction. A
projection or budget failure is never inserted, and a concurrent winner is
remeasured before use. The full original transcript, not the cache, remains the
durable source record. Sasori does not summarize a previous summary in place:
every miss is bound to the selected original public messages.

## Security and privacy boundary

User, assistant, and tool content sent to the summarizer is untrusted
model-visible data and may contain prompt injection. The fixed instruction,
canonical JSON envelope, empty tool set, strict response schema, assistant-role
insertion, and host marker constrain the protocol and prevent direct summarizer
tool execution. They do not neutralize prompt injection, prove a factual claim,
or stop the derived note from influencing the primary model. Only the Harness's
ordinary approval/effect contracts constrain a proposed runtime tool call.

Enabling semantic compaction may make an additional provider request and sends
selected historical user/tool content to the explicitly configured summarizer.
It is never enabled implicitly. Operators remain responsible for provider data
handling and model selection.

## Durable state and public claims

The durable transcript, checkpoint, accepted model replies, tool ledger,
approvals, events, and recovery semantics are unchanged. The derived semantic
summary and diagnostics are process-local in this first slice. They are not
shown as durable Workbench history and cannot be claimed as public audit data.

This feature is not:

- long-term or cross-run Memory;
- lossless or guaranteed fact-preserving compression;
- an unlimited-context mechanism;
- a provider-token or monetary cost meter;
- a durable summary archive;
- a prompt-injection-proof sandbox;
- a host-owned tool-truth ledger or proof that the note preserved tool outcomes;
- proof of cross-provider continuation compatibility;
- a guarantee of lower latency or cost.

## Why

The structural projector already owns the difficult correctness boundary:
complete turns, indivisible tool call/result groups, safe normalization of core
rejections, protected system policy, hot turns, deterministic measurement, and
fail-closed overflow. Reusing that projection before a summarizer prevents a
second context parser from drifting away from the runtime contract.

A model adapter remains the smallest shared boundary. Every existing
application entry point already supplies a `Model` to the same `Harness`; an
explicit adapter composes there without making optional model/provider policy a
core concern.

## Consequences

- Python callers can compose the adapter directly.
- The first-party provider-backed applications can opt in through explicit
  bounded-context and summarizer environment configuration; the deterministic
  Incident application remains credential-free and unchanged.
- A successful compaction adds one summarizer request before the primary model
  request unless an exact validated cache entry is reused.
- Real-model summary quality requires a separately versioned evaluation corpus
  measuring fact recall, unsupported facts, contradictions, denied-effect
  correctness, and citation retention. Deterministic fakes prove the protocol
  and failure semantics, not general model quality.
- Durable Memory remains a separate module with source, score, scope, version,
  deletion, and rebuild contracts.

## Rejected alternatives

### Put the summarizer in `Harness._drive()`

Rejected because it would enlarge the inspectable core, couple recovery to an
optional provider call, and create semantic differences between otherwise
identical model adapters.

### Send raw provider continuation state to the summarizer

Rejected because it can disclose opaque vendor-private state and bind a
provider-neutral summary to another provider's wire format.

### Insert generated text as a system message

Rejected because it elevates untrusted derived content above retained user and
tool evidence.

### Allow the summarizer to call tools

Rejected because compaction is context derivation, not an authority-bearing
agent run. Tool execution remains exclusively inside the existing Harness
contract.

### Silently fall back after failure

Rejected because a caller could believe semantic facts were preserved when the
primary model actually received only an omission marker. A future fallback
policy, if needed, must be explicit and observable under a separate decision.

### Treat the free-text note as a tool-truth ledger

Rejected as a claim, not as a future feature. A whole-source digest and strict
schema cannot prove that free text preserved a denial, failure, approval, or
unknown effect. A future host-authored ledger would need to derive call/result
IDs, message digests, error codes, and outcome classes directly from validated
tool atoms and remain separate from the unverified model note. That contract is
not shipped in this slice.

### Persist summaries in the run checkpoint

Rejected for this slice. It would change recovery/public-state semantics and
blur semantic compaction with durable Memory. Such a change requires its own
schema, ownership, invalidation, deletion, and restart decision.
