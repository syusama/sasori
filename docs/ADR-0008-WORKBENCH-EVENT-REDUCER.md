# ADR-0008: Workbench event reduction and view isolation

Status: accepted for the bundled Workbench after `0.1.0.dev0`.

## Context

The HTTP server already reads both JSON history and SSE batches from the same
append-only SQLite event projection. The Workbench previously sent all three
delivery modes through one `addEvent()` function, but that function mutated the
global UI state and DOM directly. It accepted any JavaScript number as a
sequence, advanced over gaps, and overwrote a duplicate sequence without
checking its semantic identity. A malformed or out-of-order item could
therefore move the reconnect cursor past an event that had never been shown.

Run selection also had two lifetimes: the SSE request was aborted when another
run opened, while the preceding status/history requests were not owned by that
selection. A late response for an older selection could replace the newer
view. Syntax checks and static-asset tests did not exercise either behavior.

This is a consumer reliability decision. It does not change the Python event
contract, event version, SQLite rows, HTTP JSON, or SSE wire shape.

## Decision

The Workbench uses one dependency-free pure reducer for live SSE, cold JSON,
and reconnect delivery. Its state is scoped to exactly one `run_id` and
contains a contiguous event list plus the last safely consumed sequence.

For every projected event the reducer:

1. validates the version-1 public envelope and JSON-safe stable fields;
2. ignores a structurally valid event for another run, because an aborted old
   stream may finish delivering a buffered item;
3. appends only `cursor + 1`;
4. treats an identical `(run_id, seq)` delivery as an idempotent no-op;
5. rejects a conflicting duplicate rather than overwriting accepted history;
6. rejects a gap rather than advancing the reconnect cursor; and
7. returns new frozen state without reading or modifying the DOM.

Unknown event type names that satisfy the version-1 naming grammar remain
visible through the existing raw-label fallback. An unsupported event version
fails closed because the consumer cannot assume its shape or semantics.
Additional envelope fields are ignored; the reducer retains only the stable
public version-1 fields.

The view layer owns rendering. It applies a reducer result, updates the visible
cursor, and renders only when state changed. A projection contract error is
shown to the operator and stops automatic reconnect for that selected run; an
unchanged malformed durable event must not create a silent retry loop.

Previously published content-addressed asset URLs remain served byte-for-byte
for their immutable cache lifetime. The HTML entry point moves to new versioned
reducer and application URLs; a deployment must not reuse an old URL for new
bytes or remove it while an older page can still reference it.

Each run selection owns one abort controller and monotonically increasing view
epoch. It also owns at most one child watcher controller for status/history/SSE
polling; starting a replacement watcher aborts the older watcher without
invalidating the selected view. Code checks the watcher or view epoch and
selected `run_id` after every awaited request before mutating state. Opening a
newer run aborts both the older watcher and selection, and a response that wins
the network race but loses the epoch race is discarded.

## Rejected alternatives

- `Math.max(cursor, seq)`: it can permanently skip a missing durable event.
- A `Map` assignment keyed only by sequence: it silently replaces conflicting
  evidence and hides which run owns the key.
- Clearing all events and retrying every projection error: a malformed durable
  row would loop forever and conceal the contract violation.
- A frontend state-management dependency: the required reducer and ownership
  rules are small, testable with Node's standard library, and do not justify a
  second application runtime.
- Trusting request abort alone: a completed promise callback can still be
  queued when a newer selection begins.

## Verification

The Node regression suite covers sequential append, semantically identical
duplicates from differently ordered JSON, stale-run delivery, gaps,
conflicting duplicates, malformed/fractional/non-finite sequences, unsupported
versions, invalid JSON data, forward-compatible version-1 event names, and
post-acceptance input mutation. The real-browser regression loads the production
HTML and exact versioned assets while a same-origin fixture holds responses
after request dispatch. It covers late status, cold-event, and SSE delivery,
same-run epoch replacement, and accepted create/approval mutations. Each held
response is released only after a newer view is visible, proving that neither a
different older selection nor an older epoch for the same `run_id` can reclaim
the DOM. The test has no provider, browser-package, or production-server test
endpoint dependency.

Python, CLI, and HTTP continue to use the same public event projection. Any
future change to the event version, stable fields, gap policy, conflict policy,
or run-selection ownership requires a new decision record and updated
consumer acceptance evidence.
