# ADR-0006: Immutable application binding and explicit resume

Status: accepted for the local single-owner `0.1.0.dev0` server and Workbench.

## Context

Sasori now has three fixed first-party application compositions: Incident,
Research, and Developer. A local server may enable more than one of them, while
every run must still enter the existing `Harness.run()` / `Harness.resume()` →
`_drive()` path. Selecting an application only at request time would let a
client resume an old run with a different model/tool registry after restart.

Approval and manual effect decisions also need an honest public state. Those
operations durably record a decision but intentionally do not drive the Loop.
Reporting the run as active after the decision makes a required user action
look like background execution.

## Decision

The server freezes an explicit `app_id → Harness` mapping at startup. Every
Harness uses the same file-backed `SQLiteStore`, the same Harness implementation,
and one process-wide mutation gate. Application selection is one dictionary
lookup; it does not introduce another Loop, router Harness, queue, scheduler,
workflow engine, or event replay executor.

New HTTP runs bind the selected `app_id` in the same store transaction that
creates the run. The binding is immutable. Resume, approval, and effect routes
do not accept an application ID; they load the durable binding and select the
corresponding startup Harness.

SQLite schema version 3 adds nullable `runs.app_id`:

- a new database is created directly at version 3;
- version 1 migrates to version 2 and then version 3;
- version 2 adds `app_id` and advances to version 3;
- an existing unbound run may use the only configured application in
  single-application compatibility mode;
- an unbound run on a multi-application server fails closed with
  `409 app_binding_missing`; Sasori never guesses from a run ID, tool name,
  pending call, or catalog entry.

`GET /v1/apps` projects the fixed application/worker/skill metadata, actual
loaded tools, availability, and plugin permission disclosure. `GET /v1/runs`
returns cursor-paginated safe summaries and may filter by `app_id`. `POST
/v1/runs` accepts `app_id`; it is required when more than one application is
configured. Unknown applications return `404 app_not_found`, while configured
but failed application factories return `409 app_unavailable` with a bounded
reason code.

The public run projection includes `app_id` and the initial user input. History
summaries include only bounded previews and safe pending metadata. Neither
surface exposes full message history, `provider_state`, system prompts,
checkpoint JSON, MCP argv/environment/snapshots, plugin secret values, or
internal filesystem paths.

After approval or manual effect resolution, the durable status is
`awaiting_resume`; its public projection is `state="paused"` and
`pause_reason="resume_required"`. Only an explicit `resume` request re-enters
`_drive()`. Repeating a completed resume returns the stored terminal result and
does not repeat a committed tool result or final answer.

## Workbench consequence

The bundled Workbench consumes only the public REST/SSE contracts. Live SSE,
cold JSON event loading, and reconnect all reduce the same durable events and
deduplicate by `(run_id, seq)`. Bearer mode uses `fetch()` plus a streaming body
because `EventSource` cannot set `Authorization`. Approval/effect decisions and
resume remain separate visible actions.

The catalog's permission data is disclosure, not enforcement. An enabled
`trusted_process` application or plugin still executes with the Sasori process
and OS user's full authority. `effective_access = FULL HOST PROCESS PRIVILEGES`
and `enforced = false` must remain visible; this ADR does not create a sandbox.

## Rejected alternatives

- A second run-to-app JSON file or side table written outside run creation:
  it can diverge from the checkpoint transaction.
- Inferring the application from run IDs, tools, or pending state: ambiguous
  historical data would execute under the wrong trusted code.
- A universal Harness that routes models/tools dynamically: it creates a
  product execution path beside the readable single-agent runtime.
- Combining approval and resume in one endpoint: it erases the durable human
  boundary and makes explicit recovery harder to audit.
- A generic workflow, background queue, WebSocket, or replay executor: none is
  required for the current local single-owner product.

## Verification

Regression checks cover version 1 → 2 → 3 migration, immutable bindings,
restart, the multi-app legacy failure, application availability, history
cursor/filter behavior, non-disclosure, `awaiting_resume`, explicit resume,
and exactly one committed side effect across completed resume and restart.
Changes to this binding, recovery state, public projection, or permission
disclosure require a new decision record and updated acceptance evidence.
