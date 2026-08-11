# Typed ordered Workflows

Sasori Workflow is a core-external adapter for finite, serial, ordered Tool
steps. It runs on the same Harness as an ordinary Agent turn, so approval,
effects, idempotency, cancellation, events, checkpoint recovery, CLI, and HTTP
do not fork into a second runtime.

It is deliberately not a general DAG or distributed Workflow Engine. Read
[ADR-0013](ADR-0013-TYPED-WORKFLOW-BOUNDARY.md) for the exact trust, durability,
and non-goal boundary.

## Thirty-second definition

```python
from sasori import Harness, SQLiteStore, Tool
from sasori_flow import (
    InputRef,
    InputSlot,
    StepRef,
    ToolStep,
    WorkflowSpec,
    compile_workflow,
)


def inspect(summary: str) -> str:
    return f"diagnostic:{summary}"


def record(summary: str) -> str:
    # A real side effect belongs here.
    return summary


inspect_tool = Tool("inspect", inspect, effect="read_only")
record_tool = Tool(
    "record",
    record,
    effect="side_effecting",
    tool_revision="1",
)

spec = WorkflowSpec(
    workflow_id="incident-response",
    version="1",
    inputs=(InputSlot("incident", "string"),),
    steps=(
        ToolStep.from_tool(
            "inspect",
            inspect_tool,
            {"summary": InputRef("incident")},
            result_type="string",
        ),
        ToolStep.from_tool(
            "record",
            record_tool,
            {"summary": StepRef("inspect")},
            result_type="string",
        ),
    ),
    output_step="record",
)


class UnusedModel:
    async def complete(self, messages, tools):
        raise AssertionError("compile_workflow replaces the base model")


store = SQLiteStore("runs.sqlite3")
base = Harness(UnusedModel(), (inspect_tool, record_tool), store=store)
workflow = compile_workflow(spec, base)
```

`compile_workflow()` returns a real `Harness` subclass bound to the supplied
core store. The base model never runs; a deterministic, stateless WorkflowModel
projects one complete wrapper ToolCall per model step.

## Lower-boilerplate Python authoring

`SerialWorkflowBuilder` is a definition-time convenience layer. It returns the
same `InputRef`, `StepRef`, `ToolStep`, and final `WorkflowSpec` used above:

```python
from sasori_flow import SerialWorkflowBuilder

flow = SerialWorkflowBuilder("incident-response", version="1")
incident = flow.input("incident", value_type="string")
inspection = flow.step(
    "inspect",
    inspect_tool,
    arguments={"summary": incident},
    result_type="string",
)
recorded = flow.step(
    "record",
    record_tool,
    arguments={"summary": inspection},
    result_type="string",
)
spec = flow.build(output=recorded)
```

Declaration order is execution order. The builder freezes after `build()` and
owns no runtime or durable state. For an equivalent manual definition,
`as_data()`, digest, derived application ID, wrapper Tool identity, and recovery
behavior are identical.

## Strict JSON definition codec

The existing canonical schema-version-1 data is also its own portable static
document format:

```python
from sasori_flow import workflow_spec_from_json

spec = workflow_spec_from_json(document_bytes)
workflow = compile_workflow(spec, base)
```

`workflow_spec_from_data()` accepts already-decoded JSON data.
`workflow_spec_from_json()` accepts UTF-8 `str` or `bytes` and rejects duplicate
keys, unknown or missing fields, NaN/Infinity, malformed UTF-8, byte-order
marks, invalid Unicode, excessive depth/bytes/counts, invalid references, and
unsupported schema/execution markers. Parsing never imports Python, loads a
handler, creates a run, calls a model, or invokes a Tool.

The document binds Tool name, effect, revision, and schema digest. Compilation
still requires an explicitly supplied trusted base Harness and fails before run
creation if its installed Tool registry has drifted. Parsing the static JSON
does not execute code. Compiling and running an accepted definition is
authorization-sensitive and invokes explicitly supplied trusted installed
Tools; those Python handlers are not sandboxed plugins.

## Static compiled manifest preflight (W1.1)

`preflight_workflow()` validates the definition against the same trusted Tool
registry and wrapper compiler used by `compile_workflow()`, then returns a
detached deterministic manifest without constructing a Harness or Store:

```python
from sasori_flow import preflight_workflow

manifest = preflight_workflow(spec, (inspect_tool, record_tool))
assert manifest["definition_sha256"] == spec.digest
assert manifest["steps"][1]["requires_approval"] is True
assert manifest["steps"][1]["recovery_policy"] == (
    "manual_effect_resolution_on_ambiguity"
)
```

The manifest exposes immutable inputs, dependencies, logical/dispatch Tool
contracts, result bounds, approval points, and effect-derived recovery policy.
Literal bindings expose only JSON type, canonical byte count, and SHA-256; the
literal value is absent. Runtime inputs, outputs, call IDs, approval
fingerprints, idempotency keys, recovery reasons, and mutable state are absent.

Generating it does not call a model or Tool handler, create a run/checkpoint,
or emit an event. It imports no handler named by JSON. The supplied `Tool`
objects are trusted installed Python contracts, not sandboxed or security
scanned code. A compiled `WorkflowHarness.definition_manifest()` returns the
same semantics, and the first-party app catalog reuses this composer instead of
re-deriving its own step contract.

The W1.1/W1.2 Workbench definition preview exact-validates that application
catalog manifest through an immutable extension while preserving the existing
run projection and event reducer. It displays dependencies, approval points,
recovery policy, and the trusted-Python/no-sandbox boundary. That historical
preview did not execute or persist a Workflow definition; W1.3 persistence is a
separate server-owned catalog described below.

This W1.1 boundary is Hosted-verified at
[`709200b`](https://github.com/syusama/sasori/commit/709200b8d6e4521245109852be54170c09fb0da4)
in [run 31375975778](https://github.com/syusama/sasori/actions/runs/31375975778):
all 20 non-tag jobs passed and the exact-tag-only bundle was correctly skipped.
It is not a Workflow Studio, saved user catalog, visual editor, branch/parallel
executor, Agent node, subflow system, sandbox, signed provenance, or
production-readiness claim. See
[ADR-0015](ADR-0015-STATIC-WORKFLOW-MANIFEST-PREFLIGHT.md).

## Transient Static Serial Workflow Studio preflight (W1.2)

The Hosted-verified W1.2 implementation adds one browser authoring surface over
the same W1/W1.1 contracts:

```text
transient strict JSON text
    -> POST /v1/workflows/preflight
    -> workflow_spec_from_data()
    -> preflight_workflow(spec, frozen_host_tools)
    -> exact detached manifest
```

The request body is the definition itself, so the HTTP adapter owns the one
strict JSON parse and the existing 1 MiB transport limit remains aligned with
the Workflow definition limit. The browser sends its exact textarea bytes; it
does not parse and stringify the draft into a relaxed second representation.
Duplicate keys, unknown fields, dynamic code/import fields, malformed Unicode,
non-finite numbers, size/depth/count drift, and compiler contract drift fail
closed.

The server freezes one Studio Tool tuple during startup. It collects Tools from
successfully loaded ordinary Harnesses only, excludes compiled
`WorkflowHarness` wrapper Tools and unavailable applications, removes every
ambiguous Tool name instead of choosing a winner, sorts the remaining names,
and never accepts a preflight eligibility decision from the request or browser
catalog. A
Workflow-only deployment intentionally freezes an empty tuple and rejects each
Tool step. Deployers that want Studio suggestions must also configure the
ordinary source-Tool Harnesses they trust; this slice does not reach through
private compiled-step state.

Preflight reuses the existing Workflow codec and shared compiler. It does not
construct a Harness or Store, call a model/provider/Tool handler/idempotency
callback, take the runtime mutation gate, or create/change a run, call,
checkpoint, message, event, approval, recovery, artifact, or catalog record.
It may complete concurrently while an ordinary run waits at a Tool boundary,
but that is read-only inspection—not parallel Workflow execution. Client abort
means only that the response was abandoned; it is not forced cancellation of
synchronous server work.

The W1.2 `workflow-studio.0.1.0` layer calls the endpoint with the existing
bearer/same-origin client
boundary and hands a successful response to the existing exact manifest
consumer. It renders response and draft content with text nodes, keeps no
definition in `localStorage`, and offers no save, activation, deployment, or
run control. Every editor input mutation:

1. increments an edit epoch;
2. clears the previous success and manifest immediately;
3. aborts or logically abandons the pending request;
4. permits a response to render only if request identity, captured epoch,
   captured exact text, and non-aborted signal still match.

Before fetch, the editor also requires its `TextEncoder` bytes to round-trip
through a fatal UTF-8 `TextDecoder` to the exact same JavaScript string. An
unpaired surrogate therefore displays `INVALID UNICODE`, disables preflight,
clears any prior verdict, and sends no replacement-character payload.

Only an exact `422 workflow_preflight_rejected` response becomes the
authoritative `REJECTED` state. A `401`, `403`, `503`, network failure,
non-JSON response, malformed success envelope, or client-side validation
failure remains `UNVERIFIED` with `NO SERVER VERDICT`. Retryability is shown to
the operator and never triggers an automatic retry.

Consequently, submit A then edit B without resubmitting cannot display A's
manifest next to B. The visible boundary remains `DRAFT ONLY`, `NO EXECUTION`,
`TRUSTED PYTHON`, and `NO SANDBOX`.

This W1.2 boundary is Hosted-verified at
[`e3bc816`](https://github.com/syusama/sasori/commit/e3bc816c9d33febcc364e595a7480b475d181efb)
in [run 31391700342](https://github.com/syusama/sasori/actions/runs/31391700342):
all 20 non-tag jobs passed and the exact-tag-only bundle was correctly skipped.
It does not add a saved Workflow catalog, durable draft, activation,
run-from-draft, visual DAG, branch, parallel set, Agent node, subflow,
marketplace, sandbox, second reducer/runtime/checkpoint, exactly-once execution,
or production-readiness claim. See
[ADR-0016](ADR-0016-STATIC-SERIAL-WORKFLOW-STUDIO.md).

## Durable saved static-serial catalog (W1.3 local candidate)

W1.3 adds durable authoring without adding another execution runtime:

```text
strict Workflow definition
    -> startup-frozen Tool preflight
    -> canonical definition + detached manifest
    -> independent SQLite catalog transaction
    -> immutable revision + mutable head
```

The persistence identity is not the definition identity:

| Value | Meaning |
|---|---|
| `catalog_id` | client-known durable record identity, `wfcat_` plus a validated UUID v4 in 32 lowercase hexadecimal digits |
| `catalog_revision` | server-managed positive immutable history revision and strong-ETag CAS generation |
| `definition.workflow_id` | logical Workflow identity authored inside the strict definition |
| `definition.version` | author-controlled definition version string |
| `definition_sha256` | canonical definition digest, still used by the compiler and derived application identity |

One catalog head may therefore move to a snapshot whose Workflow ID, author
version, or digest differs from the previous snapshot. That movement does not
replace the old row: revision `N+1` points back to revision `N`, while an exact
historical GET remains readable. `catalog_revision` never replaces
`definition.version`, and neither grants application identity or run authority.

`WorkflowCatalogStore` lives in `sasori_flow`, not core. Its SQLite authority is
separate from the run/checkpoint/event database, uses its own process lock,
schema identity, `synchronous=FULL`, WAL, and `BEGIN IMMEDIATE` mutation. The
default sibling of `sasori.sqlite3` is `sasori.workflows.sqlite3`; deployments
may override it with `--workflow-db` or `SASORI_WORKFLOW_DB`. The resolved files
must differ. There is deliberately no cross-database transaction because saving
a definition changes no run state.

An existing catalog opens only when its exact version-1 table, constraint,
immutable-trigger, and composite-foreign-key contract matches. Startup requires
foreign keys and rejects reported FK violations. Current detail, historical
detail, and list share one head validator that checks the current snapshot
binding and the contiguous revision chain. Strict stored-scalar failures are
integrity errors, while invalid caller inputs remain request errors. The list
also validates its hidden `limit + 1` sentinel before returning the next stable
descending cursor.

Create and update accept the exact existing Workflow definition, not a relaxed
wrapper. The server performs the one strict HTTP JSON parse, reuses
`workflow_spec_from_data()` and `preflight_workflow()`, and derives both stored
canonical documents. The request cannot supply its own manifest, digest,
revision, owner, or lifecycle. Invalid JSON/Unicode, unknown fields, current
Tool drift, ambiguity, wrapper-only Tools, and unavailable Tools fail before a
catalog transaction.

Create uses `PUT /v1/workflows/{catalog_id}` with `If-None-Match: *`. Update
uses the exact strong ETag returned by GET:

```text
"sasori-wfcat-<32 hex>-r<catalog_revision>-<definition_sha256>"
```

CAS is checked in the same transaction that appends the snapshot and moves the
head. Two stale writers cannot both win. A stale writer receives `412` even if
its submitted definition now equals the current head. A current writer
submitting identical canonical definition and manifest bytes receives the same
head without a false new revision.

The immutable `saved_manifest` records the contract accepted for one saved
revision. Detail reads independently report `current_contract` against the
current process's already-frozen ordinary-Harness Tool tuple. Tool
effect/revision/schema/signature/ambiguity drift can make that verdict
`incompatible`; it cannot rewrite the saved definition, manifest, digest,
revision, or history. Definition/manifest/head corruption fails closed.

The Workbench `workflow-studio.0.2.0` layer lists saved heads and opens exact
definitions. Its save state separates selected Catalog ID, loaded ETag, edit
epoch, selection epoch, request identity, and captured text. Late responses
cannot mark a newer draft or another record as saved. Exact `412` becomes
`CONFLICT` while preserving the local draft. Network/timeout/abort/malformed
success becomes `OUTCOME UNKNOWN`. An accepted PUT whose owner result times out
returns exact `504 workflow_catalog_outcome_unknown`, `retryable=false`, the
matching Catalog ID, and no `Retry-After`, ETag, or Location. Only GET
reconciliation is automatic; the browser never repeats PUT, merges, or
overwrites automatically.

Recovery context is retained per Catalog ID. Switching cards isolates rendering
but does not discard the older record's recovery state; late PUT, reconciliation
GET, and detail GET results cannot replace the newly selected editor. A matching
GET proves the desired definition is durable, not which request committed it;
a differing head becomes conflict and a create not-found establishes current
absence. Adopting a server head or attempting another conditional PUT remains
an explicit operator action.

The rail follows the stable pagination cursor and deduplicates identities across
pages. Before loading a detail into the editor, the browser recomputes SHA-256
over canonical definition bytes and binds it to the saved manifest, response
digest, and exact strong ETag. The browser has no `localStorage` or IndexedDB
authority.

Catalog list/detail/create/update call no model, Tool handler, wrapper,
idempotency hook, subprocess, network, or run/checkpoint/event/approval/effect/
artifact authority. Saved records do not enter the executable application
catalog at `/v1/apps`, do not construct `WorkflowHarness`, and cannot be passed
to `/v1/runs`. Saving is authoring durability, not activation.

The existing bearer is only deployment-wide admission; it has no user, tenant,
workspace, or record-owner subject. Every admitted caller can read and modify
the whole saved catalog. W1.3 does not claim RBAC or tenant isolation.

This slice intentionally has no delete, restore, purge, metadata mutation,
import/export, sharing, publish, activation, deployment, scheduling,
run-from-draft, run-from-saved, DAG, branch, parallel ready set, Agent node,
subflow, encryption at rest, secure erase, signed provenance, or sandbox. See
[ADR-0017](ADR-0017-DURABLE-SAVED-WORKFLOW-CATALOG.md). The implementation is a
locally verified W1.3 candidate; Hosted evidence and promotion remain pending.

## Run, approve, then explicitly resume

```python
import asyncio

from sasori import RunPaused


async def main() -> None:
    try:
        await workflow.run(
            {"incident": "checkout latency"},
            run_id="incident-42",
        )
    except RunPaused as paused:
        assert paused.reason == "approval_required"
        request = paused.request
        assert request is not None

    # This records a decision. It does not execute the mutable Tool.
    workflow.resolve_approval("incident-42", request.fingerprint, True)

    # The explicit resume performs the approved dispatch.
    result = await workflow.resume("incident-42")
    print(result.final_message.content)


asyncio.run(main())
```

A second `resume()` on the completed run returns the same committed final and
does not replay the source Tools.

## First-party Incident Mechanism

The installed first-party example is:

```text
incident text
  -> inspect_incident  [read_only]
  -> record_action     [side_effecting; approval required]
  -> canonical typed final
```

Use the factory spec instead of hard-coding its digest-derived application ID:

```powershell
$app = "sasori_apps.workflow_incident:create_harness"

sasori --db runs.sqlite3 --app $app --json run `
  "checkout latency" --run-id incident-42

sasori --db runs.sqlite3 --app $app --json approval `
  incident-42 <fingerprint> --approve

sasori --db runs.sqlite3 --app $app --json resume incident-42
```

The first command exits `3` at `approval_required`. Approval exits `0` with
`resume_required` and still performs no action. Resume exits `0` after the one
approved action commits.

## Definition identity and upgrades

`WorkflowSpec.digest` is canonical SHA-256. The digest includes the ordered
definition and the fixed source Tool effect/revision/schema contracts. The
derived application ID includes the first twelve digest characters.

A definition change is a new application identity, even when the author version
was not changed. Keep the old definition/factory installed while its durable
runs may still need recovery. Sasori will not silently resume an old run under a
new definition.

Saved catalog revision is orthogonal. Updating a catalog head appends authoring
history; it does not install the new definition as an application, migrate an
old run, or authorize recovery under a new digest. Re-saving the exact current
definition and manifest is a catalog no-op.

The digest does not authenticate a publisher or hash handler source code. Wheel
and release provenance are separate concerns.

## Inputs

Workflow inputs are JSON objects with declared slots. Supported kinds are
`string`, `integer`, `number`, `boolean`, `object`, `array`, and `null`.

- Python callers may pass a mapping.
- A Workflow with exactly one string slot may accept a plain string or one
  ordinary user Message.
- Other CLI/HTTP text inputs must contain a JSON object.
- Unknown keys, missing required keys, wrong kinds, invalid Unicode,
  NaN/Infinity, and size violations fail before a run is created when they are
  invocation errors.

The public run projection keeps the submitted user text. The validated canonical
typed object is stored in a separate system control binding and checked during
replay.

## Step bindings

`InputRef("name")` reads a declared Workflow input.

`StepRef("step-id")` reads the typed result of an earlier declared step. Forward
references are rejected.

`Literal(value)` embeds finite JSON data in the definition and therefore in its
digest.

Step argument names must exactly match the source handler parameters. The
reserved `idempotency_key` cannot be supplied by a Workflow definition. Source
handlers must expose explicit positional-or-keyword or keyword-only parameters;
positional-only, `*args`, and `**kwargs` signatures are rejected at compile
time. W0 checks JSON kinds at Workflow inputs and step results and binds the
source Tool schema digest, but does not infer arbitrary nested JSON schemas
across edges.

## Effect and crash behavior

| Source Tool effect | Approval | Ambiguous crash behavior |
|---|---:|---|
| `read_only` | No | May be retried when the core contract permits |
| `idempotent` | Yes | Retries only with the same verified business key |
| `side_effecting` | Yes | Pauses at `effect_unknown` for an operator decision |

Approval is always separate from execution. A Workflow does not add automatic
rollback, compensation, or exactly-once semantics.

For an idempotent Tool, the Harness stores a Workflow-bound key while the source
handler receives its original business key. Workflow call IDs remain evidence
correlation only.

## Result envelope and final outcome

Every successful source result is wrapped before the core commits it:

```json
{
  "version": 1,
  "definition_sha256": "...",
  "step_id": "inspect",
  "value": "diagnostic:checkout latency",
  "value_sha256": "..."
}
```

The final assistant content is canonical JSON:

```json
{
  "version": 1,
  "workflow_id": "incident-response",
  "workflow_version": "1",
  "definition_sha256": "...",
  "status": "succeeded",
  "output": {
    "step_id": "record",
    "value": "diagnostic:checkout latency",
    "value_sha256": "..."
  }
}
```

Identity, exact shape, canonical encoding, JSON kind, bytes, and value digest are
checked during durable replay.

## Manual effect recovery

An ordinary side effect that may have executed but did not commit a wrapper
result remains `effect_unknown`. An operator may choose the core recovery
actions `fail`, `retry` where allowed, or `record_result`.

If cooperative cancellation settles the outer run while a mutable call remains
ambiguous, public state stays `cancelled` but retains the bounded
`effect_unknown` recovery handle. Only `fail` and `record_result` remain
available; retry and Loop re-entry are forbidden. The bundled Workbench removes
the retry option for this terminal case and submits the same core effect
endpoint. Recording the decision resolves the call evidence without changing
the run into `completed` or a resumable state.

For `record_result`, pass the complete wrapper result envelope shown above. A
bare source value is not sufficient. `WorkflowHarness` validates the envelope
before committing recovery evidence, so an invalid digest, step, type, or value
hash leaves the original recovery state unchanged.

## Projections and events

`WorkflowHarness.projection(run_id)` remains a trusted Python diagnostic view
containing input and intermediate outputs. It is derived from the same core
transcript and tool-call records; it is not another store.

`WorkflowHarness.public_projection(run_id)` is the versioned, bounded, redacted
step contract. It exposes immutable definition/Tool identity, durable public
call IDs where a call exists, and stable machine states only:

```text
pending  requested  running  approval_required  resume_required
retryable_idempotent  effect_unknown  completed  failed  stopped
```

It excludes input, resolved arguments, literal values, intermediate output,
result envelopes, approval fingerprints, idempotency keys, recovery reasons,
provider state, and exception prose. `stopped` means the run is terminal and
the step has no durable Tool result, so the projection will not advance it. A
call may be absent or may have entered dispatch without a durable result;
`stopped` therefore does not prove non-dispatch, absence of an external effect,
or forced cancellation of a remote system or running synchronous handler.
`pending` has a `null` call ID. `stopped` may have a durable call ID or `null`.
A cancelled mutable call with an ambiguous outcome remains `effect_unknown`;
only downstream terminal steps without that ambiguity project as `stopped`.

For a loaded matching Workflow application,
the core `compose_run_projection()` first constructs the complete run response
and then accepts only `WorkflowHarness.public_projection_extension(run_id)`
with the exact top-level `workflow` namespace. The convenience
`WorkflowHarness.public_run_projection(run_id)` calls that same core composer;
a Harness cannot replace `run_id`, `app_id`, state, revision, cursor, pending,
or final-message fields. Malformed or mismatched extensions fail with the
stable, redacted `projection_integrity_failed` error. HTTP create, approval,
effect, resume, and single-run status use the same composer. CLI
mutation commands do too; `sasori --app module:factory status RUN_ID` adds the
extension after exact definition validation, while status without `--app`
remains a core-only cold read. Ordinary applications keep their prior response
shape, and run-list items never expand Workflow steps. There is no separate
Workflow-step endpoint.

The core also validates the complete Workflow v1 inner allowlist: exact object
and step keys, bounded field types, status/effect enums, SHA-256 fields, unique
call and step identities, nullable-call rules, current-step semantics, and the
exact core cursor. This validation runs before an extension enters a CLI or HTTP
response; Workbench validation is an independent fail-closed consumer check.

The application catalog at `/v1/apps` continues to expose loaded Workflow
ID/version/digest, serial-only
flags, wrapper Tool contracts, logical Tool names, and the ordered
definition-bound step mapping. The bundled Workbench uses `run.workflow` for
durable execution-rail semantics. The saved authoring catalog at
`/v1/workflows` is a different authority and never implies that a Harness is
loaded. The application catalog supplies immutable executable-definition
mapping; saved detail supplies authoring snapshots.
The existing event reducer remains the timeline/cursor authority; event arrival
triggers a coalesced single-run status refresh with at most one status GET in
flight and one pending follow-up. Nested and core cursors must match exactly;
an old run-selection epoch cannot update a newer rail. This does not create a
second Workflow reducer, client checkpoint, or durable authority.

Workflow adds no `workflow.*` events. Existing `run.*`, `model.*`, `tool.*`,
`approval.*`, and `recovery.*` records remain the durable truth.

## Hard limits

| Boundary | Limit |
|---|---:|
| Steps | 128 |
| Input slots | 128 |
| Arguments per step | 128 |
| Canonical definition | 1 MiB |
| Saved manifest | 4 MiB |
| Catalog ID | `wfcat_` plus 32 lowercase UUID-v4 hexadecimal digits |
| Catalog revision | positive signed 64-bit integer |
| Saved list page | 1-100 heads; stable `catalog_seq` cursor |
| Canonical typed input | 256 KiB |
| Public input | 256 KiB |
| One literal | 128 KiB |
| Resolved step payload | 256 KiB |
| JSON depth | 32 |
| One result | up to 1 MiB; 64 KiB default |

Wrapper Tool names are provider-compatible and source call IDs use the shared
core public bound.

## Trust and cancellation

Workflow factories and Tools are trusted installed Python code. They are not
sandboxed and retain their effective host filesystem, network, process, and
secret access.

Cancellation is cooperative. Sasori does not swallow cancellation and will not
start the next step after it, but it cannot claim an arbitrary remote system or
already-running synchronous handler stopped.

## Test checklist for a new Workflow

At minimum, add deterministic checks for:

- canonical definition identity and definition-change recovery refusal;
- input, reference, definition, payload, result, and Unicode limits;
- source Tool effect/revision/schema and argument-name drift;
- read-only execution and mutable approval/denial separation;
- approve-without-execute followed by explicit resume;
- idempotent retry with the same source business key;
- ordinary side-effect `effect_unknown` and typed manual recovery;
- cancellation without downstream dispatch;
- durable pending-call cross-record validation;
- child-process exit after dispatch, tool return, and final commit;
- Catalog ID/definition identity separation, create/update CAS, stale writer,
  identical no-op, immutable historical reads, and stable pagination;
- saved definition/manifest/head tamper refusal, current Tool drift without
  mutation, run/catalog same-path refusal, and second Catalog owner exclusion;
- catalog crash points before revision insert, after revision insert, after head
  movement, and immediately after commit;
- saved authoring leaves run/event/checkpoint/call/action authorities unchanged,
  and browser conflict/outcome-unknown flows never retry PUT automatically;
- Python, CLI, HTTP, installed wheel, rebuilt sdist, container restart, and
  browser journeys.

Real-provider smoke tests supplement these deterministic gates; they do not
replace them.

## Roadmap boundary

```text
W0  one-Harness ordered Tool proof
W1  strict static serial authoring and versioned public step projection
W1.1 static compiled manifest and zero-execution preflight
W1.2 transient browser draft and authoritative HTTP preflight preview
W1.3 durable saved static-serial catalog with immutable revisions and CAS;
     no activation or execution authority
W2  bounded Workbench step inspection from the public projection;
    existing reducer retained for timeline/cursor
W3  bounded parallel ready set
W4  branches, agent nodes, subflows, visual authoring
W5  queue, lease, fencing, scheduler, distributed execution
```

ADR-0013 decides the W0 runtime boundary. The current W1 implementation,
Hosted-verified at
[`b410ceb`](https://github.com/syusama/sasori/commit/b410cebf8633e3ea77ca187174e4f02347aea840)
in [run 31366385628](https://github.com/syusama/sasori/actions/runs/31366385628),
also implements the bounded W2 inspection slice for exact static serial
definitions through the public projection. It is not visual authoring, a DAG
editor, or a new runtime. Richer authoring and all W3-W5 phases require their
own contracts, negative architecture controls, recovery decisions, and
acceptance evidence.

The current W1.1 implementation, Hosted-verified at
[`709200b`](https://github.com/syusama/sasori/commit/709200b8d6e4521245109852be54170c09fb0da4)
in [run 31375975778](https://github.com/syusama/sasori/actions/runs/31375975778),
adds only the ADR-0015 static manifest/preflight and immutable definition-preview
boundary. It does not change the W0/W1 execution marker, digest, application
identity, wrapper identity, event trace, recovery semantics, or public run
projection.

The current W1.2 implementation, Hosted-verified at
[`e3bc816`](https://github.com/syusama/sasori/commit/e3bc816c9d33febcc364e595a7480b475d181efb)
in [run 31391700342](https://github.com/syusama/sasori/actions/runs/31391700342),
adds only a transient strict-JSON browser draft, the authenticated static
preflight endpoint over the shared compiler and startup-frozen ordinary Tool
registry, and exact detached-manifest rendering. It adds no persistence or
execution authority and changes none of the W0-W1.1 runtime, reducer,
checkpoint, event, effect, or recovery semantics.

The W1.3 implementation candidate adds only the ADR-0017 deployment-owner saved
authoring database, immutable revisions, strong-ETag CAS, historical reads,
current-Tool compatibility verdict, and the fail-closed saved Studio layer. It
does not change any W0-W1.2 execution identity, reducer, run/checkpoint/event,
approval, effect, or recovery semantics. Its local package/browser/container
evidence is not Hosted promotion evidence; an exact implementation commit and
its later documentation-promotion commit must each pass their own Hosted gates.
