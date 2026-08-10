# ADR-0014: Static serial authoring and public Workflow projection

- Status: Accepted for W1 implementation
- Date: 2026-08-10
- Depends on: [ADR-0013](ADR-0013-TYPED-WORKFLOW-BOUNDARY.md)

## Context

W0 proved a definition-bound sequence of ordered Tool steps on the existing
Sasori Harness. Its `WorkflowSpec.as_data()` representation is canonical and
versioned, but it has no strict inverse codec. Authors must also assemble every
`InputSlot`, reference, `ToolStep`, and final `WorkflowSpec` manually.

`WorkflowHarness.projection()` already provides a trusted local diagnostic view
from the core transcript and call records. That view contains intermediate
values and has deliberately not been the CLI or HTTP contract. Consequently,
non-Python consumers have no bounded semantic step snapshot and the Workbench
currently derives UI labels from the existing catalog, run projection, and
event reducer.

W1 closes those two adapter gaps. It does not widen Workflow execution.

## Decision

### 1. W0 remains the only executable definition

W1 adds two authoring inputs that both produce the existing immutable
`WorkflowSpec`:

1. a strict inverse codec for the existing schema-version-1 JSON data; and
2. a thin Python `SerialWorkflowBuilder` for ordered declaration.

Neither input is a second executable graph type. `compile_workflow(spec, base)`
remains the only Workflow compiler and the resulting `WorkflowHarness` still
uses the one core Harness, SQLiteStore, event stream, and checkpoint boundary.

For every definition expressible in W0:

```text
manual spec as_data == parsed spec as_data == builder spec as_data
manual digest       == parsed digest       == builder digest
manual app_id       == parsed app_id       == builder app_id
```

The existing definition `schema_version`, execution marker, digest algorithm,
wrapper Tool identities, call identities, result envelopes, and final outcome
format do not change.

### 2. Strict definition codec

The public codec is:

```python
workflow_spec_from_data(value: object) -> WorkflowSpec
workflow_spec_from_json(document: str | bytes) -> WorkflowSpec
```

The only accepted document is the exact value returned by
`WorkflowSpec.as_data()`:

```json
{
  "schema_version": 1,
  "workflow_id": "incident-mechanism",
  "version": "1",
  "execution": "single-harness-ordered-tools-v1",
  "inputs": [],
  "steps": [],
  "output_step": "record"
}
```

Every object has an exact key set. Unknown and missing keys fail closed.
`workflow_spec_from_json()` also rejects duplicate keys, non-standard numeric
constants, malformed UTF-8, byte-order marks, invalid Unicode, excessive raw
bytes, and malformed JSON. The existing count, JSON-depth, literal, definition,
input, argument, payload, and result bounds continue to apply.

The document contains source Tool names and their effect, revision, and schema
digest bindings. It contains no handler, import, module path, script,
expression, template, environment interpolation, condition, retry policy, or
dynamic step generator. Parsing never imports code, loads a plugin, creates a
run, calls a model, or invokes a Tool.

The parsed definition is compiled only against a caller-supplied, trusted base
Harness. `compile_workflow()` continues to reject missing Tools and source Tool
effect, revision, schema, signature, or argument drift before run creation.
Installed Python factories and Tools remain trusted code, not a sandbox.

JSON is supported before YAML. YAML aliases, merge keys, implicit types, and
multiple equivalent encodings would add identity ambiguity without changing
the W1 runtime value.

### 3. Thin Python builder

The builder contract is:

```python
flow = SerialWorkflowBuilder("incident-mechanism", version="1")
incident = flow.input("incident", value_type="string")
inspection = flow.step(
    "inspect",
    inspect_tool,
    arguments={"summary": incident},
    result_type="string",
)
spec = flow.build(output=inspection)
```

`input()` returns the existing `InputRef`. `step()` immediately uses
`ToolStep.from_tool()` and returns the existing `StepRef`. Declaration order is
execution order. `build()` returns the existing `WorkflowSpec` and freezes the
builder. Duplicate declarations, invalid bindings, invalid output references,
and post-build mutation fail closed.

The builder performs no runtime work and owns no durable state.

### 4. Versioned public step projection

W1 adds a redacted, read-only Workflow projection generated from the exact
loaded `WorkflowHarness` and its existing durable records. It has this shape:

```json
{
  "schema_version": 1,
  "workflow_id": "incident-mechanism",
  "version": "1",
  "definition_sha256": "...",
  "app_id": "flow.incident-mechanism....",
  "execution": "single-harness-ordered-tools-v1",
  "output_step": "record",
  "current_step_id": "record",
  "latest_seq": 11,
  "steps": [
    {
      "position": 1,
      "step_id": "inspect",
      "kind": "tool",
      "logical_tool_name": "inspect_incident",
      "dispatch_tool_name": "wf_...",
      "effect": "read_only",
      "logical_tool_revision": null,
      "dispatch_tool_revision": null,
      "logical_schema_sha256": "...",
      "dispatch_schema_sha256": "...",
      "result_type": "string",
      "max_result_bytes": 32768,
      "call_id": "wf-...",
      "status": "completed",
      "error_code": null
    }
  ]
}
```

The projection is bounded to 256 KiB of canonical JSON and at most the existing
128 definition steps. It includes no timestamps or provider prose.

Machine step status is one of:

| Status | Durable meaning |
|---|---|
| `pending` | no durable call exists and the live run may still reach the step |
| `requested` | a complete definition-bound call is durable but not dispatched |
| `running` | the core dispatch boundary has been entered |
| `approval_required` | the mutable call is waiting for approval |
| `resume_required` | a decision is durable and explicit resume is required |
| `retryable_idempotent` | recovery may retry only with the verified same key |
| `effect_unknown` | an external side effect may have occurred |
| `completed` | a successful Tool result is durable |
| `failed` | a Tool result with a stable error code is durable |
| `stopped` | the run is terminal, this step has no durable Tool result, and the projection will not advance it |

For `stopped`, a durable call may be absent, or it may have entered dispatch
without producing a durable result. The status therefore does not prove that
the Tool was never dispatched, that no external effect occurred, or that an
arbitrary remote operation or already-running synchronous handler was forcibly
stopped. Cancellation remains cooperative.

`pending` requires `call_id: null`. `stopped` permits either a durable call ID
or `null`. All other states require a bounded durable call ID. If cancellation
settles the outer run while a mutable call is already ambiguous, that current
step remains `effect_unknown`; only downstream steps project as `stopped`.
The outer run remains `cancelled` but retains `pause_reason: effect_unknown` and
the bounded pending-call recovery handle until an operator records a result or
failure. Retry and Loop re-entry are forbidden. Resolution leaves the run
cancelled and removes the now-resolved handle. This is a recovery-evidence
decision, not a claim that cancellation stopped the external effect.

`current_step_id` is the first non-completed, non-stopped step while a run is
non-terminal. It is `null` for a terminal run.

The public projection exposes definition identity, bounded Tool contract
metadata, public call correlation, stable status, and stable error code. It does
not expose intermediate output, resolved arguments, literal values, result
envelopes, approval fingerprints, idempotency keys, recovery reasons, exception
prose, provider state, or internal transcript/control messages.

The existing `WorkflowHarness.projection()` remains a trusted local detailed
view for backward compatibility. The new public projection is a separate
contract.

### 5. One composer for Python, HTTP, and exact-app CLI

The core `compose_run_projection(store, run_id, harness)` always constructs the
existing single-run projection first. It ignores the legacy ability for a
Harness to return a complete run object and recognizes only
`public_projection_extension(run_id) -> {"workflow": {...}}`. The extension
must use schema version 1, match the core application and exact event cursor,
and remain canonical-JSON bounded to 256 KiB. A mismatch or Harness exception
becomes the fixed `projection_integrity_failed` Sasori error without private
exception prose. `WorkflowHarness.public_run_projection(run_id)` is a Python
convenience that calls this same core-owned composer.
The composer also validates the complete Workflow v1 object and step allowlists,
bounded types, enums, hashes, uniqueness, nullable-call rules, and current-step
semantics before composition. Client validation cannot substitute for this
server-side disclosure boundary.

HTTP uses that composer for Workflow create, paused create, resume, paused
resume, approval, effect recovery, and single-run status. Ordinary applications
retain their exact existing response keys. The run-list endpoint remains
lightweight and never expands all Workflow steps.

The HTTP adapter passes the already loaded trusted Harness to the core composer
and does not import `sasori_flow` from the core runtime. If a matching Workflow
Harness is loaded, integrity errors fail closed; the adapter must not silently
omit a corrupt Workflow extension. If an old Workflow factory is not installed,
core-only cold status remains readable and no newer same-name definition is
guessed.

CLI mutation commands already load an exact application and use the same
composer. `sasori status RUN_ID` remains core-only and requires no application.
`sasori --app module:factory status RUN_ID` loads and verifies the exact app and
adds the Workflow extension. A definition mismatch fails closed.

### 6. Workbench consumes the same public projection

The bundled Workbench Workflow rail uses `run.workflow` for durable step
semantics and the application catalog only for immutable definition mapping.
The existing event reducer remains the authority for timeline/cursor state;
the nested cursor must exactly equal the core cursor. Event arrival coalesces a
single-run status refresh with at most one status GET in flight and one pending
follow-up instead of deriving a second Workflow state machine. Run-selection
epoch checks prevent an old response from updating a newer rail. There is no
Workflow reducer, client checkpoint, or second durable store.

Because Workflow JavaScript is immutable-cached, the server publishes this
consumer as `workflow.0.2.0.js` and continues serving the existing
`workflow.0.1.0.js` URL without overwriting it.
The general Workbench cancelled-recovery policy is published separately as
`app.0.1.4.js`; earlier immutable application assets remain byte-stable. It
keeps `record_result` and `fail` visible for cancelled ambiguity, removes
`retry`, and never adds an automatic resume path.

## Preserved invariants

- one Harness driver and one SQLiteStore/checkpoint authority;
- no `workflow.*` event family or Workflow-specific tables;
- no second reducer, mutable graph state, scheduler, queue, lease, or worker;
- strictly serial, definition-order Tool dispatch;
- no direct handler invocation from WorkflowModel;
- approval records a decision but does not execute a Tool;
- explicit resume remains required after approval;
- idempotent retry reuses the verified business key;
- ordinary side-effect ambiguity remains `effect_unknown`;
- completed resume returns the committed final without replay;
- definition and Tool contract drift fail before dispatch;
- public events and W0 golden traces remain unchanged;
- third-party entry points remain trusted installed code, not a sandbox.

## Rejected alternatives

- a second definition schema or a JSON graph runtime;
- YAML in W1;
- dynamic imports, scripts, expressions, templates, or environment expansion;
- new Workflow execution or step endpoints;
- serializing mutable Snapshot objects;
- deriving public status separately in Python, CLI, and HTTP;
- silently omitting the Workflow extension on integrity failure;
- expanding Workflow steps in run-list responses;
- overwriting an immutable-cached Workbench asset;
- changing W0 digest or app identity for authoring convenience.

## Explicit non-goals

W1 does not add DAGs, branches, conditions, parallel ready sets, loops,
map/reduce, fan-out/fan-in, Agent nodes, multi-agent orchestration, subflows,
visual authoring, per-step retry policies, compensation, rollback, timers,
human-input nodes, queueing, leases, fencing, distributed execution,
exactly-once external effects, tenant isolation, or untrusted-code execution.

## Acceptance gates

Before W1 is promoted as shipped:

1. parsed, builder, and manual definitions have identical data, digest, app ID,
   wrapper Tool identities, and W0 golden definition digest;
2. duplicate/unknown keys, invalid encoding/numbers/Unicode, all existing
   bounds, invalid references, and Tool contract drift fail closed;
3. every public status has a deterministic regression case and the projection
   is proven not to mutate revision, generation, events, or effects;
4. public projection redaction and the 256 KiB bound are executable checks;
5. Python, HTTP, and exact-app CLI projections are semantically identical;
6. ordinary app responses, core-only CLI status, run lists, and public events
   remain compatible;
7. W0 approval, explicit resume, recovery, cancellation, crash, restart, and
   no-replay tests pass unchanged;
8. the full unit suite, installed wheel, rebuilt sdist, mainland-source
   no-cache container workflow, real browser journey, and exact-revision Hosted
   CI pass;
9. README, FOUNDATION, and benchmark promotion occurs only in a later commit
   after exact implementation-revision Hosted evidence succeeds.

Passing these gates does not establish live-provider quality, publisher
provenance, multi-tenant security, public deployment, or production readiness.
