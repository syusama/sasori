# ADR-0016: Static Serial Workflow Studio preview boundary

- Status: Accepted and Hosted-verified for W1.2
- Date: 2026-08-10
- Depends on: [ADR-0014](ADR-0014-STATIC-SERIAL-AUTHORING-PUBLIC-PROJECTION.md),
  [ADR-0015](ADR-0015-STATIC-WORKFLOW-MANIFEST-PREFLIGHT.md)

## Context

W1 added one strict schema-version-1 JSON codec for the existing serial
`WorkflowSpec`. W1.1 added one zero-execution compiler preflight and an exact,
detached definition manifest. The Workbench can inspect a loaded Workflow, but
an author still has no product surface for checking a draft before any run
exists.

The useful next slice is deliberately smaller than a general Workflow Studio:

```text
raw strict JSON draft
    -> authenticated local HTTP adapter
    -> existing strict Workflow codec
    -> existing compiler preflight against host-owned Tool contracts
    -> detached immutable manifest preview
```

This boundary is security-sensitive. A convenient editor must not become a
dynamic Python loader, an unreviewed Tool allowlist, a second Workflow runtime,
or a browser-owned source of effect and recovery truth. A button labelled
"validate" must also not perform a dry run that creates records or invokes
trusted code.

## Decision

### 1. W1.2 is a transient draft preflight, not a saved Studio

The first Studio slice accepts an in-memory browser draft and returns one
preflight result. It has no create, save, update, delete, publish, activate,
execute, import, or schedule operation.

There is no Workflow catalog table, draft database, browser persistence,
checkpoint, event, approval, recovery record, or run identity. Reloading the
page discards the draft. A later durable catalog requires a separate ADR for
identity, optimistic concurrency, migrations, activation authority, audit,
authorization, and deletion semantics.

### 2. The HTTP request is the exact strict Workflow JSON document

The local adapter adds:

```http
POST /v1/workflows/preflight
Content-Type: application/json; charset=utf-8

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

The request body is the definition itself, not a wrapper containing an already
parsed object or a second JSON string. This preserves one strict parse boundary
for duplicate keys, malformed UTF-8, byte-order marks, non-finite constants,
unknown/missing fields, excessive depth, and all existing count/byte limits.

The existing HTTP envelope remains authoritative: one `Content-Length`, no
chunked body, exact JSON UTF-8 media type, a 1 MiB body ceiling, bounded socket
read time, existing bearer/same-origin checks, and no query parameters. The
endpoint does not accept YAML, multipart data, URLs, file paths, archives, form
data, templates, expressions, JavaScript, Python, shell, module names, entry
points, environment interpolation, or handler code.

### 3. The deployment owns and freezes the Tool registry

The request cannot name code to import. At process startup, the HTTP owner
freezes the Studio Tool registry from Tools of successfully loaded, explicitly
configured ordinary application Harnesses. Those application factories are
already trusted installed Python selected by the deployer.

The registry rules are fail closed:

- compiled `WorkflowHarness` wrapper Tools are excluded;
- unavailable applications contribute no Tools;
- a Tool name present in more than one loaded Harness is ambiguous and every
  occurrence of that name is excluded;
- registry order is deterministic and never supplied by the browser;
- the registry is not refreshed or mutated by a preflight request;
- the submitted `effect`, `tool_revision`, schema digest, argument names, and
  handler signature must match the frozen Tool contract through the existing
  shared compiler boundary.

The Workbench may use authenticated `/v1/apps` Tool metadata as authoring
guidance. That catalog cannot decide preflight eligibility. Only the
server-owned frozen Tool objects passed to `preflight_workflow()` decide
whether a definition is accepted.

A deployment containing only compiled `WorkflowHarness` applications has an
empty Studio registry and therefore rejects every Tool step as unavailable.
That is the intended fail-closed result for this slice. A canonical Studio
deployment must also configure the ordinary source-Tool Harnesses it wants to
offer. The server must not recover source Tools by inspecting private compiled
steps or wrapper internals. A future explicit deployer-owned Studio registry is
a separate configuration decision.

Installed application factories and Tool handlers remain trusted Python with
full effective host privilege. Excluding imports from the draft protects this
request boundary; it does not sandbox, inspect, sign, attest, or prove honest
behavior for installed code.

### 4. One existing codec and compiler remain authoritative

The endpoint performs only:

```python
spec = workflow_spec_from_data(strictly_parsed_body)
manifest = preflight_workflow(spec, frozen_tools)
```

It does not implement a browser compiler or an HTTP-specific relaxed checker.
`preflight_workflow()` continues to share `_compile_steps()` with
`compile_workflow()`, including duplicate/missing Tool, effect/revision/schema,
signature, reserved argument, and wrapper-collision checks.

Successful response schema version 1 is exact:

```json
{
  "ok": true,
  "schema_version": 1,
  "manifest": {}
}
```

`manifest` is the existing W1.1 detached manifest without additional runtime
fields. Canonical manifest bytes, definition digest, derived application ID,
literal redaction, trust disclosure, approval labels, and recovery labels are
unchanged.

### 5. Preflight has zero execution and zero durable mutation

Handling the request must not:

- construct another Harness, `WorkflowHarness`, `SQLiteStore`, or artifact
  store;
- call a model, provider, source Tool handler, wrapper Tool handler,
  idempotency callback, fault injector, plugin loader, network service, or
  subprocess;
- create or mutate a run, call, checkpoint, event, message, approval, effect,
  recovery, artifact, app catalog, or Tool registry;
- allocate a call ID, approval fingerprint, idempotency key, execution lease,
  scheduler item, or browser checkpoint.

Read-only inspection must not acquire the runtime mutation gate. A preflight
may run while another Harness drive is awaiting, without claiming queued or
parallel Workflow execution.

### 6. Errors are structured, bounded, and adapter-owned

The HTTP parser keeps its existing transport codes such as `malformed_json`,
`body_too_large`, `request_timeout`, `unsupported_media_type`, and
`invalid_body`.

After transport parsing, controlled Workflow errors use:

```json
{
  "ok": false,
  "error": {
    "code": "workflow_preflight_rejected",
    "message": "workflow step record tool schema changed",
    "retryable": false,
    "reason_code": "tool_contract_mismatch"
  }
}
```

The stable reason codes are:

| Reason | Meaning |
|---|---|
| `invalid_definition` | the exact Workflow data contract was rejected |
| `tool_contract_mismatch` | the frozen Tool registry or compiler contract rejected the definition |
| `manifest_rejected` | the bounded detached manifest could not be composed |

Messages come only from Sasori-controlled `WorkflowValidationError` or
`WorkflowCompileError` branches and are bounded before projection. Arbitrary
handler, provider, store, import, traceback, or exception prose is never
returned. Unexpected exceptions remain the generic existing `internal_error`.

Owner timeout, shutdown, connection pressure, and caller authentication keep
their existing adapter codes and retry semantics. A timeout is not reported as
a successful preflight.

### 7. Browser authority is limited to draft text and result display

The Workbench publishes new immutable `workflow-studio.*` assets instead of
overwriting any existing cached asset. The Studio may:

- edit bounded plain JSON text;
- suggest Tool contracts already disclosed by the application catalog;
- submit that exact text to the authenticated same-origin endpoint;
- render a successful server manifest or a structured server error;
- display the `TRUSTED PYTHON` and `NO SANDBOX` boundary prominently.

The Studio must not locally derive or repair effect, revision, schema digest,
approval, recovery, definition digest, app ID, or manifest acceptance. It must
not execute code from draft or response content and must render all content as
text without `innerHTML`.

Each editor mutation increments an edit epoch, clears every prior successful
manifest/status, and aborts or logically abandons any in-flight request. Each
submission captures the exact draft text, current edit epoch, unique request
identity, and an `AbortController`. A response may render only while all four
still match and its signal is not aborted. A later edit, later submission, or
closing the Studio invalidates the older request even when the changed draft
has not been submitted. An old, aborted, malformed, or late response cannot
replace the current editor status or manifest. Client abort is only a
UI/network cancellation request; the bounded server preflight may already be
completing. Because preflight has no mutation, that distinction creates no
recovery record and makes no claim of forcibly stopping synchronous work.

The exact-text boundary includes Unicode scalar integrity. Before fetch, the
Studio encodes the draft with `TextEncoder` and requires a fatal UTF-8
`TextDecoder` round-trip to reproduce the same JavaScript string. A lone
surrogate would otherwise be silently replaced by U+FFFD; instead it is marked
`INVALID UNICODE`, disables submission, clears any older verdict, and sends no
request.

Only an exact HTTP `422 workflow_preflight_rejected` error is an authoritative
definition rejection and may render `REJECTED`. Authentication/authorization,
timeout/shutdown, network, non-JSON, malformed-success, and client-validation
failures render `UNVERIFIED` with `NO SERVER VERDICT`. A disclosed `retryable`
value is operator guidance only; this Studio does not retry automatically.

### 8. Existing runtime identity and recovery semantics do not change

This endpoint is not on the run path. It does not change:

- `WorkflowSpec.as_data()`, digest, application ID, wrapper Tool identity, or
  call ID;
- the single Harness Loop, SQLite/checkpoint authority, approval then explicit
  resume, effect recovery, cancellation, event projection, or final commit;
- `/v1/runs`, `/v1/apps`, public events, Workflow run projection, CLI, or Python
  compile behavior;
- the rule that step-boundary recovery is not exactly-once execution.

No `workflow.*` event family or mutable browser reducer is added.

## Threat model

| Threat | Fail-closed boundary |
|---|---|
| Draft requests arbitrary Python, shell, URL, or module | exact schema rejects the field; no dynamic import path exists |
| Duplicate JSON key hides a changed Tool contract | the one strict HTTP parse rejects duplicates before authoring |
| Browser forges effect/revision/schema or approval policy | shared compiler checks frozen Tool objects; server manifest derives policy |
| Two loaded apps expose the same Tool name | every ambiguous name is excluded from the Studio registry |
| Literal leaks through preview | existing manifest exposes only JSON type, canonical byte count, and SHA-256 |
| A handler executes during validation | deterministic spies fail the zero-execution acceptance gate |
| A preflight creates durable state | store/run/event/checkpoint snapshots remain byte/row stable in regression tests |
| A slow old response overwrites a new draft | abort plus epoch comparison drops the stale result |
| Client abort is mistaken for remote cancellation | UI language states only the response was abandoned; no execution claim is made |
| Installed code is described as sandboxed | visible trust object remains `trusted_installed_python`, `sandboxed: false` |

## Rejected alternatives

- a dry run that constructs a Harness, Store, run, call, checkpoint, approval,
  or invokes any handler;
- request-supplied entry points, module paths, Python, JavaScript, shell, URL
  imports, expressions, templates, or environment expansion;
- a client-computed manifest or client-owned preflight eligibility decision;
- silently choosing one Tool when loaded applications have a name collision;
- saving drafts in SQLite, localStorage, IndexedDB, files, or browser history;
- adding a visual DAG, branches, parallel sets, Agent nodes, subflows, retries,
  schedules, activation, or execution in this slice;
- overwriting existing immutable Workbench JavaScript or CSS assets.

## Acceptance gates

Before W1.2 may be promoted as shipped:

1. The endpoint accepts the canonical first-party serial definition and returns
   the exact same manifest as Python preflight and a compiled Harness.
2. Duplicate/missing/unknown fields, invalid UTF-8/Unicode/numbers/depth/size,
   query parameters, media type, and incomplete/timed-out bodies fail closed.
3. Unknown, ambiguous, duplicate, drifted, or wrapper-only Tool contracts fail
   without choosing or executing a handler.
4. Model, Tool, idempotency, store constructor, run/event/checkpoint/approval,
   network, process, and fault-injector spies prove zero execution/mutation.
5. A read-only preflight does not acquire or relax the runtime mutation gate and
   ordinary application create/status/history remain compatible.
6. Success/error responses are exact, bounded, detached, deterministic, and
   contain no literal plaintext, mutable run/call identity, approval
   fingerprint, idempotency key, checkpoint identity, execution lease,
   traceback, or arbitrary exception prose. The existing derived static
   `app_id` remains part of the W1.1 manifest.
7. The browser renders only server-owned results, exposes trust/no-sandbox and
   draft-only/no-execution boundaries, rejects non-round-trippable Unicode
   before fetch, separates authoritative rejection from an unverified transport
   outcome, and drops aborted or stale responses.
8. Editing draft B while draft A is pending invalidates A without requiring a
   second submission; A cannot display success, manifest, digest, or app ID.
9. Missing, wrong, or repeated bearer credentials and wrong or repeated Origin
   headers fail before parsing/preflight; only exact configured CORS origins or
   explicitly trusted loopback no-auth pass, and authentication failures leave
   every parser/compiler/Tool spy at zero.
10. A Workflow-only deployment has an empty Studio Tool registry and fails
    closed; canonical success configures an ordinary source-Tool Harness, and
    unavailable, ambiguous, or wrapper-only Tools never enter the registry.
11. Keyboard, focus, reduced-motion, narrow-screen, and empty/error/success
   states are exercised against the exact bundled W1.2 implementation assets.
12. Source tests, real browser fixture and journey, installed wheel,
   rebuilt-sdist wheel, release inventory, and mainland-source no-cache
   container workflow pass.
13. README/FOUNDATION/benchmark promotion occurs only after the exact
    implementation revision completes Hosted CI. A later promotion commit must
    complete its own Hosted CI.

Passing these gates does not establish saved Workflow authoring, activation,
execution from drafts, visual DAG editing, branches, parallelism, Agent nodes,
subflows, marketplace installation, tenant isolation, plugin honesty, sandbox,
live-provider quality, signed provenance, public deployment, or production
readiness.

## Promotion evidence

The W1.2 implementation is bound to
[`e3bc816`](https://github.com/syusama/sasori/commit/e3bc816c9d33febcc364e595a7480b475d181efb)
and [Hosted run 31391700342](https://github.com/syusama/sasori/actions/runs/31391700342).
All 20 non-tag jobs across five job families passed; the exact-tag-only release
bundle was correctly skipped on the ordinary `main` push. The six-platform
source matrix covered 414 deterministic tests, with five documented
platform/permission skips in the local Windows baseline. Installed-wheel and
rebuilt-sdist matrices passed; the accepted wheel was 252,158 bytes under the
256,000-byte limit and used release verifier v11 with
`sasori-source-tree-v8`.

Real Chrome covered 21 fixture cases across desktop and 390×844 reduced-motion
profiles, including keyboard/focus, stale-response, invalid-Unicode, exact
rejection, and unverified-transport behavior. Real-server Studio, Incident, and
typed Workflow journeys passed. The locked mainland-source no-cache Compose
gate completed Workflow `preflight → prepare → complete → after-restart` and
proved that preflight changed no durable run/event or action ledger, while the
approved action executed once and was not replayed after restart.

This evidence promotes only the transient static draft/preflight/manifest
preview described in this ADR. It is not evidence for a saved catalog, durable
draft lifecycle, activation, run-from-draft, DAG/parallel execution, Agent
nodes, subflows, sandboxing, exactly-once effects, signed provenance, public
deployment, or production readiness. The later documentation-promotion commit
must complete its own Hosted CI before the promotion process is closed.
