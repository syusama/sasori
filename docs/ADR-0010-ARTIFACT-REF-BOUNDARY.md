# ADR-0010: Artifact references are immutable run-scoped extensions

- Status: accepted
- Date: 2026-08-09

## Decision

Sasori provides durable artifacts through the dependency-free
`sasori_artifacts` extension. Artifact bytes remain outside the runtime core in
an immutable content-addressed blob root. Artifact metadata and the matching
`artifact.available` event are committed inside the existing run SQLite
transaction.

This decision does not add an artifact field to `Message`, `ModelReply`,
`RunResult`, checkpoints, or the single-agent Loop. A trusted Python host
publishes bytes explicitly after it has a real `run_id`; Sasori does not scan
tool output, guess that a string is a path, or register arbitrary workspace
files automatically.

The public `ArtifactRef` version 1 contains only stable facts:

```json
{
  "version": 1,
  "artifact_id": "artifact-8cbb...",
  "run_id": "run-123",
  "content_sha256": "64 lowercase hexadecimal characters",
  "size_bytes": 19427,
  "filename": "report.txt",
  "media_type": "text/plain; charset=utf-8",
  "created_seq": 17
}
```

It never contains an absolute path, storage key, temporary path, signed URL,
bearer token, provider state, caller-declared MIME type, or preview policy.
`filename` is bounded display/download metadata and never participates in blob
addressing. `media_type` is a conservative server detection; uncertain bytes
become `application/octet-stream`.

## Write ordering and atomic visibility

Artifact registration uses the following order:

1. validate the run ID, filename, declared metadata, byte type, and 16 MiB
   limit before touching storage;
2. hash the complete bytes and write a same-filesystem temporary file under the
   artifact root;
3. flush and `fsync()` the temporary file;
4. finalize it to `blobs/sha256/<prefix>/<digest>` with a no-overwrite hard
   link;
5. if the digest target already exists, open and verify its exact size and
   SHA-256 instead of overwriting it;
6. begin `BEGIN IMMEDIATE` in the run database;
7. verify the run, immutable ID idempotency, and the per-run limit of 128;
8. append `artifact.available` and obtain its real run event sequence;
9. insert the immutable artifact row with a foreign key to the same
   `(run_id, seq)`; and
10. commit the event and metadata together.

This is atomic visibility, not a false claim that SQLite and the filesystem
share one transaction. A failure before blob finalize exposes nothing. A
database rollback after blob finalize may leave an unassociated
content-addressed orphan, but no public list, event, or download route can
discover it. Event insertion followed by metadata failure rolls back both. A
committed metadata row can therefore never point to a partial blob created by
this publisher.

P0 has no delete, retention, or garbage-collection API. It makes no promise
that unassociated blobs are retained forever or already reclaimed. A future GC
requires its own concurrency/recovery decision.

## Read and authorization boundary

Every read is scoped by both route values:

```sql
SELECT * FROM artifacts WHERE run_id = ? AND artifact_id = ?
```

The current bearer token authenticates one Sasori instance. Sasori does not yet
have accounts, users, organizations, or tenants, so this is explicitly
**instance authentication plus exact run association**, not tenant isolation.
Unknown and cross-run artifact IDs return the same `404 artifact_not_found`.
There is no global artifact route and no lookup or download by digest.

Before an HTTP success status or content header is sent, Sasori opens the
derived blob path, rejects links/reparse points and non-regular files, checks
the opened descriptor with `fstat()`, reads from that same descriptor, and
verifies exact size and SHA-256. Missing or modified bytes return
`503 artifact_integrity_failed` without partial content. Platforms without an
atomic no-follow primitive receive the available lstat/open/same-file checks;
this is not described as hostile-filesystem sandboxing.

Content is always served as an attachment. Only a single byte range is
accepted. Repeated or multi-range headers, invalid units, zero-length ranges,
and unsatisfiable ranges return `416` with `Content-Range: bytes */<size>`.
The Workbench may fetch verified UTF-8 text or JSON and render it through
`textContent`; HTML/SVG/PDF/image inline execution is not part of P0.

## Public event and terminal semantics

`artifact.available` uses the existing event envelope version 1 because the
envelope fields and delivery rules have not changed. Its `data.artifact` is the
same stable ArtifactRef projection. Event consumers already accept unknown
well-formed version-1 types and preserve the durable `(run_id, seq)` cursor.

`run.completed` settles the Loop. It does not assert that no run-scoped
extension can append a later durable event. A trusted host may publish an
artifact after the terminal event. An SSE connection that has already closed
is not a perpetual subscription; cursor reconnect, the artifact list endpoint,
and cold Workbench reopen are the recovery paths. `RunResult.events` is the
Loop's return-time trace, not a snapshot of all future extension events.

The optional `--publish-final-artifact` server policy materializes one Markdown
copy of a completed run's final message. It is disabled by default, deterministic,
idempotent across restart, and described as host-derived output—not as an
artifact emitted by the model or tool. Runs without that policy preserve their
existing golden trace and event count.

## Trust boundary

`ArtifactStore.put()` and the owner-thread publisher are trusted installed
Python code. They are not a sandbox or an upload endpoint. Plugin manifests
that disclose an `artifacts:` logical permission do not automatically receive
a publisher, run identity, filesystem permission, or HTTP grant.

The P0 API intentionally omits HTTP upload, mutable tags, delete, global
enumeration, signed sharing links, user grants, versions, and multi-agent
scratchpads. Those concerns require identity, quota, lifecycle, and recovery
contracts beyond ArtifactRef.

## Rejected alternatives

### Store metadata in a second artifact SQLite database

Rejected because two databases cannot atomically bind a run, its durable event
cursor, and artifact metadata. Startup reconciliation is not a substitute for
a single transaction fact.

### Put blob bytes in events or checkpoints

Rejected because it inflates the core recovery path, duplicates immutable
bytes, and makes every adapter pay for an optional product concern.

### Use `os.replace()` for digest targets

Rejected because it can overwrite a previously registered content-addressed
claim. Existing targets are verified and reused or the operation fails closed.

### Inline browser preview based on filename or declared MIME

Rejected because those values are untrusted. P0 preview is authenticated fetch
plus inert text rendering for conservatively detected UTF-8 text/JSON only.
