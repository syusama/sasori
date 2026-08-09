# ADR-0012: Durable bounded Memory boundary

Status: **accepted for a local single-owner vertical slice**

Date: 2026-08-09

Decision owner: repository maintainer

## Context

Sasori has three records that must not be renamed as long-term Memory:

- the durable run transcript is the recoverable Loop record;
- `sasori_context` produces deterministic omissions or a lossy, unverified
  process-local semantic note;
- `com.sasori.rag-sqlite` indexes external document evidence.

None of them owns a long-lived, versioned, deletable projection of remembered
items. A Memory extension must add that product value without adding a second
agent Loop or moving persistence, retrieval, provider calls, or identity policy
into the core package.

The current runtime also has an identity limit that the design must not hide:

- `Harness.run()` knows `run_id` and `app_id`, but `Model.complete()` does not;
- a server process creates one long-lived Harness per application;
- the bearer token authenticates the Sasori instance, not a user or tenant;
- there is no public `session_id`, principal mapping, or per-request owner.

Consequently, this ADR does not authorize personal, tenant-isolated, or dynamic
per-request Memory in the current HTTP product.

## Decision

### 1. Core stays unchanged

Durable Memory lives in `sasori_memory`, uses a separate SQLite file, and
composes through the existing `Model`, `Tool`, `Harness.event_sink`, and plugin
contracts. The core does not import `sasori_memory`, add Memory tables, create a
background extractor, or gain another execution loop.

The first slice has one runtime path:

```text
Harness
  -> application PromptedModel
  -> MemoryContextModel
  -> structural / semantic context adapter
  -> provider
```

Memory retrieval therefore enters before the existing final context projection
and budget measurement. It cannot create an independent unlimited prompt tail.

### 2. v1 identity is fixed and deployment-owned

Every operation binds the complete immutable identity:

```text
owner_id + app_id + scope_id + session_id + binding_revision
```

Deployment namespace identifiers are validated lowercase ASCII IDs. Runtime
`run_id` follows the core `[A-Za-z0-9][A-Za-z0-9._-]{0,63}` contract, while
provider call IDs use the shared core contract: 1 through 256 UTF-8 bytes, no
NUL, opaque, and case-preserving. A longer or structurally invalid call ID is
rejected before tool dispatch. Source-turn cursors are separately bounded
opaque UTF-8 strings. Memory preserves both fields exactly instead of
lowercasing them. The trusted Python host or deployment configuration supplies
the namespace. The model cannot select or override owner, application, scope,
session, or binding revision.

The first-party server integration, when enabled, is explicitly:

```text
local-single-owner + fixed application scope + fixed session namespace
```

All runs of that configured application share the fixed namespace. This is
useful for a private local worker, but it is not a user/tenant identity model.
Changing the configured binding after a run was bound fails closed; it never
silently reads another namespace.

### 3. A post-commit bridge carries run context, not Memory truth

`MemoryRunBridge` is a synchronous Harness event sink. Core commits an event
before invoking the sink. On `run.started`, `model.started`, and `tool.started`,
the bridge idempotently creates or validates the durable run-to-Memory binding
and sets an immutable `ContextVar` value for the current asyncio task. Each
committed model/tool start also creates a process-local one-use invocation
lease. Context copies into the Harness child task or worker thread share that
lease; only the first adapter/handler claim succeeds. Replacement, completion,
failure, approval pause, or terminal events revoke the old lease.

The bridge must never store request identity on the shared model or Harness
instance. Concurrent Python callers receive task-local values. A bridge error is
recorded locally and leaves the task without a Memory context. Since core
observers are best effort and swallow observer failures, `MemoryContextModel`
and Memory tool handlers independently reload the durable binding. Missing,
stale, already-consumed, or mismatched context fails before retrieval or
mutation. The lease is not durable execution authority: it only prevents a
copied or parent-task context from authorizing another invocation after the
committed phase has been consumed. Durable run binding is still reloaded from
SQLite.

The event sink is not proof that a Memory mutation committed. Durable writes use
their own SQLite transaction and the Harness idempotent-tool recovery contract.

### 4. One record protocol, immutable revisions

Memory kinds are `episodic`, `semantic`, and `procedural`, but they share one
record, version, deletion, retrieval, and recovery protocol. A record contains:

- schema version, deterministic `memory_id`, and caller-defined `logical_key`;
- immutable positive `revision` and current status;
- kind, content, content SHA-256, and bounded priority;
- owner/application/scope/session identity;
- source kind, run ID, turn ID, model step, call ID, source SHA-256;
- source authority (`model_proposed_unverified` in the first tool slice);
- extractor identity and policy digest;
- observation identity, collection revision, and stable sequence.

`memory_id` is derived from the fixed binding plus `logical_key`. A create uses
`expected_revision=0`; an update or delete must match the current revision.
Updates append a new immutable revision and atomically move one mutable head.
Stale CAS writes fail. A replay with the same idempotency key and request digest
returns its previously committed result; the same key with different bytes
fails. Before returning a replay, the store verifies operation kind, complete
binding, committed audit row, result digest, strict operation-specific envelope,
and the immutable record/observation or suppression state behind that result.
A canonical but forged result is corruption, not a cache hit.

The first slice performs no LLM extraction. `remember_memory` stores an
explicit, human-approved model proposal and labels it unverified. Later
extractors require their own prompt/model/policy identity, source echo, attempt
state, and real-model quality gate. Source association never proves factual
entailment.

### 5. Source and same-session replay identity

A model-proposed mutation derives its source from the already committed Harness
tool call, never from model-provided owner/scope fields. The observation key
binds at least:

```text
owner + app + scope + session + run + turn + step + call
+ source digest + extractor identity + policy digest
```

Two runs in the same configured session namespace remain distinct turns. A
replay of the same accepted tool call is idempotent. Equal text in a different
turn keeps distinct provenance and can create a later revision only through an
explicit expected-revision mutation.

The store never persists or hashes vendor-private `provider_state` as source
lineage. Within the shared 1-through-256-byte public bound, provider call IDs
are opaque, case-preserving evidence fields; they are not namespace identifiers
or idempotency keys. A 257-byte value is rejected at the core/provider boundary
before approval or dispatch.

### 6. Bounded lexical retrieval

The authoritative record/version tables are separate from a derived lexical
index. Retrieval applies owner/application/scope/session and active-generation
filters in SQL before ranking. It has hard limits for:

- query UTF-8 bytes and normalized terms;
- scope records and candidates;
- `top_k`, one-record bytes, and total returned bytes;
- injected JSON bytes and final primary request units.

The explicit `search_memory` tool keeps these strict limits. Automatic recall
does not shrink the Agent's valid input contract to 2,048 bytes or sixteen
words: it deterministically normalizes the latest user message, clips at a
UTF-8 boundary, keeps at most the configured distinct lexical terms, and binds
source/projection digests plus a truncation flag. An overlong ordinary prompt
therefore produces bounded recall or no hits, not a failed run.

The deterministic baseline returns complete records only. It never slices a
record in half to fit. Rank is stable:

```text
matched distinct terms descending
-> priority descending
-> active sequence descending
-> memory_id ascending
```

Each hit exposes `rank`, integer `score`, `score_kind=term_coverage_bps`,
`score_direction=higher_is_better`, and retrieval-policy version. The score is
relevance under that exact lexical policy, not confidence or factual truth.
Embeddings and model reranking remain optional future adapters.

### 7. Retrieval is untrusted model-visible data

`MemoryContextModel` inserts a host-authored system guard and canonical JSON in
a `ProtectedContextMessage`. Providers still receive an ordinary assistant data
message; the type is only a host-side budget marker. It is valid solely as a
contiguous prelude after leading system messages, carries no tool/result/error/
provider metadata, and is charged to the same final context budget.

Each injected hit retains identity, revision, source, content digest, and score.
When the complete projection does not fit, the adapter removes whole lowest-
ranked hits, increments `omitted_count`, and remeasures. It never slices a hit or
silently discards the entire fresh recall as an old conversation turn. If even
the empty omission envelope plus current protected request cannot fit, the
native `ContextBudgetExceeded` propagates; downstream provider/projection errors
are not relabeled as Memory retrieval failures.

Recalled text may contain prompt injection and may influence the primary model.
It is never converted into:

- system policy;
- a provider tool call or tool result;
- approval or effect evidence;
- a public Sasori event or checkpoint.

The Harness still validates every new tool proposal. This role and execution
boundary does not make prompt injection inert or prove the model will ignore it.

### 8. Delete means deleting the Memory projection

Exact delete appends a tombstone and immediately removes the active record from
retrieval. Source delete adds a durable source suppression and tombstones every
matching active record. Scope delete makes the entire fixed namespace
suppressed. Repeated deletion is idempotent. A suppressed scope is a normal
closed state for reads: search returns a versioned empty envelope with
`scope_status=suppressed`, so the deleting run can produce its confirmation and
future runs can continue without recall. Writes and rebuild remain rejected;
there is no implicit restore.

Suppression is authoritative during rebuild, so a replay of an old observation
cannot resurrect a forgotten projection. There is no implicit restore in v1.

Memory deletion does not delete the Sasori transcript, events, artifacts,
provider records, logs, or backups. The product must never describe it as total
erasure or claim that an external provider forgot the data.

### 9. Rebuild uses an atomic generation switch

Lexical terms are derived. Rebuild writes a new generation from current,
non-suppressed heads, validates it, then switches the scope's active generation
in the same transaction. Readers see the complete old or complete new
generation, never a mix. A crash before commit leaves the old generation active.

Ordinary record mutations update the currently active generation in the same
transaction as the new head and audit record. Old index generations are retained
until an explicit future garbage-collection policy exists.

### 10. Failure is explicit and bounded

The first slice treats Memory as `required` when its adapter/tools are enabled.
Missing run context, consumed lease, binding mismatch, schema mismatch, corrupt
rows, invalid Unicode, CAS conflict, idempotency conflict, database failure, or
index inconsistency fails before the primary model or mutation. Explicit search
keeps strict query limits; automatic recall projects long prompts into them.
Scope suppression produces an explicit empty read envelope but still rejects
writes/rebuild. Memory never injects a partial record or falls back to a global
namespace, old process cache, RAG collection, or semantic summary.

No provider prose, database SQL text, secret, owner ID, or raw path enters public
events. Memory audit is MemoryStore-owned; adapter diagnostics are bounded and
process local. Adding `memory.*` public Sasori events would require a separate
event-contract decision.

### 11. Recovery claim

Memory mutations are ordinary Harness idempotent tools. If the Memory transaction
commits and the process fails before the tool result commits, resume reuses the
same Harness idempotency key. The Memory operation table returns the exact prior
result and does not append another active revision.

This is step-boundary recovery. It does not prove a remote extractor stopped,
prevent duplicate provider requests or billing, or create a cross-database
exactly-once transaction between the core run DB and Memory DB.

## Rejected alternatives

- **Persist semantic compaction output and call it Memory.** Rejected because the
  note is lossy, unverified, process-local, and has no deletion/version contract.
- **Use RAG collections as personal Memory.** Rejected because document evidence
  collection and owner-scoped remembered projections have different authority,
  source, revision, and deletion semantics.
- **Let the model send `owner_id` or `scope_id`.** Rejected because untrusted
  prompt content could select another namespace.
- **Treat the bearer token as a user identity.** Rejected because it authenticates
  one server instance and has no principal mapping.
- **Store current run state on a shared adapter instance.** Rejected because one
  app Harness is reused and direct Python callers may be concurrent.
- **Automatically extract after `run.completed`.** Rejected from v1 because there
  is no durable outbox/cursor or extractor-attempt reconciliation contract yet.
- **Delete and rebuild the active index in place.** Rejected because a crash could
  expose an empty or mixed projection.

## Acceptance gates

The slice is not Durable bounded Memory until deterministic tests prove:

1. foreign owner/scope/session data is filtered before ranking;
2. bridge context is task-local, immutable, one-use, restart-stable, and fail
   closed after fault/cancellation/phase replacement;
3. two runs in one fixed session remain distinct; replay does not duplicate;
4. immutable revision/CAS and idempotency conflicts reject stale writes;
5. exact/source/scope deletion cannot be undone by replay or rebuild;
6. rebuild fault injection exposes only an old or new complete generation;
7. explicit and automatic query/candidate/result/injection/final-context budgets
   and tie-breaks are exact; long prompts and large matches keep the current turn;
8. corrupt/unknown schema and invalid Unicode never reach the primary model;
9. malicious recalled text cannot directly become system/tool/approval/effect
   state, while documentation still admits it can influence the model;
10. a crash after Memory commit but before tool-result commit resumes to one
    active revision and one durable operation result;
11. existing core, provider, CLI, HTTP, Workbench, package, and container gates
    remain unchanged when Memory is disabled.
12. uppercase core run IDs and opaque provider call IDs within the shared
    1-through-256 UTF-8 byte public bound retain exact identity, while invalid
    or 257-byte IDs are rejected before dispatch;
13. operation replay rejects a forged canonical result, audit digest, kind, or
    binding instead of returning it as committed truth;
14. scope delete completes its run, later reads are empty, and later writes or
    rebuilds remain suppressed;
15. structural and semantic compaction preserve the protected Memory prelude
    under one final budget.

Real-provider extractor/retriever/end-to-end metrics are a separate gate. Until
those measurements exist, Sasori does not publish factual precision, Recall@k,
token, latency, billing, or cost-savings numbers for Memory.

This ADR is accepted for the deliberately narrow local single-owner slice after
the implementation, disabled-path regression, packaging, and mainland-source
container gates above passed on 2026-08-09. Exact-revision Hosted CI evidence is
still pending and must be promoted separately; acceptance does not expand the
identity, retrieval, quality, or deployment boundaries stated above.
