# Local HTTP and SSE API

Status: experimental single-node adapter. It is designed for local Workbench and black-box acceptance, not public multi-tenant SaaS.

## Start

```powershell
sasori-server --host 127.0.0.1 --port 8080 `
  --db .\runs.sqlite3 `
  --workflow-db .\workflows.sqlite3 `
  --artifact-root .\artifacts `
  --app incident=sasori_apps.incident:create_harness `
  --app research=sasori_apps.research:create_harness `
  --app developer=sasori_apps.developer:create_harness `
  --trusted-loopback-no-auth
```

For any non-loopback bind, and normally for loopback too, use `SASORI_SERVER_TOKEN` or `--token-file`. All `/v1/*` requests then require `Authorization: Bearer ...`. Tokens in query strings are not supported. Exact same-origin browser requests are accepted. Cross-origin access is absent by default; each `--cors-origin` is an exact origin, never `*`, and cookies are not used.

The process owns one file-backed run `SQLiteStore`, one independent saved
Workflow catalog database, and freezes one `app_id -> Harness` mapping at
startup. `SASORI_WORKFLOW_DB` is equivalent to `--workflow-db`; when omitted,
`sasori.sqlite3` derives `sasori.workflows.sqlite3`. The two authorities must
not resolve to the same file. Every loaded Harness uses the run store and the
same Harness implementation. All access runs on one asyncio owner thread, and
one mutation gate covers all applications. Saved Workflow catalog operations
use their own SQLite transaction/CAS boundary and do not acquire that runtime
gate. Another run/resume/approval/effect request receives `503 runtime_busy`
with `Retry-After: 1`; it is not described as queued. GET application catalog,
saved Workflow, history, status, and events can run while a model or threaded
tool is awaiting.

The process also owns one immutable artifact root. `SASORI_ARTIFACT_ROOT` is
equivalent to `--artifact-root`; a file-backed database defaults to a sibling
path with the `.artifacts` suffix. `--publish-final-artifact` explicitly enables
the bundled host policy that creates one idempotent Markdown copy of a
completed final message. It is disabled by default and does not change the
Loop or imply that tools automatically publish files. The strict environment
equivalent is `SASORI_PUBLISH_FINAL_ARTIFACT=1`.

## Run projection

Every CLI/HTTP response uses the same public projection:

```json
{
  "run_id": "run-123",
  "app_id": "incident",
  "input": "checkout latency is high",
  "state": "paused",
  "pause_reason": "approval_required",
  "detail": "awaiting_approval",
  "step": 2,
  "revision": 9,
  "generation": 9,
  "latest_seq": 8,
  "final_message": null,
  "pending": {
    "fingerprint": "sha256...",
    "call_id": "call-1",
    "tool_name": "record_action",
    "arguments": {"summary": "..."},
    "effect": "side_effecting",
    "idempotency_key": null,
    "tool_revision": "1"
  }
}
```

For a run whose exact loaded Harness is a Sasori typed Workflow, create,
approval, effect, resume, and single-run status add one optional top-level
`workflow` extension:

```json
{
  "schema_version": 1,
  "workflow_id": "incident-mechanism",
  "version": "1",
  "definition_sha256": "sha256...",
  "app_id": "flow.incident-mechanism....",
  "execution": "single-harness-ordered-tools-v1",
  "output_step": "record",
  "current_step_id": "record",
  "latest_seq": 11,
  "steps": [
    {
      "position": 2,
      "step_id": "record",
      "kind": "tool",
      "logical_tool_name": "record_action",
      "dispatch_tool_name": "wf_record_...",
      "effect": "side_effecting",
      "logical_tool_revision": "1",
      "dispatch_tool_revision": "sha256...",
      "logical_schema_sha256": "sha256...",
      "dispatch_schema_sha256": "sha256...",
      "result_type": "string",
      "max_result_bytes": 32768,
      "call_id": "wf_...",
      "status": "approval_required",
      "error_code": null
    }
  ]
}
```

Machine status is one of `pending`, `requested`, `running`,
`approval_required`, `resume_required`, `retryable_idempotent`,
`effect_unknown`, `completed`, `failed`, or `stopped`. The extension is
canonical-JSON bounded to 256 KiB and does not include Workflow input,
arguments, literal values, intermediate output, result envelopes, approval
fingerprints, idempotency keys, recovery reasons, provider state, timestamps,
or exception prose. `stopped` is a projection of terminal run state, not proof
that an arbitrary remote or synchronous operation was forcibly cancelled.
`call_id` is `null` while a step is still `pending`; it may also be `null` for
a downstream `stopped` step that never acquired a durable call. A cancelled
mutable call whose outcome is ambiguous remains `effect_unknown`, even though
the outer run is terminal. In that case only, the outer projection retains
`pause_reason: effect_unknown` and the existing `pending` recovery handle while
`state` remains `cancelled`. Operators may `record_result` or `fail`; retry and
Loop re-entry remain forbidden. Resolving the ambiguity removes the handle but
does not change the cancelled run into a resumable or completed run.

Ordinary applications retain the exact base shape above. History list items do
not include `workflow`. A server that does not have an old Workflow definition
loaded can still return its core cold status; it never guesses a newer
definition. If the matching loaded Workflow fails definition/transcript/call
integrity validation, the request fails closed instead of silently omitting the
extension. The core always constructs `run_id`, `app_id`, state, revision,
cursor, pending Tool, and final-message fields first. A Harness can contribute
only the exact `workflow` namespace through the bounded extension protocol; it
cannot replace core fields. The core validates the exact Workflow v1 field and
step allowlists, bounded types, enums, call bindings, uniqueness, and cursor;
client validation is defense in depth, not the disclosure boundary. A
malformed, mismatched, oversized, or throwing extension returns the stable
`projection_integrity_failed` error without including Harness exception prose.

`completed` means the Loop's final answer and event were durably committed. It is not a claim that an external business goal succeeded.

After approval or non-cancelled manual effect resolution, the projection is
paused with `pause_reason: "resume_required"`. The durable internal status is
`awaiting_resume`; only the explicit resume endpoint re-enters the same Loop.
Cancelled effect resolution is the exception described above: it clears the
recovery handle while state remains `cancelled`, with no Loop re-entry.

## Endpoints

### `GET /v1/apps`

Returns schema-versioned application metadata for the first-party catalog
entries, currently Incident, Research, Developer, and Incident Mechanism
Workflow. Each item contains fixed worker and skill metadata, runtime
availability, actual loaded tool schemas/effects/revisions, and plugin
permission disclosure. Configuration affects availability, not whether a
catalog entry is returned. A disabled or failed entry is `unavailable` with a
bounded `reason_code`; catalog data never includes raw exceptions, API keys,
environment values, system prompts, MCP snapshots, or internal paths.

`requested_permissions` is disclosure. For `trusted_process` code the response
also states `effective_access: "FULL HOST PROCESS PRIVILEGES"` and
`enforced: false`; the server does not claim a plugin sandbox.

### `POST /v1/workflows/preflight` (Hosted-verified W1.2 static preflight)

This read-only endpoint accepts the complete strict Workflow definition as the
request body. There is no outer `{definition: ...}` envelope and no Tool/app
selector supplied by the client:

```json
{
  "schema_version": 1,
  "workflow_id": "studio-inspect-incident",
  "version": "1",
  "execution": "single-harness-ordered-tools-v1",
  "inputs": [
    {"key":"summary","type":"string","required":true,"max_bytes":65536}
  ],
  "steps": [
    {
      "step_id": "inspect",
      "kind": "tool",
      "tool_name": "inspect_incident",
      "effect": "read_only",
      "tool_revision": null,
      "schema_sha256": "5aea8fcade49abc2d6e5addfc31eb8734b32266f70241943356f7e0b6cade7be",
      "arguments": {"summary":{"kind":"input","key":"summary"}},
      "result": {"type":"string","max_bytes":65536}
    }
  ],
  "output_step": "inspect"
}
```

The normal `/v1/*` security and transport boundary applies before Workflow
parsing: one bearer credential when authentication is enabled, exact
same-origin or an explicitly configured CORS origin, one `Content-Length`, no
chunked transfer, UTF-8 `application/json`, no query parameters, and a 1 MiB
raw-body ceiling. Duplicate keys, a byte-order mark, non-finite numbers,
malformed/deep JSON, unknown fields, dynamic entry points, expressions, and
other schema drift fail closed.

On success, the exact top-level response keys are:

```json
{"ok":true,"schema_version":1,"manifest":{}}
```

`manifest` is the complete detached W1.1 manifest documented in
[WORKFLOWS.md](WORKFLOWS.md#static-compiled-manifest-preflight-w11), not an
abbreviated runtime view. Its exact fields cover definition identity, derived
application identity, serial-only capability flags, trust/no-sandbox,
redacted inputs, ordered dependencies, logical and dispatch Tool contracts,
approval, recovery policy, and result bounds. Literal values are replaced by
type, canonical byte count, and SHA-256. A caller may mutate its received JSON
without changing a later response or any server-owned object.

The server freezes the preflight Tool registry once at successful startup from
Tools belonging to explicitly configured, successfully loaded ordinary
`Harness` applications. Compiled `WorkflowHarness` wrappers and unavailable
applications contribute nothing. If a Tool name appears in more than one
ordinary Harness, every occurrence is excluded as ambiguous. Registry order is
deterministic. `/v1/apps` metadata may help the editor suggest a draft, but it
never decides preflight acceptance; the frozen server-owned `Tool` objects do.
A Workflow-only deployment therefore has an empty Studio registry and rejects
all Tool steps. It does not introspect compiled wrapper internals to recover
source Tools.

Controlled definition/compiler failures return `422`:

```json
{
  "ok": false,
  "error": {
    "code": "workflow_preflight_rejected",
    "message": "bounded Sasori-controlled validation text",
    "retryable": false,
    "reason_code": "invalid_definition"
  }
}
```

`reason_code` is exactly one of:

| Reason | Boundary |
|---|---|
| `invalid_definition` | the strict Workflow definition was rejected |
| `tool_contract_mismatch` | the frozen Tool set or shared compiler rejected effect, revision, schema, signature, binding, or wrapper identity |
| `manifest_rejected` | the bounded detached manifest could not be composed |

Transport/authentication errors retain their existing adapter codes. A
five-second owner timeout returns retryable `503 runtime_busy` with
`Retry-After: 1`; it is not success. Browser `AbortController` cancellation
abandons the response but does not prove that bounded synchronous server work
was forcibly stopped.

The bundled Studio presents only an exact `422 workflow_preflight_rejected`
envelope as the authoritative `REJECTED` state. Authentication, authorization,
timeout, shutdown, network, non-JSON, malformed-success, and client-validation
failures remain `UNVERIFIED` with `NO SERVER VERDICT`. The `retryable` field is
displayed as server guidance; the Studio never retries a draft automatically.

Successful and rejected preflight requests do not acquire the runtime mutation
gate, construct a Harness/Store, call a model/provider/Tool/idempotency hook,
or create/change any run, call, message, event, checkpoint, approval, recovery,
artifact, catalog, or execution identity. This endpoint itself has no save,
activate, deploy, schedule, or run-from-draft operation. The W1.2 Studio kept
only transient page text; W1.3 saving uses the separate conditional catalog API
below. This bounded preflight endpoint is Hosted-verified at
[`e3bc816`](https://github.com/syusama/sasori/commit/e3bc816c9d33febcc364e595a7480b475d181efb)
in [run 31391700342](https://github.com/syusama/sasori/actions/runs/31391700342).
See [ADR-0016](ADR-0016-STATIC-SERIAL-WORKFLOW-STUDIO.md) for its exact
acceptance evidence and non-goals.

### Durable saved Workflow catalog (Hosted-verified W1.3)

W1.3 adds a deployment-owner authoring catalog outside core. It persists strict
static serial Workflow definitions and the detached manifest accepted at save
time. It does not activate, publish, compile, schedule, or execute them. Saved
records do not enter `/v1/apps`, and `/v1/runs` still accepts only an
application explicitly configured by the deployer.

Four identities remain separate:

| Field | Authority |
|---|---|
| `catalog_id` | client-generated persistence identity: `wfcat_` plus the 32 lowercase hexadecimal digits of an RFC 4122 version-4 UUID |
| `catalog_revision` | server-managed positive immutable revision and CAS generation |
| `definition.workflow_id` / `definition.version` | author-controlled logical identity and version inside the strict definition |
| `definition_sha256` | canonical definition content digest |

The catalog uses a mutable head plus append-only revision snapshots in its own
SQLite file. Definition and manifest bytes are canonical UTF-8 JSON. Reads
verify their digests and the head/revision relationship; corruption fails
closed. File locking, `synchronous=FULL`, WAL, and atomic transactions establish
single-machine crash recovery, not distributed consensus or tamper-proof
provenance. Literal bindings are stored in plaintext as part of the definition.

Startup validates the exact version-1 table, constraint, immutable-trigger, and
composite-foreign-key contract, requires `PRAGMA foreign_keys=1`, and rejects a
non-empty `PRAGMA foreign_key_check`. Current detail, historical detail, and
list use the same head validator. It verifies the current snapshot binding and
a contiguous revision chain from 1 through the head. Stored scalar violations
are catalog integrity failures; malformed caller paths, queries, preconditions,
and bodies remain ordinary request errors.

#### `GET /v1/workflows?limit=<1..100>&before=<catalog_seq>`

Returns stable descending head summaries. `limit` defaults to `50`; use the
returned `next_before` value for the next page. List items deliberately omit
the full definition and manifest:

```json
{
  "ok": true,
  "schema_version": 1,
  "items": [
    {
      "catalog_id": "wfcat_1234567812344abc8def1234567890ab",
      "catalog_revision": 2,
      "workflow_id": "studio-inspect-incident",
      "definition_version": "2",
      "definition_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    }
  ],
  "next_before": null
}
```

Unknown, repeated, empty, non-integer, or out-of-range query values return
`422 invalid_request` without accessing a record.
The server validates the extra `limit + 1` sentinel before returning a non-null
cursor. The Workbench follows only a positive descending `next_before`, loads
older pages on explicit request, and deduplicates identities across pages.

#### `GET /v1/workflows/{catalog_id}`

Returns the current head. Add one positive integer `revision` query to read an
exact immutable historical snapshot:

```http
GET /v1/workflows/wfcat_1234567812344abc8def1234567890ab?revision=1
```

The exact response envelope is:

```json
{
  "ok": true,
  "schema_version": 1,
  "record": {
    "catalog_id": "wfcat_1234567812344abc8def1234567890ab",
    "catalog_revision": 1,
    "parent_revision": null,
    "definition_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "definition": {},
    "saved_manifest": {},
    "head_revision": 2,
    "is_head": false,
    "current_contract": {
      "status": "compatible",
      "reason_code": null
    }
  }
}
```

`saved_manifest` is immutable save-time evidence. `current_contract` is a
zero-execution read-time comparison against the current process's
startup-frozen Tool registry. Its status is `compatible` or `incompatible`;
the bounded incompatible reason is `tool_contract_mismatch` or
`manifest_rejected`. Tool drift never rewrites the saved definition, manifest,
revision, or run authority. Detail responses include the exact strong `ETag`
for the returned revision and `Cache-Control: private, no-store`.

#### `PUT /v1/workflows/{catalog_id}`

The body is the complete strict Workflow definition itself. There is no outer
wrapper and the client cannot supply a digest, manifest, revision, owner, or
activation state.

Create requires exactly:

```http
PUT /v1/workflows/wfcat_1234567812344abc8def1234567890ab
If-None-Match: *
Content-Type: application/json

<exact Workflow definition>
```

A successful create returns `201`, the detail envelope, `Location`, and a
strong ETag:

```text
"sasori-wfcat-1234567812344abc8def1234567890ab-r1-<definition_sha256>"
```

Update uses the same route and body with exactly one current strong
`If-Match`. The ETag binds catalog identity, revision, and definition digest.
CAS is checked inside the transaction that inserts the immutable snapshot and
moves the head. Two writers with the same ETag can therefore create at most one
new revision. A stale writer returns `412`, even when its submitted definition
happens to equal the current head. A current writer submitting the exact same
canonical definition and manifest receives `200` with the unchanged revision
and ETag; no false history entry is appended.

Create/update runs the same strict JSON codec and startup-frozen Tool preflight
as the read-only endpoint before mutation. It does not call a model, Tool,
wrapper, idempotency hook, network, subprocess, or runtime run/checkpoint/event/
approval/effect/artifact authority. Successful saving is authoring evidence,
not permission to execute trusted Python.

Conditional headers are fail closed. Missing create/update preconditions return
`428 workflow_catalog_precondition_required`. Repeated, weak, list-valued,
malformed, mutually exclusive, non-star create, or wildcard update conditions
return `422 invalid_request`. Other stable catalog errors are:

| HTTP | Code | Meaning |
|---|---|---|
| `404` | `workflow_catalog_not_found` | catalog identity or requested revision does not exist |
| `412` | `workflow_catalog_revision_mismatch` | create/update condition does not match durable state |
| `422` | `workflow_preflight_rejected` | strict definition or current Tool contract was rejected |
| `503` | `workflow_catalog_integrity_failed` | stored bytes or head/revision relationship failed validation |
| `503` | `workflow_catalog_store_unavailable` | bounded catalog storage/configuration failure |
| `504` | `workflow_catalog_outcome_unknown` | a conditional PUT was submitted but no authoritative mutation result was obtained; reconcile by GET and never automatically repeat PUT |

Authentication and exact-Origin checks occur before body read, JSON parsing,
preflight, or catalog access. The bearer has no user/tenant subject, so this is
one deployment-wide catalog, not row-level ownership, RBAC, or multi-tenant
isolation.

After the owner accepts a saved-Workflow PUT, an owner wait timeout has this
exact public response:

```http
HTTP/1.1 504 Gateway Timeout
Cache-Control: private, no-store
Content-Type: application/json; charset=utf-8
```

```json
{
  "ok": false,
  "error": {
    "code": "workflow_catalog_outcome_unknown",
    "message": "saved Workflow mutation outcome is unknown; reconcile with a read-only GET",
    "retryable": false,
    "catalog_id": "wfcat_1234567812344abc8def1234567890ab"
  }
}
```

The response has no `Retry-After`, `ETag`, or `Location`, and does not claim
that SQLite committed or rolled back. It is distinct from preflight/admission
`503 runtime_busy`, which remains retryable and may carry `Retry-After: 1`
because no ambiguous catalog mutation is being repeated.

A client timeout, disconnect, abort, shutdown, non-JSON response, or malformed
success is also treated locally as `OUTCOME UNKNOWN`. The client may issue one
read-only GET for the already-known `catalog_id` but never automatically repeats
PUT. A 200 whose canonical definition matches proves that the desired definition
is now the durable head, not that this exact HTTP request committed. A differing
head is a conflict. Exact not-found after a create establishes current absence;
any later create remains an explicit operator action. GET transport, malformed,
storage, or integrity failure leaves the outcome unresolved. Exact `412` is a
separate authoritative conflict: preserve the local draft, read the current
head, and require an explicit choice rather than automatic merge or overwrite.

W1.3 does not provide delete, restore, purge, metadata mutation, import/export,
sharing, activation, run-from-saved, DAG/parallel execution, Agent nodes,
subflows, encryption at rest, secure erase, signed provenance, or sandboxing.
See [ADR-0017](ADR-0017-DURABLE-SAVED-WORKFLOW-CATALOG.md). This implementation
is bound to
[`a3c4870`](https://github.com/syusama/sasori/commit/a3c48709ffbbdec5edc8f9ec420e63fe80635cc7)
and [Hosted run 31468469213](https://github.com/syusama/sasori/actions/runs/31468469213).
The exact revision passed deterministic, package, real-Chrome,
mainland-source-container, restart, CAS, crash-point, and no-execution gates.
That was an ordinary `main` run whose exact-tag-only job was skipped; it did not
create a tag, TestPyPI/PyPI round trip, signed provenance, or formal release.

### `GET /v1/runs`

`GET /v1/runs?limit=50&before=219&app_id=developer` returns cursor-paginated
history. `limit` is `1..100`; `before` and `app_id` are optional. Items contain
only:

```json
{
  "cursor": 218,
  "run_id": "dev-42",
  "app_id": "developer",
  "state": "paused",
  "pause_reason": "approval_required",
  "step": 3,
  "latest_seq": 11,
  "input_preview": "modify README and inspect Git state",
  "final_preview": null,
  "pending": {"tool_name":"write_text","effect":"side_effecting"}
}
```

History does not expose full input/final content, pending arguments,
fingerprints, idempotency keys, tool output, checkpoint/history/provider state,
system prompts, MCP configuration, or filesystem paths. Fetch run detail and
events after selecting an item.

### `POST /v1/runs`

```json
{"run_id":"optional-valid-id","app_id":"research","input":"one user prompt"}
```

Returns `200` completed or `202` paused. `app_id` is required when multiple
applications are configured and is atomically bound to the new run. An unknown
ID returns `404 app_not_found`; a configured application whose factory failed
returns `409 app_unavailable`. Clients cannot submit assistant/tool messages,
worker IDs, skill/tool subsets, or Python factories through this endpoint.

### `POST /v1/runs/{run_id}/resume`

Body: `{}`. The server loads the run's immutable application binding and calls
that Harness's normal `resume()`. Returns `200` completed or `202` paused.
Failed/cancelled runs and invalid transitions return `409`/`422`; resume never
occurs automatically after an approval or effect decision. A legacy unbound run
may use the only configured application, but a multi-application server returns
`409 app_binding_missing` rather than guessing.

### `GET /v1/runs/{run_id}`

Returns the durable projection, including `app_id` and the initial user input,
without driving the model. A matching loaded Workflow also returns the optional
redacted `workflow` extension described above. Private provider/checkpoint state
is excluded.

### `POST /v1/runs/{run_id}/approval`

```json
{"fingerprint":"exact immutable call fingerprint","approved":true}
```

The same decision is idempotent; a conflicting/stale decision returns `409`.
This endpoint records the decision only and returns a paused
`resume_required` projection.

### `POST /v1/runs/{run_id}/effect`

```json
{
  "fingerprint":"exact effect-unknown call fingerprint",
  "action":"record_result",
  "reason":"operator verified receipt R-7",
  "result":{"receipt":"R-7"}
}
```

Actions are `record_result`, `fail`, or `retry`. A non-empty audit reason is
mandatory. A non-cancelled resolution records the decision and returns
`resume_required`; it does not drive the Loop. A cancelled run cannot retry;
`record_result` or `fail` clears its recovery handle while the run remains
`cancelled`, and it cannot re-enter the Loop.

### `GET /v1/runs/{run_id}/events?after_seq=N`

JSON mode returns only durable events with `seq > N`. A negative/non-integer/conflicting cursor is rejected; a cursor ahead of durable history returns `409` rather than waiting forever.

With `Accept: text/event-stream`, the response contains:

```text
retry: 1000

id: 43
event: tool.started
data: {"seq":43,"event":{"type":"tool.started",...}}

```

Reconnect with either `Last-Event-ID` or `after_seq`; if both are present they must match. Events are read from `stored_events()`, not the best-effort observer. Consumers deduplicate by `(run_id, seq)`. Keepalive comments are not durable events. The stream closes after a caught-up terminal event or its bounded connection lifetime; no synthetic completion event is invented.

### `GET /v1/runs/{run_id}/artifacts`

Returns immutable references registered against the exact run:

```json
{
  "run_id": "run-123",
  "artifacts": [{
    "version": 1,
    "artifact_id": "artifact-8cbb...",
    "run_id": "run-123",
    "content_sha256": "64 lowercase hexadecimal characters",
    "size_bytes": 19427,
    "filename": "report.txt",
    "media_type": "text/plain; charset=utf-8",
    "created_seq": 17
  }]
}
```

The response excludes storage keys, absolute paths, temporary or signed URLs,
caller-declared MIME, provider state, and mutable preview policy. The current
Bearer authenticates the Sasori instance; exact run association is enforced,
but this is not user or tenant isolation.

### `GET|HEAD /v1/runs/{run_id}/artifacts/{artifact_id}/content`

The server verifies the complete opened file against the reference's exact
size and SHA-256 before sending success headers. Unknown and cross-run artifact
IDs have the same `404 artifact_not_found`; missing or modified bytes return
`503 artifact_integrity_failed` without partial content.

Content is always `attachment` with RFC 5987 filename metadata,
`Cache-Control: private, no-store`, `X-Content-Type-Options: nosniff`, a digest
ETag, and `X-Sasori-Content-SHA256`. One byte range is supported. Repeated or
multi-range headers, invalid units, zero-byte ranges, and unsatisfiable ranges
return `416` plus `Content-Range: bytes */<size>`. Query parameters and global
artifact/digest routes are not supported. See [ARTIFACTS.md](ARTIFACTS.md) and
[ADR-0010](ADR-0010-ARTIFACT-REF-BOUNDARY.md).

## Workbench static resources

`GET /` serves the bundled Workbench. Only an exact allowlist of versioned
resources under `/assets/` is served; there is no directory listing, arbitrary
file read, server template injection, or SPA fallback. `/` uses `no-cache`;
assets use immutable caching. CSP disallows inline script/style, object/embed,
foreign framing, and non-self connections. The page stores an optional bearer
token in `sessionStorage` only and uses authenticated `fetch()` plus a streaming
SSE parser because native `EventSource` cannot attach `Authorization`.

Live SSE, cold JSON loading, and reconnect feed one pure reducer scoped by
`run_id`. Only the next contiguous sequence advances its reconnect cursor; an
identical `(run_id, seq)` is an idempotent no-op, while a gap, conflicting
duplicate, invalid version-1 envelope, or mismatched SSE `id`/`event` fails
closed. A run-selection epoch prevents an older status, history, stream, or
mutation response from replacing the currently selected view. Aborting that
view cancels local waiting only; it is not a claim that an already accepted
server operation stopped. See [ADR-0008](ADR-0008-WORKBENCH-EVENT-REDUCER.md).

The immutable `workflow-studio.0.2.0.css`/`.js` layer adds the W1.3 saved
authoring rail without changing that run reducer. Browser state keeps selected
`catalog_id`, loaded strong ETag, edit epoch, selection epoch, captured draft,
request identity, and recovery context per `catalog_id`. Selection epochs decide
whether a result may render; switching records does not delete another record's
recovery context. Late PUT success, late outcome-unknown reconciliation, and
late detail GET can update only their original record's rail/recovery state,
never the newly selected editor, ETag, label, or draft. Exact `412` becomes
`CONFLICT` and preserves the local draft; the operator must explicitly adopt
the refreshed server head. Exact 504, transport, or malformed-success ambiguity
becomes `OUTCOME UNKNOWN`; only a read-only GET of the already-known identity is
automatic, never another PUT.

List pagination uses the stable descending cursor and cross-page identity
deduplication. Before a detail is loaded, the browser recomputes SHA-256 over
the canonical definition bytes and binds the result to the manifest, record
digest, and exact strong ETag. The browser uses no `localStorage` or IndexedDB
authority, and the saved rail exposes no Run, Activate, Publish, Deploy, or
Schedule action.

Tool/provider text is untrusted and rendered as text, never executable HTML.
Artifact cards come only from the run-scoped artifact endpoint. UTF-8 text/JSON
preview uses authenticated fetch and `textContent`; downloads use a short-lived
Blob URL, never a bearer token in a link. The Workbench does not invent plan,
cancel, multi-agent, sandbox, or marketplace-install behavior that the runtime
lacks.

### Health

- `GET /healthz`: HTTP process is alive.
- `GET /readyz`: the owner loop, app factories, run database, Workflow catalog
  database, and both exclusive owner locks are ready.

Runtime busy does not make readiness fail. Health/readiness are not proof that a real Agent workflow passed.

## Request and deployment boundary

- JSON POST/PUT bodies require `Content-Length`, UTF-8 `application/json`,
  unique object keys, finite numbers, and at most 1 MiB. Chunked and unsupported
  bodies fail closed. Saved Workflow PUT also requires one exact non-repeated
  `If-None-Match` or `If-Match` condition as described above.
- Access logging is suppressed so bearer tokens, prompts, tool arguments, and manual results do not enter ordinary logs.
- A request disconnect does not mean user cancellation; there is no public cancel endpoint in this slice.
- The server does not implement TLS, accounts, tenants, cookies, uploads, rate-limit policy, general background jobs, multiple workers, replicas, leases, failover, artifact deletion/GC, or network-filesystem guarantees. The opt-in final-artifact host policy performs only deterministic idempotent startup reconciliation for its own output.
- Use one process with separate local run and Workflow-catalog databases. CLI
  or second-server operations against either running file correctly fail its
  owner lock; operational clients should use HTTP. The deployment bearer is a
  deployment-wide admission credential, not a user/tenant subject, so every
  admitted caller can read and modify the saved authoring catalog.
