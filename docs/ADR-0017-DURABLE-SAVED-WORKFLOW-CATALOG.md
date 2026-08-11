# ADR-0017: Durable saved Workflow catalog

- Status: Accepted for W1.3 implementation
- Date: 2026-08-10
- Depends on: [ADR-0014](ADR-0014-STATIC-SERIAL-AUTHORING-PUBLIC-PROJECTION.md),
  [ADR-0015](ADR-0015-STATIC-WORKFLOW-MANIFEST-PREFLIGHT.md), and
  [ADR-0016](ADR-0016-STATIC-SERIAL-WORKFLOW-STUDIO.md)

## Context

W1.2 accepts one transient strict-JSON serial Workflow draft, preflights it
against the startup-frozen ordinary-Harness Tool registry, and returns a
detached immutable manifest. It deliberately has no durable authoring state.

W1.3 adds the smallest durable authoring authority that preserves the existing
runtime boundary. A saved definition must survive restart, stale writers must
not overwrite a newer save, and every accepted historical definition must stay
readable without becoming executable. This requires identities and revisions
that are separate from the fields already owned by `WorkflowSpec`.

The local comparison baselines are LeAgent
`1f16badc834abbd829d3cb7e9f8fcb5b2d57f443` and ToFu
`3a457c6ca685d4a36ec5f69d6636b7adc5c8d092`. LeAgent's HTTP update mutates the
current Flow row and increments a counter without expected-revision CAS, while
its Agent save path has a different ownership/publication boundary. ToFu keeps
a global mutable JSON catalog and permits an inline unsaved definition to run.
Sasori reuses neither behavior. Atomic file or database writes do not replace
stale-writer detection, and saving never grants execution authority.

## Decision

### 1. W1.3 is a saved authoring catalog, not an activation registry

W1.3 supports exactly:

- conditionally creating a saved static serial Workflow;
- conditionally appending an immutable definition revision;
- listing saved heads;
- reading the current or an exact historical revision;
- reporting whether a saved revision still matches the current process's
  startup-frozen Tool registry; and
- editing, loading, saving, and resolving conflicts in the Workbench.

W1.3 does not support delete, restore, purge, metadata mutation, import,
sharing, publish, activate, deploy, schedule, run-from-draft, run-from-saved,
visual DAGs, branches, parallel ready sets, Agent nodes, or subflows. A later
delete slice must use a CAS tombstone and must never reuse a `catalog_id`; a
later restore must append a new revision rather than rewrite history. Those
contracts are intentionally not implied by W1.3.

No saved definition is loaded into `_harnesses`, `/v1/apps`, or the Tool
registry. Server startup does not compile saved records. `POST /v1/runs`
continues to accept only deployer-configured application identities.

### 2. Catalog and definition identities are distinct

The following values have separate authorities:

| Value | Authority and meaning |
|---|---|
| `catalog_id` | immutable saved-record identity, `wfcat_` plus 32 lowercase hexadecimal UUID digits |
| `catalog_revision` | server-managed positive integer for one saved record's immutable revision history |
| `definition.workflow_id` | logical identity inside the existing strict Workflow definition |
| `definition.version` | author-controlled string inside the existing strict Workflow definition |
| `definition_sha256` | existing canonical Workflow definition content identity |
| `saved_manifest` | detached manifest accepted when this exact catalog revision was saved |
| `current_contract` | read-time compatibility verdict under the current process's frozen Tool registry |

The client generates a random version-4 UUID and formats the catalog identity
as `wfcat_<32 lowercase hex digits>`. The server validates the exact shape and
UUID version/variant, then makes the identity immutable. The client cannot
choose a revision, digest, manifest, owner, lifecycle state, or execution
identity.

Client-generated collision-resistant identity is chosen for one narrow reason:
a create that committed before its HTTP response was lost can be reconciled by
`GET` of the already-known identity, and replaying `PUT ... If-None-Match: *`
cannot create a second record. This avoids a second idempotency-key ledger while
retaining a stable recovery handle. Names, titles, Workflow IDs, and definition
versions are never persistence lookup keys.

### 3. The catalog has a mutable head and immutable snapshots

The catalog uses an independent SQLite database with schema identity
`application_id` and `user_version=1`. It has two logical tables:

```text
workflow_heads
  catalog_id, catalog_seq, current_revision,
  current_definition_sha256, current_workflow_id, current_definition_version

workflow_revisions
  catalog_id, catalog_revision, parent_revision,
  workflow_id, definition_version, definition_sha256,
  definition_json, manifest_sha256, manifest_json
```

`(catalog_id, catalog_revision)` is unique and revision rows are never updated
or deleted. A head can point only to a revision with the same catalog identity,
revision number, digest, Workflow ID, and definition version. `catalog_seq` is
assigned once and supplies stable descending pagination.

Definition and manifest documents are canonical UTF-8 JSON bytes. The
definition is limited by the existing 1 MiB Workflow boundary. Stored bytes,
digests, head/snapshot relations, and decoded exact contracts are verified on
read. A mismatch fails closed as catalog integrity failure; Sasori does not
silently repair or reinterpret it. These checks detect accidental corruption,
not an attacker who can consistently rewrite the local database. W1.3 does not
claim signed provenance, encryption at rest, or secure erasure.

Opening an existing version-1 catalog verifies the exact table, constraint,
composite-foreign-key, and immutable-trigger definitions, requires
`PRAGMA foreign_keys=1`, and runs `PRAGMA foreign_key_check`. Current detail,
historical detail, and list pagination all pass through one head validator. It
binds the head to the current snapshot's identity, revision, digest, Workflow
ID, and definition version and requires a contiguous revision chain from 1 to
the current revision. Stored scalar decoding is strict; invalid caller input
remains a caller error, while an invalid stored scalar is an integrity failure.
The `limit + 1` pagination sentinel is validated before it may imply another
page, so a corrupt hidden row cannot manufacture a cursor.

### 4. The catalog is outside core and owns a separate database

`sasori_flow.catalog` owns the store and saved-Workflow service. It does not add
tables or methods to core `sasori.SQLiteStore`. The default file path is derived
from the run database:

```text
sasori.sqlite3 -> sasori.workflows.sqlite3
```

`--workflow-db` and `SASORI_WORKFLOW_DB` may override it. The run and catalog
paths must not resolve to the same file. `:memory:` is supported only for
deterministic tests and is never durable evidence.

The file-backed catalog holds a non-blocking process lock for its lifetime and
uses SQLite `foreign_keys=ON`, `synchronous=FULL`, WAL, exclusive locking, and
`BEGIN IMMEDIATE` writes. Definition snapshot insertion and head creation or
CAS movement commit in one transaction. There is no transaction across the
catalog and run databases because W1.3 performs no run mutation.

The runtime owner opens and validates the catalog before readiness, then opens
the run store and artifacts, loads configured Harnesses, and freezes the Tool
registry. Startup opens and migrates the catalog but does not enumerate or
compile saved records. Catalog schema/lock failure makes readiness fail closed;
an individual saved definition that later becomes Tool-incompatible does not.

Shutdown stops accepting owner calls and settles them before closing artifacts,
the run store, and the independent catalog store. Each cleanup step runs even
if an earlier close fails. Catalog operations do not acquire the run mutation
gate and cannot alter its concurrency or recovery semantics.

### 5. Save uses the one strict codec, compiler, and frozen Tool registry

Create and update accept the existing Workflow definition as the request body.
They do not introduce a relaxed authoring schema or client-supplied manifest.
The request passes through:

```text
strict HTTP JSON
  -> workflow_spec_from_data
  -> existing shared compiler/preflight
  -> startup-frozen ordinary-Harness Tool registry
  -> canonical definition + detached manifest
  -> one catalog transaction
```

Duplicate JSON keys, unknown or missing fields, non-finite values, invalid
Unicode, invalid UTF-8, BOMs, excessive body size, invalid Tool contracts, and
ambiguous or unavailable Tools fail before catalog mutation. Model methods,
Tool handlers, wrapper handlers, idempotency callbacks, run/checkpoint/event/
approval/effect/artifact stores, subprocesses, and the network are not invoked.

Literal values remain part of the canonical definition and are stored in local
SQLite plaintext. Authenticated detail reads return the full definition. The
saved manifest continues to expose literal type, size, and digest instead of
the literal value. Errors, audit evidence, and list summaries must not copy
definition literals.

### 6. HTTP create/update use strong conditional requests

The authenticated deployment-owner API is:

```text
GET /v1/workflows?limit=<1..100>&before=<catalog_seq>
GET /v1/workflows/{catalog_id}
GET /v1/workflows/{catalog_id}?revision=<positive integer>
PUT /v1/workflows/{catalog_id}
```

`/v1/workflows/preflight` remains the W1.2 zero-mutation endpoint. Exact routing
distinguishes it from catalog IDs, which always start with `wfcat_`.

Create requires exactly one `If-None-Match: *` and no `If-Match`. Update
requires exactly one strong current `If-Match` and no `If-None-Match`. Missing
preconditions return `428 workflow_catalog_precondition_required`. Repeated,
weak, wildcard-on-update, list-valued, malformed, or mutually exclusive
conditional headers fail closed as `422 invalid_request`. A stale create or
update returns `412 workflow_catalog_revision_mismatch`.

The strong ETag is deterministic and binds all CAS identity:

```text
"sasori-wfcat-<32 hex>-r<catalog_revision>-<definition_sha256>"
```

CAS is checked again inside the same `BEGIN IMMEDIATE` transaction that inserts
the snapshot and moves the head. Two writers using the same ETag can produce at
most one new revision. A stale write remains stale even if its definition bytes
happen to equal the new head. When the expected ETag is current and the new
canonical digest and manifest bytes equal the head, update is a no-op: it
returns the current record and ETag without appending a false revision.

Create returns `201`; update and no-op return `200`; reads return `200`.
Mutation and detail responses include exact schema-version-1 envelopes, the
strong `ETag`, and `Cache-Control: private, no-store`. Create also returns the
exact `Location`. List returns bounded head summaries, never definitions or
manifests, and uses stable `catalog_seq` pagination.

Historical detail is immutable. `revision=N` never means "expected revision"
and is not accepted on mutation paths.

### 7. Saved evidence and current compatibility are different facts

Every snapshot preserves the exact manifest accepted at save time. Detail
reads also re-run the zero-execution preflight against the current process's
already-frozen Tool objects and return one bounded verdict:

```json
{"status":"compatible","reason_code":null}
```

or:

```json
{"status":"incompatible","reason_code":"tool_contract_mismatch"}
```

Read-time validation never changes the definition, saved manifest, revision,
head, or any runtime state. Tool effect/revision/schema/signature/ambiguity
drift cannot make an old `saved_manifest` look currently accepted. A malformed
or corrupted saved record fails closed instead of producing either verdict.

### 8. Authorization is deployment-wide, not multi-user ownership

The existing bearer check and exact Origin policy run before body read, JSON
parse, codec, preflight, or catalog access. Current bearer authentication has no
subject, claims, tenant, or row-level principal. W1.3 therefore describes one
deployment-owner catalog. Every caller admitted by the deployment-wide bearer
can read and modify the whole catalog.

The database stores a fixed schema owner marker only to prevent future schema
migration from confusing this scope with a user identity. It does not establish
RBAC, tenant/workspace isolation, actor attribution, public sharing, or
publisher provenance. Trusted loopback no-auth remains an explicit deployment
choice, not an authenticated authoring claim.

### 9. Browser mutation outcomes are fail closed

Workbench state is keyed by selected `catalog_id`, loaded ETag, edit epoch, and
request identity. Only an exact success envelope whose identity, revision,
digest, ETag, captured draft, and edit epoch all match may display `SAVED`.

An exact `412 workflow_catalog_revision_mismatch` displays `CONFLICT`, retains
the local draft, and offers an explicit refresh that presents server-current
and local-draft facts without automatic merge or overwrite. Exact static
definition/preflight rejection displays `REJECTED`.

When the owner accepted a saved-Workflow PUT but the HTTP adapter cannot obtain
its authoritative result, the public response is exactly
`504 workflow_catalog_outcome_unknown`, with `retryable=false`, the matching
request `catalog_id`, and `Cache-Control: private, no-store`. It has no
`Retry-After`, `ETag`, or `Location`. A network failure, client abort, shutdown,
non-JSON response, or malformed success is locally treated the same way:
`OUTCOME UNKNOWN`, never "not saved" and never an automatic PUT retry.

Recovery state is retained per `catalog_id`, not only for the selected card.
The browser may perform a read-only GET of that identity. A matching head proves
that the submitted desired definition is now durable; it does not attribute the
commit to one particular HTTP request. A differing head is a conflict and
preserves the local draft. A create GET that returns exact not-found establishes
current absence but still requires an explicit later create. Transport,
malformed, storage, or integrity failure leaves the outcome unresolved.
Switching records invalidates only the visible verdict for the active editor;
it does not discard another record's recovery context. Late PUT success, late
outcome-unknown handling, and late reconciliation/detail GET results cannot
replace the newly selected editor, ETag, label, or draft.

List loading uses the stable `before` cursor, validates each page contract, and
deduplicates `catalog_id` across pages. Before a detail record enters the editor,
the browser recomputes SHA-256 over the canonical definition bytes and binds it
to the returned manifest digest, record digest, and strong ETag.

The browser does not store definitions in `localStorage` or IndexedDB. No
browser state is persistence authority.

## Failure and recovery contract

- A fault before commit leaves the prior head and snapshots intact.
- A fault after commit but before response leaves one complete new revision;
  GET by the stable catalog identity recovers the result.
- Cancellation of the owner future, request timeout, client disconnect, or
  `AbortController` does not prove a synchronous SQLite operation stopped.
- A saved-catalog owner timeout therefore returns the exact non-retryable 504
  outcome-unknown envelope. It states only that the adapter lacks an
  authoritative result; it does not state that commit succeeded or failed.
- Reconciliation is GET-only. A matching GET establishes the desired durable
  head, a differing GET establishes conflict, and a create not-found establishes
  current absence. None authorizes an automatic repeat of the original PUT.
- Restart can observe only a complete old head or a complete new head. It can
  never expose a snapshot without its transaction's head state or a head that
  references a missing snapshot.
- Checkpoint/resume and side-effect recovery remain exclusively owned by the
  existing Harness runtime. W1.3 does not claim exactly-once execution.

## Acceptance gates

W1.3 cannot be promoted until runnable checks establish all of the following:

1. schema identity, exact DDL/trigger/composite-FK fingerprint, foreign-key
   check, empty initialization, foreign/unknown/newer schema refusal,
   second-owner exclusion, close/reopen, and run/catalog same-path refusal;
2. canonical definition/manifest round-trip, strict stored-scalar taxonomy,
   shared current/historical/list head validation, pagination-sentinel
   validation, and read-time detection of byte, digest, head, revision-chain,
   and manifest corruption;
3. create revision 1, immutable revision reads, update revision increment,
   current-identical no-op, stale-identical rejection, and two-writer CAS;
4. faults before snapshot insert, after snapshot insert, after head CAS, and
   immediately after commit produce only complete old/new states after reopen;
5. strict HTTP body/path/query/header/auth/origin cases fail before forbidden
   parser, compiler, handler, runtime-store, or external-action calls;
6. saved Tool contract drift remains readable as incompatible without changing
   the saved manifest, revision, or catalog bytes;
7. catalog create/update/list/get leave every run/event/checkpoint/call/
   approval/effect/artifact table and external action ledger unchanged;
8. using a catalog identity or definition identity with `/v1/runs` cannot
   execute a saved Workflow;
9. exact 504 body/header taxonomy with no `Retry-After`, no second PUT, and
   GET-only recovery, plus real-browser desktop, narrow reduced-motion, stable
   pagination, canonical digest mismatch, stale edit, late PUT/detail/recovery
   record switching, conflict, malformed success, and invalid Unicode;
10. deterministic per-member Deflate-9/BZIP2-9 repacking, normal pip
    extraction/installation acceptance for both the original and
    rebuilt-sdist wheel on Python 3.11-3.13 across Linux and Windows,
    release-verifier acceptance, and
    a no-cache mainland-source container create/update/restart/read journey;
11. exact implementation-SHA Hosted CI and a later promotion commit's own
    Hosted CI. Direct wheel `zipimport` is not a supported installation path.

Golden evidence compares semantic identity, revision, digest, manifest, and
verdict fields. It excludes timestamps, transport prose, and other documented
nondeterministic values.

## Consequences

Sasori gains durable static Workflow authoring with a recovery handle and real
optimistic concurrency while keeping one readable execution runtime. Immutable
history costs additional local storage, and full definitions intentionally
remain plaintext. The catalog remains a trusted local/deployment-owner feature,
not a public multi-tenant service.

W1.3 still does not match LeAgent or ToFu in saved graph/DAG breadth, multi-Agent
product features, or visual editing. It establishes a narrower, stronger
definition/history/CAS/no-execution contract on which later lifecycle slices
can be built without changing the Harness.
