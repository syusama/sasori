# Security policy

## Supported versions

Sasori has not published a stable release. Until the first tagged release, only
the current `main` candidate is considered for security fixes; older commits and
local forks are unsupported. A release-specific support window will be added
before a stable version is published.

## Reporting a vulnerability

Prefer a private GitHub Security Advisory draft at
<https://github.com/syusama/sasori/security/advisories/new>. If private
reporting is unavailable, ask the maintainers for a private contact channel
without including exploit details, credentials, or user data in a public issue.

Include the affected revision/version, deployment mode, minimal reproduction,
impact, and any known mitigation. Redact API keys, bearer tokens, cookies,
database contents, MCP environment values, absolute private paths, and provider
request/response bodies. There is no guaranteed response SLA during the
pre-release phase.

## Security boundaries

- Installed Python entry points and `trusted_process` plugins are trusted code
  with the Sasori process and OS user's full privileges. Manifest permissions
  are disclosure and review metadata, not a sandbox.
- MCP stdio children run with the configured executable and the same OS-user
  authority. The frozen snapshot validates configuration identity; it does not
  isolate an untrusted server.
- Workspace path checks, web allowlists, approval fingerprints, idempotency
  keys, and the SQLite owner lock narrow specific operations. They do not make
  the host a multi-tenant or hostile-code boundary.
- The bundled HTTP server is a single-owner local service. Public deployment
  requires an independently maintained TLS reverse proxy, authentication,
  request limits, network policy, secret management, backups, monitoring, and
  process isolation. Query-string bearer tokens are not supported.
- Cancellation is cooperative. Sasori can stop waiting and request cancellation
  but cannot prove that a remote provider, synchronous thread, child descendant,
  or already-started external side effect stopped.
- Checkpoint/resume is step-boundary recovery, not exactly-once execution.
  Side-effecting tools need an externally enforced idempotency key or explicit
  manual recovery.

Never use the public CPython TLS fixture in `tests/` as a production credential.
It is intentionally public test material and is documented in
`THIRD_PARTY_NOTICES.md`.
