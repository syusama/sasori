# ADR-0005: Frozen MCP stdio host boundary

Status: accepted for the host-only `0.1.0.dev0` slice.

## Decision

Sasori supports MCP stdio through an administrator-frozen host adapter. It is
not an installed dynamic plugin entry point and it does not change the core
runtime. The host parses one bounded strict-JSON snapshot, constructs ordinary
`Tool` values, constructs an exact `PluginManifest`, and requires the existing
`validate_registration()` check to pass before a Harness can use them.

The snapshot freezes:

- an absolute executable argv, absolute cwd, and explicit environment sources;
- the total child deadline;
- the exact expected `initialize.result`;
- the complete normalized `tools/list.result`; and
- the administrator's effect decision for each tool.

Missing effect decisions default to `side_effecting`. MCP annotations and
descriptions never lower the effect. MCP tools are not classified idempotent in
this slice because the protocol does not prove Sasori's idempotency-key and
recovery contract.

The canonical snapshot SHA-256 is embedded in every tool revision. Any command,
environment source, handshake, schema, complete tool list, or effect change
therefore creates a different registration contract. The adapter never refreshes
or registers tools from a live server.

## Invocation

Each tool invocation starts one short-lived direct child with
`asyncio.create_subprocess_exec`. Model arguments are sent only inside JSON-RPC;
they never enter argv, cwd, or env. The child receives only explicitly frozen
environment values plus Windows `SYSTEMROOT` when required for process startup.
Parent PATH, proxy variables, provider keys, tokens, HOME, and credential state
are not inherited by default.

The adapter implements a deliberately small MCP stdio subset:

1. `initialize` request;
2. `notifications/initialized`;
3. one non-paginated `tools/list` request;
4. one `tools/call` request; and
5. direct-child EOF and process reaping.

The live initialize and complete tool-list results must match the frozen values
before `tools/call` is written. Only closed input objects whose properties are
all required and representable by Sasori's provider schema compiler are exposed.
Unsupported MCP schemas fail during host configuration, not during a run.

## Protocol and trust

Every message is one bounded UTF-8 JSON line. Batch messages, BOM, duplicate
keys, NaN/Infinity, partial EOF lines, wrong or duplicate IDs, unexpected
requests/notifications, unknown result fields, non-text result blocks, stderr
floods, late stdout, and oversized text fail closed. Stderr is drained only to
prevent deadlock and is never treated as JSON-RPC, an event, or model content.

Successful text is prefixed with `[UNTRUSTED MCP OUTPUT]`. Text that resembles a
Sasori approval, event, checkpoint, or effect decision remains ordinary tool
data. The child has no Harness, SQLiteStore, projection, event sink, approval,
or manual-recovery handle.

For a side-effecting MCP tool, the existing Harness persists approval and
dispatch intent before the handler starts the child. Denial therefore starts no
process and writes no RPC. After dispatch, handshake drift, timeout, cancellation,
process failure, JSON-RPC error, or `isError:true` is conservatively
`effect_unknown`; resume never starts a second child. Read-only failures are
ordinary explicit tool errors.

Timeout and cancellation close stdin and terminate, kill if needed, and reap the
direct child. This proves only that Sasori stopped waiting for and reclaimed the
direct process. It does not prove that descendants, remote calls, network effects,
or previously performed external effects stopped.

## No sandbox claim

`create_subprocess_exec`, fixed argv, a minimal environment, byte limits, and
reaping protect the control-plane contract. They do not isolate the child. The
MCP server still runs with the same OS-user authority and may access host files,
network, processes, secrets it can discover itself, and descendants. The dynamic
manifest therefore uses `trusted_process`, its effective access remains
`FULL HOST PROCESS PRIVILEGES`, and permissions are disclosure rather than
enforcement.

## Deferred

Pagination, optional/open/nested schemas, resources, images, audio,
structuredContent, progress/logging, roots, prompts, sampling, elicitation,
long-lived MCP sessions, background servers, network transports, container
isolation, automatic snapshot refresh, and installed dynamic entry points are
deferred until a concrete server requires them and supplies deterministic tests.
