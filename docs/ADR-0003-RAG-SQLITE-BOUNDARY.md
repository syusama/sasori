# ADR-0003: deterministic local SQLite RAG boundary

Status: accepted for the first SQLite RAG plugin slice on 2026-08-07.

## Decision

Sasori ships `com.sasori.rag-sqlite` as a trusted installed plugin with two
ordinary Harness tools and no worker. `index_text` is idempotent revision `1`
and requires the existing Harness approval flow. Its deterministic host key is
the SHA-256 of canonical NFC text with normalized newlines plus strict
collection and source IDs. `search_documents` is read-only.

The plugin owns the file selected by `SASORI_RAG_DB`, or
`.sasori-rag.sqlite3` in the current directory by default. Its SQLite
application ID and schema version reject a core or unrelated database. Each
tool call opens one short connection and closes it explicitly; no connection,
thread, cache, scheduler, or state is shared with the core `SQLiteStore`.
Only indexing may initialize an empty database. Search returns an empty result
when the path is absent; an existing database is opened in SQLite read-only and
query-only modes. Existing schema objects and columns are validated and never
silently recreated when missing or incompatible. Canonical DDL for all five
owned tables, FTS shadow columns, and the complete owned-object set are checked;
extra triggers or schema objects fail closed.

Text is normalized and split by fixed character size and overlap. Source and
chunk SHA-256 values, deterministic chunk IDs, ordinals, and normalized-text
offsets are persisted. One `BEGIN IMMEDIATE` transaction replaces every old
chunk and FTS row for the source, advances collection/source revisions, and
stores the idempotency request/result. A failure before commit rolls the whole
generation back. A committed key replays its stored result after restart only
while that source generation is still current; a superseded generation or key
reuse with another request fails closed. Because the key is derived from the
request, the first contract cannot distinguish a delayed retry from a new
A-to-B-to-A restore intent.

FTS5 is mandatory. Missing FTS5 raises an explicit unsupported error rather
than changing retrieval semantics. Search binds every SQL value and compiles
the user query to quoted literal alphanumeric tokens joined by `AND`, so FTS
operators and SQL punctuation are never executable input. Results are bounded
JSON evidence, not generated answers, and include collection/source revisions,
source/chunk IDs and hashes, offsets, BM25 score, and source text.

The `com.sasori.rag-sqlite/grounded-search` SkillSpec is static data. It can
reference only `search_documents`, requires source/chunk/hash citations, and
does not alter tool effects, approvals, recovery, or the one Harness loop.

This remains `trusted_process` installed Python with full Sasori-process and
OS-user privileges. Filesystem permissions are disclosure and upgrade-review
metadata, not runtime sandbox enforcement. The validation and transaction
rules constrain these two handlers only.

## Deferred

Embeddings, vector databases, file parsers, crawlers, answer generation,
reranking, multi-tenant storage, background indexing, workers, and marketplace
behavior are not implemented. Restoring an earlier identical source generation
requires a later public request discriminator or expected-revision contract.
