# Sasori

[![CI](https://github.com/syusama/sasori/actions/workflows/ci.yml/badge.svg)](https://github.com/syusama/sasori/actions/workflows/ci.yml)

> A small, inspectable Python runtime for tool-using AI agents.

Sasori ships one async `Harness` for a serial, single-agent tool loop. The core supports Python 3.11-3.13, uses only the standard library, projects versioned semantic events, and can durably pause or resume through `sqlite3`. Two stdlib-only provider adapters, Python/CLI/HTTP entry points, three fixed first-party application compositions, and the bundled Workbench use that same Harness path. The trusted-plugin loader and curated catalog are explicit support surfaces; no bundled application dynamically loads an external entry point. Provider adapters can opt into fail-closed upstream SSE aggregation; public token streaming, embeddings/vector retrieval, multi-agent orchestration, untrusted-plugin isolation, and a central marketplace remain later gates rather than claimed features.

## Runnable example

```python
import asyncio

from sasori import Harness, Message, ModelReply, Tool, ToolCall


def greet(name):
    return f"hello, {name}"


class ExampleModel:
    async def complete(self, messages, tools):
        if messages[-1].role == "user":
            return ModelReply(tool_calls=(ToolCall("greet-1", "greet", {"name": "Sasori"}),))
        return ModelReply(content=f"Tool said: {messages[-1].content}")


async def main():
    with Harness(
        ExampleModel(), (Tool("greet", greet, effect="read_only"),)
    ) as harness:
        result = await harness.run((Message("user", "Say hello."),))
    print(result.final_message.content)
    print([event.type for event in result.events])


asyncio.run(main())
```

Synchronous tools run through `asyncio.to_thread`, so they do not block the event loop. Cancellation and timeouts stop Sasori from waiting and request cancellation; they cannot forcibly stop a worker thread or async code that deliberately swallows `CancelledError`.

## Trust semantics decision record

- Every `Tool` declares `effect="read_only"`, `"idempotent"`, or `"side_effecting"`; the safe default is `side_effecting`. Non-read-only tools require an explicit immutable `tool_revision`. An idempotent tool must also provide a deterministic `idempotency_key(arguments)`, and its handler must accept and externally enforce Sasori's reserved keyword-only `idempotency_key`; model arguments cannot override it.
- Non-read-only calls durably emit `approval.requested` and pause. `resolve_approval()` binds the decision to a SHA-256 fingerprint of the run, accepted model step, call ordinal, tool name, canonical arguments, and tool revision. An approved call pauses as `tool_contract_changed` instead of invoking a different revision. A denial becomes a model-visible tool error.
- `SQLiteStore(path)` atomically commits the run revision, recoverable checkpoint, accepted reply/tool ledger updates, and append-only events under `BEGIN IMMEDIATE`. The event sink runs only after commit and is best-effort; consumers recover gaps through `stored_events(run_id, after_seq)` and deduplicate by `(run_id, seq)`.
- Dispatch intent is committed before invoking a tool. A committed result is reused. Ambiguous read-only work may retry; idempotent work may retry only with the same key; an ordinary side effect stops in `effect_unknown`. `resolve_effect()` requires the exact fingerprint and an audit reason for explicit `record_result`, `fail`, or `retry`. This is step-boundary recovery, **not exactly-once execution**.
- Caller cancellation durably terminates the run as `cancelled`, emits `run.cancelled`, and propagates `CancelledError`; `resume()` cannot later turn that run into `completed`. An ambiguous effect remains auditable, but a cancelled run cannot retry it.
- G1 supports one writer/process/connection and file stores request SQLite's exclusive locking mode. Revision CAS rejects stale drivers, but there is no lease, heartbeat, or multi-worker executor; WAL does not create one.
- The Python plugin loader executes only explicitly enabled, installed `trusted_process` plugins when an application elects to use that loader. "Installed identity" currently means matching entry-point and distribution metadata; it does not bind imported package bytes to a reviewed wheel digest. Bundled first-party applications compose their registrations directly. Importing an entry point has the Sasori process and OS user's full privileges; manifest permissions are disclosure and upgrade-review metadata, not runtime enforcement. `container` and `supervised_process` are static manifest modes only.
- The first-party workspace plugin rejects static path escapes supplied by the model, but it does not resist another local actor replacing a checked path with a symlink or junction before use. Its containment is bounded tool behavior inside a full-host trusted process, not a sandbox.

The recovery suite includes real child-process termination after dispatch intent, after an external effect returns but before its result commit, and after final commit. It does not claim protection from disk corruption, power-loss behavior outside SQLite's guarantees, or external exactly-once execution.

Pass the same file-backed `SQLiteStore` to a new `Harness` and call `await harness.resume(run_id)` after a process restart. The default in-memory store keeps the same durable state machine for short-lived runs but cannot survive process exit.

Use `Harness` as a synchronous context manager or call `close()` after its last
await. It closes only the default store it created; a supplied `SQLiteStore`
remains caller-owned. Closing a Harness concurrently with an active
`run()`/`resume()` is unsupported: await the operation before closing it.

## Trusted web fetch plugin

`com.sasori.web-fetch` provides one read-only `fetch_url` tool. Its trusted
entry point reads only `SASORI_WEB_ALLOWED_HOSTS`, a comma-separated exact-host
allowlist. A bare hostname permits only port 443; a non-default port must appear
explicitly as `host:port`. Wildcards, implicit subdomains, IP literals, Unicode
and punycode hostnames are not supported. Empty configuration denies every URL.

The tool performs HTTPS GET only, resolves and validates every address, connects
to one validated IP while retaining the original Host/TLS identity, and repeats
the checks after every redirect. Returned text begins with
`[UNTRUSTED EXTERNAL CONTENT]`; it is model input data, not system instructions.
This remains bounded behavior inside a full-host `trusted_process`, not a
sandbox. See [ADR-0002](https://github.com/syusama/sasori/blob/main/docs/ADR-0002-WEB-FETCH-BOUNDARY.md).

## Trusted SQLite RAG plugin

`com.sasori.rag-sqlite` owns a separate local SQLite/FTS5 database. Set
`SASORI_RAG_DB` to an existing-parent file path; otherwise the trusted entry
point uses `.sasori-rag.sqlite3` in the current directory. It never opens the
Harness `SQLiteStore` or core run database.

`index_text` is idempotent revision `1`: the Harness derives its key from
normalized collection/source/text arguments, requires approval, and injects
the reserved key only after approval. One `BEGIN IMMEDIATE` transaction
atomically replaces a source's chunks and FTS rows, advances revisions, and
stores the replay result. Replay succeeds only while that source generation is
still current; a superseded request fails closed. `search_documents` is
read-only and returns bounded JSON evidence with source/chunk IDs, hashes,
offsets, revisions, scores, and text. It does not create an absent database,
and it opens an existing database in SQLite read-only/query-only mode after
validating the exact owned schema. The bundled `grounded-search` skill may call
only search and requires those citations; it is data, not another loop or an
answer model. See
[ADR-0003](https://github.com/syusama/sasori/blob/main/docs/ADR-0003-RAG-SQLITE-BOUNDARY.md).

## First-party applications and Workbench

The distribution includes three inspectable application compositions; it does
not create three runtime engines:

| Application | Actual composition | Availability boundary |
|---|---|---|
| `incident` | deterministic model, `inspect_incident`, approved `record_action` | runnable demo when its action-log path is configured |
| `research` | configured provider, allowlisted web fetch, SQLite/FTS5 indexing and citation-preserving retrieval | requires provider and plugin configuration |
| `developer` | configured provider, bounded workspace tools, state-bound local Git, optional frozen MCP tools | requires a workspace and Git; MCP is opt-in |

The local server freezes an `app_id → Harness` mapping at startup. Those
Harnesses share one `SQLiteStore` and one mutation gate; every run still calls
the same `Harness.run()` / `resume()` → `_drive()` path. Each run stores an
immutable application binding. A legacy unbound run is accepted only when the
server has exactly one configured application; a multi-application server
returns `409 app_binding_missing` rather than guessing.

`GET /v1/apps` reports fixed worker/skill metadata, actual Harness tools,
availability, effects/revisions, and declared first-party permission intent. The bundled
no-build Workbench consumes that endpoint plus the durable run/event APIs. It
provides application selection, run history, task input, REST/SSE progress,
approval/denial, explicit resume, effect recovery, timeline and capability
views, responsive navigation, keyboard focus, and reduced-motion behavior.
Untrusted model/tool content is rendered as text. Plugin metadata explicitly
shows `FULL HOST PROCESS PRIVILEGES` and `enforced=false`; it is not a dynamic-loader
attestation or sandbox.
See [ADR-0006](https://github.com/syusama/sasori/blob/main/docs/ADR-0006-MULTI-APP-RUN-BINDING.md).

## Providers

`OpenAIResponsesModel` maps the OpenAI Responses API and `AnthropicMessagesModel` maps Anthropic Messages. Both use `urllib`, reject redirects and malformed/oversized JSON or SSE, expose typed transport/protocol errors, disable parallel tool calls, and preserve vendor continuation blocks through `provider_state`. Passing `stream=True` consumes upstream SSE but returns only after a complete, validated vendor terminal event; deltas are neither public events nor durable checkpoints. OpenAI reasoning items and Anthropic thinking/redacted-thinking blocks are persisted but never projected into public events.

Provider tools require concrete Python annotations because one shared compiler produces the strict JSON schema and validates returned arguments locally:

```python
from sasori import Harness, Message, OpenAIResponsesModel, Tool


def weather(city: str, units: str | None) -> str:
    return f"clear in {city} ({units or 'metric'})"


model = OpenAIResponsesModel("YOUR_CONFIGURED_MODEL")  # reads OPENAI_API_KEY
with Harness(model, (Tool("weather", weather, effect="read_only"),)) as harness:
    ...  # await harness.run(...)
```

Use `AnthropicMessagesModel("YOUR_CONFIGURED_MODEL")` with `ANTHROPIC_API_KEY` for Anthropic. API keys are read from constructor arguments or environment variables, never written to events or exceptions. The deterministic local wire suite covers both adapters; a real-provider smoke is intentionally not claimed unless the corresponding credentials and model names are safely configured and the two-turn tool workflow actually passes.

## CLI

An application is trusted installed Python code exposing `module:create_harness`, where the factory accepts the supplied `SQLiteStore` and returns a `Harness`. Automatic plugin discovery is deliberately absent from this boundary.

```powershell
sasori --db .\runs.sqlite3 --app sasori_apps.incident:create_harness run "checkout latency is high" --run-id incident-1
sasori --db .\runs.sqlite3 status incident-1
sasori --db .\runs.sqlite3 events incident-1 --after 0
sasori --db .\runs.sqlite3 --app sasori_apps.incident:create_harness approval incident-1 <fingerprint> --approve
sasori --db .\runs.sqlite3 --app sasori_apps.incident:create_harness resume incident-1
```

Add global `--json` for one JSON object per command; `events` emits one cursor-bearing object per line. Exit code `3` means the run durably paused for an explicit next action. Approval and effect resolution never resume implicitly.

## Local HTTP/SSE and Docker

`sasori-server` is a single-process, single-owner local service. It serves the Workbench at `/`, application metadata, cursor-paginated run history, run/resume/status/approval/manual-effect operations, durable event JSON/SSE, `/healthz`, and `/readyz`. One active mutation is allowed across all enabled applications; a second receives `503 runtime_busy` instead of being silently queued. Approval and manual effect decisions persist `pause_reason=resume_required` and never resume implicitly. It is not a public multi-tenant server, scheduler, or horizontally scalable deployment.

Multiple applications use repeated `--app id=module:factory` arguments:

```powershell
sasori-server --host 127.0.0.1 --port 8080 `
  --db .\runs.sqlite3 `
  --app incident=sasori_apps.incident:create_harness `
  --app research=sasori_apps.research:create_harness `
  --app developer=sasori_apps.developer:create_harness `
  --trusted-loopback-no-auth
```

An application whose factory cannot load is reported as unavailable; it is not
silently replaced by Incident. For normal use, prefer a bearer token even on
loopback. The no-auth switch is restricted to an explicit loopback bind.

The Compose delivery uses the digest-pinned DaoCloud Python base, hash-pinned build requirements from the Tsinghua PyPI mirror, a non-root user, read-only root filesystem, dropped capabilities, `no-new-privileges`, and bounded resources. Supply a local bearer-token file without adding it to Git:

```powershell
$env:SASORI_TOKEN_FILE = "C:\secure-local-path\sasori-token"
$env:SASORI_PORT = "18888"
docker compose up -d --build --wait
```

The real acceptance path is `POST /v1/runs` → durable approval → explicit resume → final → SSE cursor reconnect → container restart → unchanged final/effect count. Container health alone is not acceptance. File-backed stores hold a cross-process owner lock; a second server using the same database fails startup. Network filesystems, replicas, failover, public TLS, and arbitrary untrusted tool sandboxing remain out of scope.

Run the deterministic regression suite from the repository root:

```powershell
python -m unittest discover -s tests -v
```

The accepted architecture, trust boundaries, and later gates live in [docs/FOUNDATION.md](https://github.com/syusama/sasori/blob/main/docs/FOUNDATION.md).

## Release, security, and license

The local release verifier, clean-tag requirements, fresh-environment matrix,
artifact/SBOM/provenance boundaries, and container gate are documented in
[docs/RELEASE.md](https://github.com/syusama/sasori/blob/main/docs/RELEASE.md). Report vulnerabilities through the private
path in [SECURITY.md](https://github.com/syusama/sasori/blob/main/SECURITY.md).

Sasori code and first-party assets use the [MIT License](https://github.com/syusama/sasori/blob/main/LICENSE), subject to
the material and license boundaries recorded in
[THIRD_PARTY_NOTICES.md](https://github.com/syusama/sasori/blob/main/THIRD_PARTY_NOTICES.md).
