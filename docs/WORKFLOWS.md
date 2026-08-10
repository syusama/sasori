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

For `record_result`, pass the complete wrapper result envelope shown above. A
bare source value is not sufficient. `WorkflowHarness` validates the envelope
before committing recovery evidence, so an invalid digest, step, type, or value
hash leaves the original recovery state unchanged.

## Projections and events

`WorkflowHarness.projection(run_id)` returns a Python read-only view containing
the Workflow identity, core run projection, input, and ordered step state. It is
derived from the same core transcript and tool-call records; it is not another
store.

CLI and HTTP continue to expose the standard core run/event projection. The app
catalog exposes Workflow ID/version/digest, serial-only flags, wrapper Tool
contracts, logical Tool names, and the ordered definition-bound step mapping.
The bundled Workbench combines that catalog mapping with the existing reduced
public event stream to show queued, requested, running, approval, recovery, and
committed step states. It owns no second reducer or checkpoint authority. W0
does not expose a separate Workflow-step run endpoint.

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
W1  richer static serial authoring and public step projection
W2  bounded Workbench step inspection on the existing event reducer
W3  bounded parallel ready set
W4  branches, agent nodes, subflows, visual authoring
W5  queue, lease, fencing, scheduler, distributed execution
```

ADR-0013 decides the W0 runtime boundary. The current Workbench implements the
bounded W2 inspection slice for W0 definitions only; it is not visual
authoring, a DAG editor, or a new runtime. Richer authoring and all W3-W5 phases
require their own contracts, negative architecture controls, recovery
decisions, and acceptance evidence.
