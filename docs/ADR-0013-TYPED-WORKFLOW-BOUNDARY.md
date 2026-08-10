# ADR-0013: Typed ordered Workflow boundary

Status: **accepted for bounded W0 at `af3ecb4`; exact-revision Hosted promotion passed in run `31355739157`**

Date: 2026-08-10

Decision owner: repository maintainer

## Context

Sasori needs a Workflow switching-value proof without creating a second Agent
runtime. The useful lesson from larger frameworks is that workflows should
reuse the same tool, approval, durable run, and observability contracts. The
failure mode to avoid is a parallel graph runtime with its own loop, mutable
state row, checkpoint database, effect policy, and event truth.

The first slice therefore answers one narrow question:

> Can a definition-bound ordered sequence of typed tool results run through the
> existing Harness without weakening approval, idempotency, crash recovery, or
> cancellation semantics?

It does not attempt to answer graph scheduling, parallelism, branches, agent
nodes, visual authoring, distributed leases, or multi-tenant execution.

## Decision

### 1. `sasori_flow` stays outside the core

The `sasori` package does not import `sasori_flow`. The extension may use the
public `Harness`, `Model`, `Tool`, event projection, and SQLiteStore contracts,
but it may not call private core execution methods.

There is one execution authority:

```text
Python / CLI / HTTP / Workbench
  -> WorkflowHarness
  -> core Harness.run() or Harness.resume()
  -> core Harness._drive()
  -> wrapper Tool
  -> source Tool handler
```

`WorkflowHarness` is a `Harness` subclass so existing app loading, CLI, server,
approval, effect recovery, cancellation, and final-commit paths remain the
same. `sasori_flow` defines no WorkflowStore, scheduler loop, background task,
or Workflow checkpoint table.

### 2. W0 is a finite serial sequence, not a DAG engine

A `WorkflowSpec` contains ordered `ToolStep` values. A step may bind arguments
from a declared input, a literal, or an earlier step result. Forward references
are rejected. Each `WorkflowModel.complete()` call returns at most one complete
ToolCall. The final model turn returns the canonical outcome, so:

```text
Harness.max_steps = number of Workflow steps + 1
```

The current execution protocol has no ready-set scheduler, branch, join,
fan-out, fan-in, loop, map, reduce, retry DSL, or subflow.

### 3. The canonical definition owns identity

The definition SHA-256 covers:

- schema version, Workflow ID, author version, and execution protocol;
- ordered input slots;
- ordered steps and output step;
- source tool name, effect, tool revision, and tool-schema digest;
- argument binding kinds and literal values;
- result JSON kind and result byte limit.

The application ID is derived as:

```text
flow.<workflow-id>.<first-12-definition-sha256>
```

Changing any declared contract produces a different digest and application ID.
An old run cannot be resumed by a differently bound Workflow application. The
digest binds the declaration and source Tool contract; it is not a signature of
the handler's Python source, wheel, machine, or publisher identity.

### 4. Inputs and results use bounded JSON kinds

Supported kinds are:

```text
string  integer  number  boolean  object  array  null
```

Boolean is not accepted as integer. NaN, Infinity, non-JSON values, lone Unicode
surrogates, unknown inputs, missing required inputs, wrong top-level kinds, and
over-limit values fail closed.

The W0 hard limits are:

| Boundary | Limit |
|---|---:|
| Steps | 128 |
| Input slots | 128 |
| Arguments per step | 128 |
| Canonical definition | 1 MiB |
| Canonical Workflow input | 256 KiB |
| Public input record | 256 KiB |
| One literal | 128 KiB |
| Resolved step payload | 256 KiB |
| JSON nesting depth | 32 |
| One declared result | 1 MiB maximum; 64 KiB default |

The source Tool signature and its digest are checked again during compilation.
Step argument names must exactly match source handler parameters, excluding the
reserved keyword-only idempotency key. Source parameters must be explicit
positional-or-keyword or keyword-only names; positional-only, `*args`, and
`**kwargs` handlers fail compilation before a run or approval exists. W0 does
not infer nested schemas across edges and must not be described as an
end-to-end statically typed DAG.

### 5. Public input remains public; control input remains bound

The durable history contains a canonical system control record, a canonical
system input binding, and then the plain public user message. The binding stores
the validated typed input and the SHA-256 of the public content. Replay checks
all three records.

This preserves the single core `run_projection`: CLI, HTTP, history previews,
and Workbench expose the user's submitted text rather than an internal Workflow
prefix. A Python mapping is projected as canonical JSON.

### 6. Each step becomes a normal wrapper Tool

The deterministic model proposes exactly this outer argument shape:

```json
{
  "definition_sha256": "...",
  "step_id": "record",
  "payload_json": "{\"summary\":\"...\"}"
}
```

The wrapper preserves the source Tool's `read_only`, `idempotent`, or
`side_effecting` effect. Mutable wrapper revisions bind the Workflow digest,
step contract, source revision/schema, and wrapper protocol. The wrapper checks
the definition, step, canonical payload, and typed result before returning to
the Harness. `WorkflowModel` never calls a handler.

`schema_sha256` means the source Tool contract is definition-bound and drift is
rejected. It does not mean W0 performs nested JSON-Schema inference between all
input and output edges.

### 7. Approval never executes

The Harness persists the accepted wrapper ToolCall before requesting approval.
The approval fingerprint binds the core run ID, model step, ordinal, wrapper
name, canonical outer arguments, and wrapper revision. Approving records the
decision and moves the run to `awaiting_resume`; it does not invoke the source
Tool. An explicit resume is required.

Denial produces an explicit tool error and no mutable source handler executes.

### 8. Idempotency separates durable and business identities

For an idempotent source Tool:

1. the source Tool derives its business idempotency key from the resolved
   business payload;
2. the wrapper derives a Workflow-bound outer key from the definition, step,
   and business key;
3. the Harness persists and verifies the outer key;
4. the wrapper verifies it and passes the original business key to the source
   handler.

A Workflow call ID is stable correlation evidence. It is not an effect
idempotency key.

### 9. Results use a typed envelope

The wrapper returns:

```json
{
  "version": 1,
  "definition_sha256": "...",
  "step_id": "inspect",
  "value": "diagnostic",
  "value_sha256": "..."
}
```

Replay verifies the exact key set, canonical JSON, identity, declared JSON kind,
byte limit, and value digest. A read-only invalid result becomes an explicit
tool/run failure. If a mutable handler may already have executed but its result
is invalid, the existing Harness recovery state remains authoritative:

- idempotent ambiguity may enter retryable recovery and retry with the same
  business key;
- ordinary side-effect ambiguity enters `effect_unknown` and requires an
  operator decision.

An operator `record_result` must supply the complete typed wrapper envelope, not
a bare source value. `WorkflowHarness` validates it before the core commits
`recovery.resolved` or manual `tool.completed` evidence.

### 10. Durable replay is cross-record validation, not a hostile ledger

Before approval resolution, effect resolution, event access, projection, or
resume, `WorkflowHarness` replays the complete Workflow history and checks each
accepted step against its durable core tool-call row. It verifies the expected
call ID, wrapper name, arguments, effect, revision, idempotency binding, result,
model step, and committed final.

This catches corruption or inconsistent pending records before another tool
dispatch. SQLite remains trusted local durable state, not a cryptographically
signed or tamper-evident ledger. A party able to rewrite all related rows and
the application code is outside this guarantee.

### 11. Recovery is at step boundaries, not exactly once

Workflow adds no recovery algorithm. It inherits the Harness contract:

- read-only work can be repeated when safe;
- idempotent work may retry only with the same verified key;
- an ordinary side effect left in `dispatching` becomes `effect_unknown`;
- completed resume returns the committed final without replaying model or tool;
- definition or source Tool contract drift fails closed.

There is no atomic transaction between Sasori and an arbitrary external side
effect, no automatic compensation, and no exactly-once claim.

### 12. Events and projections have one authority

Workflow emits only the existing versioned event families:

```text
run.*  model.*  tool.*  approval.*  recovery.*  artifact.*
```

No `workflow.*` event family is introduced. `WorkflowHarness.projection()` is a
read-only Python derivation of the durable core run/call records. The standard
CLI and HTTP run endpoints continue to return the core projection. The app
catalog projects the immutable ordered step/tool mapping. The Workbench joins
that definition metadata with the existing public event reducer to render a
bounded serial step inspector; it does not add a Workflow-step run endpoint,
mutable graph state, or a second UI reducer.

### 13. Cancellation is cooperative

`CancelledError` is not swallowed and the next Workflow step is not started
after cancellation. This does not prove that a remote provider, an external
system, or a synchronous handler already running in a worker thread stopped.
Mutable ambiguity retains the existing effect-unknown policy.

### 14. Definitions are trusted installed Python code

Workflow specs, factories, wrappers, and source Tools execute as trusted
installed Python. Entry points are not a sandbox. W0 provides no filesystem,
network, subprocess, CPU, memory, tenant, or secret isolation.

## Rejected alternatives

- a second Workflow agent loop;
- a Workflow-specific SQLite checkpoint/event authority;
- direct handler invocation from WorkflowModel;
- one mutable JSON graph-state row as durable truth;
- new `workflow.*` public events for W0;
- a background scheduler or Pregel-style engine before a serial proof;
- provider/Workflow call IDs as business idempotency keys;
- calling trusted Python entry points a sandbox.

## Consequences

The W0 runtime remains small and inspectable, and Python, CLI, HTTP, container,
and the bounded Workbench inspector share the same effect semantics. The tradeoff is an
intentionally narrow feature surface. Sasori can claim ordered,
definition-bound tool workflows only after the relevant source, wheel, sdist,
container, browser, and exact-revision Hosted gates pass.

## Explicit non-goals

W0 is not a general Workflow Engine and does not implement DAGs, parallel ready
sets, branches, conditions, fan-out/fan-in, loops, map/reduce, retry policies,
subflows, ScriptNode, Agent nodes, multi-agent orchestration, visual editing,
cron, webhooks, queues, leases, fencing, distributed workers, automatic
rollback, exactly-once effects, tenant isolation, or untrusted-code execution.

## Acceptance gates

The implementation commit must pass:

- canonical identity, bounds, references, tool drift, and negative architecture
  tests;
- approval/denial, typed result, invalid manual recovery, cancellation, and
  durable pending-record integrity tests;
- real child-process crashes at dispatch, tool return, and final commit;
- first-party Python, CLI, and HTTP lifecycle tests;
- installed-wheel and rebuilt-sdist consumer workflows;
- mainland-source no-cache container build and real approval/resume/restart
  workflow;
- browser selection, approval, resume, reload, and no-replay journey;
- exact-revision Hosted CI before README promotion.

These gates passed for
[`af3ecb4`](https://github.com/syusama/sasori/commit/af3ecb4e613d6458a56843ce4b7de7bb056b56c2)
in [Hosted run 31355739157](https://github.com/syusama/sasori/actions/runs/31355739157).
The ordinary `main` run did not create a release tag, signature, or trusted
provenance; those remain separate release gates.

Passing deterministic tests does not establish live-provider quality,
multi-tenant security, publisher provenance, public deployment, or production
readiness.
