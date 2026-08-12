# ADR-0019: Core loop, model-stream, and settlement semantics

- Status: Accepted
- Date: 2026-08-11
- Decision owners: Sasori maintainers
- Affects: `sasori-core` contracts, Loop/Harness layering, transient model
  streaming, concurrent drive admission, idle observation

## Context

ADR-0018 created one zero-dependency `sasori-core` distribution and one
canonical Harness. The runtime already fails closed on malformed or truncated
tool calls, turns ordinary tool exceptions into explicit tool results, keeps
cancellation as independent control flow, and persists step-boundary recovery.

The Pi audit at commit `452923b54a6c8b2f95b80157a8f6c7963f183101`
identified three useful remaining P0 contracts:

1. an explicit low-level Agent Loop below the high-level Harness;
2. a provider-neutral stream with one start, zero or more deltas, and exactly
   one terminal outcome;
3. a distinction between a durable terminal run and a runtime operation that
   has fully settled and become idle.

Pi's current listener and new/legacy Harness paths are not copied. Sasori must
retain one executable path, bounded behavior, and honest cancellation claims.

## Decision

### One low-level loop

`run_agent_loop(harness, state)` is the only function that drives an already
created or restored snapshot through model and tool steps. `Harness.run()` owns
new-run creation and `Harness.resume()` owns restoration; both enter the same
admission boundary and call `run_agent_loop()`.

There is no second Session, Agent, or legacy Loop implementation.

### Provider-neutral transient stream

A model may continue to implement only:

```python
async def complete(messages, tools) -> ModelReply: ...
```

or additionally implement:

```python
def complete_stream(messages, tools) -> AsyncIterator[ModelStreamEvent]: ...
```

When `complete_stream` is callable, the runtime uses it as the model path. The
stream grammar is:

```text
start
  -> zero or more text_delta / thinking_delta / tool_call_delta
  -> exactly one done / error / aborted
  -> end of iterator
```

Rules:

- an event before `start`, duplicate `start`, missing terminal, duplicate
  terminal, an event after terminal, or a non-`ModelStreamEvent` value is a
  model-stream protocol failure;
- `done` carries one complete `ModelReply`; partial tool-call deltas never
  execute;
- `error` becomes an explicit model failure;
- `aborted` becomes `asyncio.CancelledError` and follows the existing
  cooperative cancellation path;
- one total model timeout covers the complete iterator, including its required
  end after a terminal event;
- when `done` arrives, the runtime captures its authoritative bounded reply
  before invoking an observer; the observer receives a detached, deeply
  immutable terminal snapshot;
- stream observers are best effort, and neither observer mutation nor provider
  generator mutation after `done` can change Tool input, history, approval,
  fingerprint, recovery, or another durable run fact;
- malformed terminal input remains malformed after snapshotting and can never
  become an executable Tool call;
- deltas are transient UI/provider progress, not durable public events and not
  a serialization of mutable provider state.

The existing `model.started`, `model.completed`, `model.failed`, and terminal
run events remain unchanged. Therefore this ADR does not revise the public
event schema or golden traces.

### Finished, settled, and idle

- **finished** means a terminal run event and snapshot were durably committed;
- **settled** means the current `run()` or `resume()` drive has unwound after
  its checkpoint commits and synchronous best-effort observer delivery;
- **idle** means this Harness has no admitted drive in flight.

`Harness.wait_for_idle()` waits for the third condition. It is not evidence
that a remote provider or synchronous tool was forcibly stopped. Cancellation
remains cooperative.

One Harness rejects a second concurrent drive for the same `run_id` before it
can call the model or a tool. Different run IDs may proceed concurrently when
the injected store supports them. Store writer ownership and compare-and-swap
rules remain authoritative outside this process-local admission check.

### Explicitly deferred

This ADR does not add:

- parallel tool dispatch;
- mutable before-tool hooks;
- steering or follow-up queues;
- a durable `run.settled` event;
- exactly-once execution claims.

Parallel effects, hook argument replacement, and queued control messages alter
approval, ordering, recovery, and persistence semantics. They require a
separate ADR and store conformance before becoming public APIs. Stable exports
must never be added as `NotImplemented` scaffolds.

## Acceptance

- complete-only models retain the exact semantic trace;
- valid streaming models produce the same committed reply and tool behavior;
- malformed, interrupted, duplicate-terminal, timeout, error, and aborted
  streams pass deterministic offline tests;
- a truncated streamed tool call never reaches a handler;
- stream-observer mutation and exceptions cannot change the run result, Tool
  input, fingerprint, history, or stored call arguments;
- a second same-run drive fails before another model/tool call;
- `wait_for_idle()` blocks during a drive and returns after success, pause,
  failure, or cancellation settlement;
- the full store conformance and repository suite pass on the canonical Loop.

## Consequences

Sasori gains a Pi-like low-level Loop and stream protocol without provider SDKs,
durable delta bloat, or a duplicate Session path. The core remains
zero-dependency. Interactive queues and parallel tools remain honestly absent
until their safety and recovery contracts are implemented.
