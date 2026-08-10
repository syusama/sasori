# ADR-0015: Static compiled Workflow manifest and zero-execution preflight

- Status: Accepted for W1.1 implementation
- Date: 2026-08-10
- Depends on: [ADR-0013](ADR-0013-TYPED-WORKFLOW-BOUNDARY.md),
  [ADR-0014](ADR-0014-STATIC-SERIAL-AUTHORING-PUBLIC-PROJECTION.md)

## Context

W0 proved that a finite ordered Tool Workflow can reuse one core Harness,
store, approval path, recovery policy, and event truth. W1 added strict static
data/JSON authoring and a versioned public run projection. The bounded W2
Workbench inspector consumes that projection without creating another reducer
or checkpoint authority.

The remaining author-side gap is visible before a run exists. A developer or
future Studio needs one authoritative answer to these questions:

- which immutable definition and derived application will run;
- which installed Tool contract each logical step binds;
- where inputs, earlier results, and literals enter the definition;
- which steps require approval;
- what recovery policy follows from each declared Tool effect;
- whether a Tool revision, effect, schema, or handler signature has drifted.

Today the first-party app metadata and loaded application catalog independently
assemble part of that information. That invites field drift. Calling the
runtime as a "dry run" would be worse: a dry run that imports or invokes
trusted code, creates durable state, requests approval, or dispatches a Tool is
not a static preflight.

The next product-facing slice will be a Static Serial Workflow Studio. Before
that UI can safely save or present a definition, Sasori needs a small shared
manifest contract. This ADR adds only that static contract. It does not add a
Workflow catalog store, user-authored persistence, visual editing, or new
execution semantics.

## Decision

### 1. Preflight reuses the real compiler boundary

`preflight_workflow(spec, tools)` validates the supplied `WorkflowSpec` against
the trusted installed Tool registry using the same compiler helpers as
`compile_workflow()`. It then composes the public manifest from the validated
compiled wrapper Tools.

There is one source of truth for:

- source Tool name uniqueness, presence, effect, revision, schema digest, and
  handler signature;
- wrapper Tool name, effect, revision, and schema digest;
- wrapper-name collision detection;
- Workflow definition digest and derived application ID.

The preflight must not implement a second relaxed validator. A definition that
cannot be compiled must not produce a successful manifest.

### 2. Preflight has zero execution and zero durable mutation

Generating a manifest:

- does not instantiate `Harness` or `SQLiteStore`;
- does not create a run, call row, checkpoint, event, approval, or recovery
  record;
- does not call a model, source Tool handler, wrapper Tool handler, fault
  injector, provider, plugin entry point, network service, or subprocess;
- does not reserve an idempotency key or approval fingerprint;
- does not import code named by the static JSON definition.

The caller supplies already installed trusted `Tool` objects. Constructing
wrapper Tool contracts is static Python work; executing either source or
wrapper handlers is forbidden.

Third-party installed Python remains trusted code with full effective host
privilege. A manifest is not a sandbox, malware scan, signature, publisher
attestation, or proof that a handler obeys its declared effect.

### 3. Manifest schema version 1 is exact and deterministic

The top-level shape is:

```json
{
  "schema_version": 1,
  "workflow_id": "incident-mechanism",
  "version": "1",
  "definition_sha256": "...",
  "app_id": "flow.incident-mechanism....",
  "execution": "single-harness-ordered-tools-v1",
  "output_step": "record",
  "step_count": 2,
  "supports_parallel": false,
  "supports_branches": false,
  "supports_agent_nodes": false,
  "trust": {
    "execution_mode": "trusted_installed_python",
    "sandboxed": false
  },
  "inputs": [],
  "steps": []
}
```

Input entries expose only the existing immutable contract:

```json
{
  "key": "incident",
  "type": "string",
  "required": true,
  "max_bytes": 16384
}
```

Step entries expose:

- stable definition position and step ID;
- dependencies derived from `StepRef` bindings;
- redacted argument-source descriptors;
- logical and dispatch Tool names;
- logical and dispatch revisions/schema digests;
- effect, approval requirement, and static recovery policy;
- result kind/limit and output-step marker.

Collections preserve definition position where order is semantic. Argument
descriptors use canonical argument-name order. Dependencies use definition
position and contain no duplicates. Canonical JSON of the same accepted
definition and Tool registry is byte-identical across authoring entry points,
process restart, source checkout, wheel, sdist, and container.

### 4. Literal values are never disclosed

`InputRef` and `StepRef` descriptors expose their reference key/step. A
`Literal` descriptor exposes only:

```json
{
  "name": "mode",
  "kind": "literal",
  "value_type": "string",
  "canonical_bytes": 8,
  "value_sha256": "..."
}
```

The literal value itself is absent. Its digest is the SHA-256 of the existing
canonical JSON encoding, not a password hash or a secrecy guarantee. Low
entropy literals remain guessable; secrets should not be embedded in Workflow
definitions.

The manifest never contains runtime Workflow input, Tool output, call ID,
approval fingerprint, idempotency key, recovery reason, provider prose, or
internal mutable state.

### 5. Effect risk language is fixed

| Declared effect | `requires_approval` | `recovery_policy` |
|---|---:|---|
| `read_only` | `false` | `read_only_replay_allowed` |
| `idempotent` | `true` | `same_verified_business_key_only` |
| `side_effecting` | `true` | `manual_effect_resolution_on_ambiguity` |

These are policy labels, not outcome claims:

- `read_only` does not prove a third-party handler has no hidden side effect;
- `idempotent` does not expose a future business key or establish exactly-once;
- `side_effecting` does not prove an external action stopped or never happened
  after crash, timeout, exception, or cancellation.

### 6. The manifest is bounded and detached

Canonical manifest bytes have an explicit 4 MiB upper bound. Over-limit output
fails closed; it is never silently truncated. The current 1 MiB definition,
128-step, 128-input, 128-argument, literal, payload, result, and JSON-depth
limits remain authoritative.

Every public return is detached mutable JSON data. Mutating one caller's copy
cannot change the compiled Workflow, a later manifest, application metadata,
or runtime behavior.

### 7. Static and dynamic projections keep separate authority

The manifest is immutable definition metadata. `WorkflowHarness` exposes a
detached copy of the manifest compiled for that Harness. Python application
metadata, `/v1/apps`, the current Workbench definition preview, and a future
Workbench authoring preview consume the same composer.

The current Workbench validates this exact static contract and discloses step
dependencies, approval points, recovery labels, and the trusted-Python/no-
sandbox boundary through the immutable `workflow-manifest.0.1.0.js` extension.
The previously published `workflow.0.2.0.js` remains byte-stable. This is a
definition preview and inspection surface, not visual authoring or client-side
execution authority.

Dynamic run state continues to come only from the core-owned public run
projection and durable core records. The manifest does not decide whether a
step is pending, running, approved, ambiguous, completed, failed, or stopped.
It creates no client checkpoint and no `workflow.*` event family.

### 8. W0/W1 identity and runtime semantics do not change

W1.1 must preserve existing schema-version-1 definitions exactly:

- `WorkflowSpec.as_data()` and definition digest;
- derived application ID;
- wrapper Tool names, revisions, schemas, and call IDs;
- one wrapper ToolCall per serial model step;
- approval then explicit resume;
- effect recovery, cancellation, replay, final commit, events, and public run
  projection.

Manifest generation is an inspection feature. It is not a new execution
protocol marker and cannot reinterpret an existing serial definition.

## Rejected alternatives

- a "dry run" that creates a durable run or invokes a Tool;
- a second lightweight validator that can disagree with compilation;
- exposing literal values for UI convenience;
- generating runtime call IDs, fingerprints, or business idempotency keys;
- using a manifest as mutable Workflow state;
- storing manifests in a new Workflow database before Studio persistence is
  separately decided;
- calling installed entry points sandboxed or verified;
- adding YAML, expressions, dynamic imports, branches, parallel ready sets,
  Agent nodes, subflows, or visual authoring in this slice.

## Acceptance gates

Before W1.1 may be promoted as shipped:

1. Manual data, strict JSON, builder, preflight, and a compiled Harness produce
   identical manifest semantics and preserve W0 identity.
2. Model, source handler, wrapper handler, store constructor, and fault injector
   spies prove manifest generation performs zero execution/mutation.
3. Input/step/literal descriptors are exact; literal plaintext and runtime-only
   identities are absent.
4. All three effects map to the fixed approval/recovery labels.
5. Duplicate/missing Tool, effect/revision/schema/signature drift, wrapper
   collision, and over-limit manifest fail before any run exists.
6. Returned manifests and application catalogs are detached.
7. Existing serial Workflow golden, approval, effect, cancellation, crash,
   projection, CLI, HTTP, and browser behavior remains green without execution-
   semantic drift.
8. Source, installed-wheel, rebuilt-sdist, and mainland-source container
   preflight manifests are canonically identical.
9. `/v1/apps` and Workbench consume the shared manifest and do not re-derive
   effect, dependency, or recovery fields.
10. The exact implementation revision passes Hosted CI before README promotion.

Passing these gates does not establish live-provider quality, plugin honesty,
publisher provenance, multi-tenant security, public deployment, Workflow
Studio delivery, W3 parallel execution, or production readiness.

## Future sequencing

The next product slice may use this manifest as the immutable preview for a
Static Serial Workflow Studio. A future W3 read-only ready-set runtime still
requires a separate recovery-first ADR covering atomic accepted call sets,
durable child identity, bounded concurrency, cancellation fencing, stable
result commit order, partial failure, projection schema, restart/no-replay, and
negative architecture tests. This ADR grants no authority to implement or
claim that behavior.
