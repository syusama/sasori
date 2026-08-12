# ADR-0020: Bounded transient Tool execution progress

- Status: Accepted
- Date: 2026-08-13
- Decision owners: Sasori maintainers
- Affects: `sasori-core` Tool contracts, Harness execution, provider Tool schema
  projection, Workflow wrappers, transient observers

## Context

ADR-0018 keeps the canonical Core limited to contracts, one single-agent Loop,
public projection, the Harness, and deterministic test helpers. ADR-0019 added
one bounded model stream but deliberately left Tool execution with only durable
`tool.started` and terminal `tool.completed` / `tool.failed` facts.

Long-running tools such as browsers, downloads, shell tasks, code execution,
and large queries need live progress between those durable facts. Pi's mature
low-level loop was reviewed at commit
`452923b54a6c8b2f95b80157a8f6c7963f183101` (MIT). Its Tool update lifecycle
and settle-time fencing are useful behavioral references. Sasori keeps an
independent Python implementation and does not copy Pi's TypeScript code.

A normal callback is insufficient. Synchronous Tools execute in worker threads
and may continue after the awaiting task times out or is cancelled. An
unbounded or durable callback could therefore publish late updates, bloat
checkpoints, influence approval or idempotency, or make a stale worker appear
authoritative after the run has settled.

## Decision

### Explicit execution context

A Tool opts in by declaring one runtime-reserved, keyword-only parameter:

```python
def download(url: str, *, tool_context: ToolExecutionContext) -> str:
    tool_context.report_progress({"phase": "download", "percent": 50})
    return "artifact.zip"
```

The Harness injects this parameter only when it is explicitly present and
keyword-only. Existing Tools receive no new argument. `tool_context` is never
model input: provider schemas, plugin schema digests, Workflow authoring, and
other static Tool projections omit it. A model-supplied value with that name is
rejected as a reserved argument before Tool dispatch.

The context grants only `report_progress(data) -> bool`. It does not expose the
Harness, Store, approval resolver, cancellation primitive, or another
authority-bearing object. The return value reports whether an update entered
the bounded live-delivery path; it is not evidence that the Tool or its side
effect succeeded.

### Transient event contract

Accepted updates become deeply immutable `ToolProgressEvent` values containing
only:

- version, run ID, step, call ordinal, provider call ID, and Tool name;
- a per-call sequence number starting at one;
- one detached JSON mapping payload.

They are delivered through the optional Harness `tool_progress_sink`. They are
never written to a `RunStore`, checkpoint, public `Event`, run projection,
provider message, approval request, call fingerprint, idempotency key, Tool
result, or cold replay. CLI, HTTP, Workflow, and Workbench adapters may consume
this Core sink; they must not implement a second progress state machine.

Delivery is best effort and ordered for each serial Tool call. A synchronous
observer is dispatched outside the Tool worker so observer exceptions cannot
become Tool exceptions. Ordinary exceptions and `asyncio.CancelledError`
raised by the observer are swallowed at the observer boundary. On normal Tool
settlement, the drive waits for already accepted deliveries before committing
the terminal Tool outcome. On caller cancellation, pending observer work may be
abandoned so cancellation is not converted into an observer-controlled wait.
An observer already running in a Python thread cannot be forcibly stopped and
has no authority over durable run state.

### Bounds and validation

Each Tool call has fixed Core limits:

- at most 256 accepted progress events;
- at most 64 KiB of canonical UTF-8 JSON per payload;
- at most 1 MiB of canonical UTF-8 JSON across the call.

Payload keys must be strings. Values must be finite, bounded JSON with the same
depth and node limits used for Tool arguments. Payloads are detached before
delivery and exposed as deeply immutable mappings/tuples. Malformed or
oversized updates return `False`; once an event-count or cumulative-byte budget
is exhausted, later updates return `False`. Rejection changes neither the Tool
outcome nor the durable trace.

### Settlement fencing

Each dispatch owns one reporter generation. The reporter closes when the Tool:

- returns;
- raises;
- exceeds the Tool timeout;
- is cancelled;
- reaches an `effect_unknown` or manual-recovery boundary.

After closure, `report_progress()` is a no-op returning `False`. This includes a
synchronous Tool thread or child thread that continues after the awaiting drive
has timed out or been cancelled. Closing a reporter does not claim that the
underlying thread, process, remote operation, or side effect was stopped.

Progress is created only after approval and the durable `tool.started` commit.
It cannot appear for malformed, unknown, denied, incomplete, or not-yet-approved
calls.

## Explicitly deferred

This ADR does not add:

- durable `tool.progress` public events or checkpoint replay;
- parallel Tool execution;
- mutable before/after Tool hooks;
- steering or follow-up queues;
- progress-driven cancellation or approval;
- exactly-once progress delivery;
- forced termination of synchronous Tools, processes, or remote calls.

Any durable progress schema would change public events and golden traces and
requires another decision record.

## Acceptance

- existing Tools and durable semantic traces remain unchanged;
- async and worker-thread Tools emit ordered immutable progress;
- provider schemas and plugin schema digests exclude `tool_context`;
- model-supplied `tool_context` never reaches a handler;
- observer mutation, ordinary exceptions, and `CancelledError` cannot change
  Tool arguments, output, error classification, effect, approval, fingerprint,
  idempotency, recovery, or terminal run state;
- payload, event-count, and cumulative-byte bounds are deterministic;
- return, exception, timeout, cancellation, and recovery close the reporter;
- updates from a stale synchronous or child thread are ignored without an
  unhandled exception;
- Ephemeral and SQLite stores retain equal traces with no progress rows;
- `sasori-core` remains zero-dependency and passes import-isolation, package,
  full repository, and mainland-source container gates.

## Consequences

Sasori gains live Tool feedback without treating UI telemetry as recovery
truth. Tool authors opt in with one explicit parameter, while applications get
one framework-level stream that works across the same Python/CLI/HTTP/UI
runtime path. The fixed bounds and settle-time fence keep stale worker threads
from becoming authoritative and preserve the small Core boundary.
