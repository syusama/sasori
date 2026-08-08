# ADR-0002: bounded trusted web fetch

Status: accepted for the first web-fetch plugin slice on 2026-08-07.

## Decision

Sasori ships `com.sasori.web-fetch` as one read-only HTTPS GET tool using the
existing `PluginRegistration`, `Tool`, and Harness path. Its entry point reads
only the explicit `SASORI_WEB_ALLOWED_HOSTS` allowlist; an empty value denies
all requests. Host matches are exact, ports other than 443 require an exact
`host:port` entry, and wildcard/subdomain inheritance is not implemented.

Every request and redirect is reparsed, allowlisted, and resolved. All returned
addresses must be globally routable. The connection uses one validated numeric
address directly while preserving the original Host header, TLS SNI, and
certificate hostname verification, so the HTTP layer cannot perform a second
name resolution after the check. Environment proxies and cookies are not used.
Response status, redirects, framing, encoding, textual MIME type, UTF-8, bytes,
output characters, connect/read timeouts, and one monotonic total deadline are
bounded. There is no automatic retry.

The platform resolver is not cancellable from Python. A timed-out lookup may
finish later in a daemon thread, but it cannot open a socket; a process-wide
four-slot bound prevents stalled lookups from creating unbounded threads, and
additional lookups fail closed within their own total deadline.

Output is explicitly headed `[UNTRUSTED EXTERNAL CONTENT]` and records the
final URL, normalized content type, and truncation fact. External text remains
untrusted model data and never becomes a system instruction.

The plugin still executes as `trusted_process`. Import has the Sasori process
and OS user's full privileges, and manifest permissions remain disclosure and
upgrade-review metadata rather than runtime enforcement. These network checks
are the behavior of this one tool, not an isolation boundary for installed
Python code.

## Deferred

HEAD, POST, uploads, file downloads, caching, hidden retries, robots processing,
JavaScript execution, OAuth, credentials, a crawler, and marketplace behavior
are not implemented. Adding any of them requires a separate contract and
runnable trust-boundary evidence.
