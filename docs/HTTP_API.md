# Local HTTP and SSE API

Status: experimental single-node adapter. It is designed for local Workbench and black-box acceptance, not public multi-tenant SaaS.

## Start

```powershell
sasori-server --host 127.0.0.1 --port 8080 `
  --db .\runs.sqlite3 `
  --artifact-root .\artifacts `
  --app incident=sasori_apps.incident:create_harness `
  --app research=sasori_apps.research:create_harness `
  --app developer=sasori_apps.developer:create_harness `
  --trusted-loopback-no-auth
```

For any non-loopback bind, and normally for loopback too, use `SASORI_SERVER_TOKEN` or `--token-file`. All `/v1/*` requests then require `Authorization: Bearer ...`. Tokens in query strings are not supported. Exact same-origin browser requests are accepted. Cross-origin access is absent by default; each `--cors-origin` is an exact origin, never `*`, and cookies are not used.

The process owns one file-backed `SQLiteStore` and freezes one `app_id → Harness` mapping at startup. Every loaded Harness uses that store and the same Harness implementation. All store access runs on one asyncio owner thread, and one mutation gate covers all applications. Another run/resume/approval/effect request receives `503 runtime_busy` with `Retry-After: 1`; it is not described as queued. GET catalog/history/status/events can run while a model or threaded tool is awaiting.

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

`completed` means the Loop's final answer and event were durably committed. It is not a claim that an external business goal succeeded.

After approval or manual effect resolution, the projection is paused with
`pause_reason: "resume_required"`. The durable internal status is
`awaiting_resume`; only the explicit resume endpoint re-enters the same Loop.

## Endpoints

### `GET /v1/apps`

Returns schema-versioned application metadata for Incident, Research, and
Developer. Each item contains fixed worker and skill metadata, runtime
availability, actual loaded tool schemas/effects/revisions, and plugin
permission disclosure. A configured factory that did not load is
`unavailable` with a bounded `reason_code`; catalog data never includes raw
exceptions, API keys, environment values, system prompts, MCP snapshots, or
internal paths.

`requested_permissions` is disclosure. For `trusted_process` code the response
also states `effective_access: "FULL HOST PROCESS PRIVILEGES"` and
`enforced: false`; the server does not claim a plugin sandbox.

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
without driving the model. Private provider/checkpoint state is excluded.

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
mandatory. Cancelled runs cannot retry. Resolution records the decision and
returns `resume_required`; it does not drive the Loop.

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

Tool/provider text is untrusted and rendered as text, never executable HTML.
Artifact cards come only from the run-scoped artifact endpoint. UTF-8 text/JSON
preview uses authenticated fetch and `textContent`; downloads use a short-lived
Blob URL, never a bearer token in a link. The Workbench does not invent plan,
cancel, multi-agent, sandbox, or marketplace-install behavior that the runtime
lacks.

### Health

- `GET /healthz`: HTTP process is alive.
- `GET /readyz`: the owner loop, app factory, database, and exclusive owner lock are ready.

Runtime busy does not make readiness fail. Health/readiness are not proof that a real Agent workflow passed.

## Request and deployment boundary

- JSON POST bodies require `Content-Length`, UTF-8 `application/json`, unique object keys, finite numbers, and at most 1 MiB. Chunked and unsupported bodies fail closed.
- Access logging is suppressed so bearer tokens, prompts, tool arguments, and manual results do not enter ordinary logs.
- A request disconnect does not mean user cancellation; there is no public cancel endpoint in this slice.
- The server does not implement TLS, accounts, tenants, cookies, uploads, rate-limit policy, general background jobs, multiple workers, replicas, leases, failover, artifact deletion/GC, or network-filesystem guarantees. The opt-in final-artifact host policy performs only deterministic idempotent startup reconciliation for its own output.
- Use one process and one local database. CLI operations against a running server's database correctly fail the owner lock; operational clients should use HTTP.
