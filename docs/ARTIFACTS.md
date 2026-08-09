# Durable artifacts

Status: experimental single-node extension for trusted Python hosts.

`sasori_artifacts` registers bounded immutable bytes against a real Sasori run.
It keeps blob storage outside the Loop while placing the public reference and
`artifact.available` event on the run's durable cursor.

## Python publisher

```python
from pathlib import Path

from sasori import Event, Message, SQLiteStore
from sasori_artifacts import ArtifactStore

run_store = SQLiteStore("runs.sqlite3")
run_store.start(
    "run-report",
    (Message("user", "build the report"),),
    Event("run.started", "run-report", 0),
)

artifacts = ArtifactStore(run_store, Path("artifacts"))
ref = artifacts.put(
    "run-report",
    b'{"status":"ready"}',
    declared_filename="report.json",
    declared_media_type="application/json",  # recorded as untrusted provenance
)

print(ref.artifact_id, ref.content_sha256, ref.created_seq)
assert artifacts.get("run-report", ref.artifact_id).content == b'{"status":"ready"}'

artifacts.close()
run_store.close()
```

The run must already exist. The store accepts only `bytes`, limits one artifact
to 16 MiB and one run to 128 associations, and derives an idempotent ID when
the caller does not provide one. Repeating the same run/content/filename/type
returns the same reference and does not append another event. Reusing an ID
for different immutable facts raises `ArtifactConflict`.

The declared MIME type is never trusted for public serving. Sasori recognizes
strict UTF-8 JSON, inert UTF-8 text, and a small set of binary signatures;
everything else becomes `application/octet-stream`. Detection is classification,
not proof that arbitrary active content is safe to execute.

## Server configuration

```powershell
sasori-server --host 127.0.0.1 --port 8080 `
  --db .\runs.sqlite3 `
  --artifact-root .\artifacts `
  --app incident=sasori_apps.incident:create_harness `
  --token-file .\sasori-token.txt
```

`SASORI_ARTIFACT_ROOT` is the environment equivalent. When omitted for a
file-backed database, the default is the database path with `.artifacts` as its
suffix. An in-memory run database requires an explicit artifact root.

An operator may add `--publish-final-artifact` to enable the bundled,
host-derived Markdown result policy. The default is off. The policy is useful
for the Workbench artifact journey, but it is not a claim that every tool
automatically emits a managed artifact. `SASORI_PUBLISH_FINAL_ARTIFACT=1` is
the strict environment equivalent (`0` disables it; other values are rejected).

## Read API

```text
GET  /v1/runs/{run_id}/artifacts
GET  /v1/runs/{run_id}/artifacts/{artifact_id}/content
HEAD /v1/runs/{run_id}/artifacts/{artifact_id}/content
```

All `/v1/*` authentication rules still apply. Artifact tokens are never placed
in URLs. Content uses `attachment`, `private, no-store`, `nosniff`, a digest
ETag, `X-Sasori-Content-SHA256`, and single-range support. A verified text/JSON
preview in Workbench is fetched with the same bearer header and inserted with
`textContent`; download uses an ephemeral browser Blob URL.

Error boundaries:

| Condition | Result |
| --- | --- |
| Unknown run | `404 run_not_found` |
| Unknown or cross-run artifact | `404 artifact_not_found` |
| Missing, linked, resized, or hash-mismatched blob | `503 artifact_integrity_failed` |
| Invalid artifact metadata | `422 invalid_artifact` |
| Immutable ID conflict | `409 artifact_conflict` |
| Invalid, repeated, multi, or unsatisfiable Range | `416 range_not_satisfiable` |

This is a local single-instance boundary. It does not provide user/tenant
ownership, uploads, signed URLs, delete, retention, garbage collection,
network-filesystem safety, or sandboxing. See
[ADR-0010](ADR-0010-ARTIFACT-REF-BOUNDARY.md) for the transaction and trust
decision.
