# ADR-0001: minimal trusted plugin boundary

Status: accepted for the first plugin slice on 2026-08-07.

## Decision

Sasori exposes static `PluginRegistration`, `SkillSpec`, and `WorkerSpec`
contracts. The Python plugin loader discovers installed plugins through the
standard `sasori.plugins` Python entry-point group and imports them only when an
application elects to use the loader, the plugin ID is explicitly enabled, the
installed entry point's group, name, value, distribution name, and version
metadata match the strict manifest, and the requested permissions have explicit
grants. This metadata check does not bind installed package files to a reviewed
wheel digest. Bundled first-party applications in this slice compose their
registrations directly; they do not pass through the dynamic loader.

The only executable mode in this slice is `trusted_process`.
`entry_point.load()` executes installed Python with the Sasori process and OS
user's full privileges. The permission manifest is disclosure and upgrade
review data; it is not a sandbox. User-visible disclosure must therefore show
`FULL HOST PROCESS PRIVILEGES` even when requested intent is narrower.
`container` and `supervised_process` remain parseable only for static manifest
and upgrade review; they cannot be loaded or reported as enforced.

Plugin tools remain ordinary `Tool` values. They use the existing Harness,
approval fingerprint, effect, `tool_revision`, checkpoint, recovery, and public
event path. Plugins cannot register events, approvals, checkpoints, middleware,
or another model/tool loop.

Upgrade review requires approval for every tool-effect change, including a
change from `side_effecting` to `idempotent` or `read_only`. An effect change
must also use a different `tool_revision`; the upgrade diff rejects an unchanged
revision instead of treating the new effect label as the old tool contract.

The first-party `com.sasori.workspace` plugin binds four bounded text tools to
one configured root. `write_text` is side-effecting revision `1`; approval is
performed by the existing Harness before the handler is invoked. Its path
validation and resolved-root checks reject static escape paths supplied by the
model. They do not resist another local actor or process replacing a checked
path component with a symlink or junction before use. This check/use race is
inside a `trusted_process` with full host privileges: workspace containment is
bounded tool behavior, not a sandbox.

## Deferred

This decision does not implement or claim subprocess/container isolation,
runtime permission enforcement, plugin installation or generation switching,
installed-file/wheel-digest binding, an external-plugin host application, hot
unload, a central market, lifecycle hooks, background workers, or remote disable.
Those require separate decisions and runnable evidence.
