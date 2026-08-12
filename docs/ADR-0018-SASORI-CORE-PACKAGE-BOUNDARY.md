# ADR-0018: `sasori-core` package boundary

- Status: Accepted
- Date: 2026-08-11
- Decision owners: Sasori maintainers
- Affects: distribution names, public imports, runtime/store dependency direction,
  public projection composition, release verification

## Context

The original `sasori` distribution contains the runtime, SQLite persistence,
provider adapters, HTTP/CLI adapters, plugins, workflows, applications, market
metadata, and the Workbench. Its `Harness` imports and creates `SQLiteStore`
directly, and its base projection imports SQLite types and validates a
Workflow-specific extension. Calling that bundle a lightweight core would be a
packaging claim rather than an enforceable architecture boundary.

Pi was reviewed at commit
[`452923b`](https://github.com/earendil-works/pi/tree/452923b54a6c8b2f95b80157a8f6c7963f183101)
(MIT, package version 0.84.1). Its low-level loop discipline, provider/runtime
separation, session backend conformance, steering/follow-up semantics, and
finished-versus-settled distinction are useful references. Its unfinished
public `AgentHarness`, duplicate old/new session paths, provider-heavy install
closure, and product-layer expansion inside agent-core are not Sasori targets.

Proma was separately reviewed for product UI, not core architecture. Its
AGPL-3.0-only implementation must not be copied into Sasori.

## Decision

### Names

| Role | Distribution | Python import |
|---|---|---|
| Small canonical runtime | `sasori-core` | `sasori_core` |
| Compatible batteries-included SDK | `sasori` | `sasori` |

Python identifiers cannot contain a hyphen, so installation and import use
different spellings:

```bash
pip install sasori-core
```

```python
from sasori_core import Harness, Message, Tool
```

The `sasori` distribution keeps its existing name, `sasori` import, three CLI
commands, `sasori.plugins` entry-point group, HTTP shape, event shape, and
recovery semantics. It is a compatibility façade and product bundle, not the
canonical core implementation.

### Core ownership

`sasori_core` owns only:

- immutable contracts used by the single-agent runtime;
- storage-neutral run/call/event state contracts;
- one executable single-agent Loop/Harness path;
- the versioned base event and run projection;
- deterministic, offline test helpers;
- an explicitly non-durable process-local store for zero-dependency scripts
  and conformance tests.

`sasori_core` must not import:

- `sasori` or any `sasori_*` product/extension package;
- `sqlite3` or another persistence implementation;
- provider SDKs or provider wire adapters;
- HTTP, CLI, RAG, Memory, Workflow, orchestration, marketplace, or UI code.

The process-local `EphemeralRunStore` is not a durability claim. Durable
checkpoint/resume requires an external `RunStore` adapter.

### Dependency direction

```text
sasori_core
    ↑
SQLite / providers / plugin SDK / workflow / memory / artifacts
    ↑
apps / CLI / HTTP server
    ↑
Workbench
```

All Python, CLI, HTTP, and future UI execution adapters call the same Harness
runtime. A second Loop or Session implementation is not permitted.

### Compatibility

During the `0.1.x` migration:

- `sasori.contracts` re-exports `sasori_core.contracts`;
- `sasori.runtime.Harness` subclasses the canonical core Harness and changes
  only the legacy default store from ephemeral state to in-memory SQLite;
- `sasori.SQLiteStore`, providers, plugin APIs, app loading, CLI, and server
  remain bundle exports;
- no runtime deprecation warning is emitted yet;
- stable event, run projection, approval, cancellation, and recovery fields do
  not change as part of the package move.

### Projection extensions

The base event/run projection belongs to `sasori_core` and reads only a
`RunViewSource` protocol. Workflow-specific fields and semantic validation
belong to `sasori_flow`. Extension composition must preserve the bounded,
JSON-safe, fail-closed public contract. Moving a validator must not alter the
existing HTTP representation; a semantic golden comparison is required.

Public runtime identity follows that same representability boundary. The
Harness validates `run_id` and optional `app_id` before drive admission, model
invocation, observer delivery, or store mutation. A runtime adapter therefore
cannot create durable state that the versioned public projection cannot name.

### Recovery and effects

Package movement does not strengthen the documented recovery guarantee:

- checkpoints are step-boundary recovery, not exactly-once execution;
- truncated or structurally invalid tool calls never execute;
- tool exceptions become explicit tool-result errors;
- `asyncio.CancelledError` is not swallowed;
- cancellation is cooperative;
- side-effecting calls need an idempotency key or explicit manual recovery;
- installed entry points remain trusted code, not a sandbox.

## Rejected alternatives

### Rename the existing `sasori` project to `sasori-core`

Rejected because it would rename the current wheel/sdist, break TestPyPI
publisher identity and release evidence, remove the umbrella name, and retain
all current coupling under a misleading label.

### Use `sasori.core` as the only new import

Rejected because two distributions would own files inside one regular
`sasori/` package, making installation and uninstallation brittle.

### Publish every extension as a separate wheel immediately

Rejected. Physical packages are created only when an independent dependency
closure, consumer, release cadence, or security boundary justifies them.

### Copy Pi or Proma implementation code

Rejected. Sasori learns from public contracts and verified behavior while
retaining an independent Python implementation. Proma is AGPL-3.0-only. If a
future change copies or translates Pi MIT code, it must add exact file/commit
attribution to `THIRD_PARTY_NOTICES.md` before merge.

## Migration gates

1. Introduce `sasori_core`, `RunStore`, `RunViewSource`, and the ephemeral
   implementation; make old imports compatibility façades.
2. Prove core import isolation and run the same lifecycle semantics against
   ephemeral and SQLite stores.
3. Move Workflow extension validation out of base projection without changing
   semantic golden fields.
4. Add the independently buildable `sasori-core` wheel/sdist and exact bundle
   dependency in the next immutable prerelease version.
5. Rebuild release, SBOM, TestPyPI, Docker, and clean-install verification for
   both distributions before uploading.

The pre-boundary `sasori==0.1.0.dev0` candidate is not reused for the split.
No tag or TestPyPI upload may claim that old artifact contains the new boundary.

## Acceptance

- `import sasori_core` loads no SQLite, HTTP, provider, plugin, workflow, or UI
  module.
- `sasori_core` has no third-party runtime dependency.
- one deterministic tool loop runs with `EphemeralRunStore`.
- the shared store conformance suite passes for ephemeral and SQLite adapters.
- invalid public run/application identities fail before drive or store mutation.
- all legacy `sasori` imports and CLI/HTTP/browser contracts continue to pass.
- `python -m unittest discover -s tests -v` passes.
- public projection/golden changes require a separate ADR.
- the independent core and bundle artifacts pass wheel, sdist, clean-venv,
  mainland-source container, and exact TestPyPI round-trip verification.

## Consequences

The repository temporarily ships a compatibility façade and a new canonical
namespace. That small duplication at the import boundary is deliberate; the
Loop itself exists only once. The release pipeline becomes a two-artifact
contract, but a core-only consumer can install a readable, offline-testable,
zero-dependency runtime without providers, SQLite, HTTP, or Web assets.
