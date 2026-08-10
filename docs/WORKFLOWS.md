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

The app catalog continues to expose Workflow ID/version/digest, serial-only
flags, wrapper Tool contracts, logical Tool names, and the ordered
definition-bound step mapping. The bundled Workbench uses `run.workflow` for
durable rail semantics and the catalog only for immutable definition mapping.
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
- Python, CLI, HTTP, installed wheel, rebuilt sdist, container restart, and
  browser journeys.

Real-provider smoke tests supplement these deterministic gates; they do not
replace them.

## Roadmap boundary

```text
W0  one-Harness ordered Tool proof
W1  strict static serial authoring and versioned public step projection
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
