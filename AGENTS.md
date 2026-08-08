# Sasori repository instructions

## Scope

- Keep `sasori` Python-first and keep the core runtime small enough to read end to end.
- The core owns only contracts, the single-agent loop, event projection, and the test harness.
- Provider SDKs, persistence, HTTP servers, RAG, multi-agent orchestration, UI, and marketplace code stay outside the core.
- Reuse the standard library and existing project patterns before adding dependencies or abstractions.
- Do not claim planned, experimental, or upstream behavior as shipped Sasori behavior.

## Docker and package sources

- Docker image references must use a mainland-China-accessible registry by default, for example `docker.m.daocloud.io/library/python:3.12-slim`.
- Debian/Ubuntu package installation in images must switch to a mainland mirror such as Aliyun before `apt-get update`.
- Python installation in images must use a configurable index whose default is `https://pypi.tuna.tsinghua.edu.cn/simple`.
- Keep versions and hashes locked. A mirror changes transport, never dependency integrity.
- A Docker change is incomplete until it builds through the configured mainland sources and a real container workflow passes.

## Runtime invariants

- One runtime path serves Python, CLI, HTTP, and future UI adapters.
- Public events are a versioned projection, not a serialization of mutable internal state.
- A truncated or structurally invalid tool call must never execute.
- Tool exceptions become explicit tool-result errors; cancellation is never swallowed.
- Cancellation is cooperative. Do not claim that an arbitrary remote model or synchronous tool stopped unless verified.
- Checkpoint/resume is step-boundary recovery, not exactly-once execution. Side-effecting tools require an idempotency key or an explicit manual-recovery policy.
- Third-party Python entry points are trusted installed code, not a sandbox.

## Changes and tests

- Change the smallest shared boundary that solves the real problem.
- Every non-trivial branch, loop, parser, or recovery rule needs one runnable regression check.
- Tests use deterministic fake models/tools by default; real-provider smoke tests supplement them.
- Provider adapters must pass one shared conformance suite, including malformed output, timeout, rate limit, interrupted stream, duplicate call, and cancellation cases.
- Golden traces compare stable semantic fields. Timestamps, provider prose, and other documented nondeterministic fields are excluded.
- Run `python -m unittest discover -s tests -v` before handing off core changes.
- Do not commit test artifacts, caches, temporary files, or agent session metadata.

## Collaboration

- Specs and acceptance tests are the handoff contract between planning, implementation, frontend, and verification agents.
- Use isolated worktrees for parallel writers. Only one owner integrates public-contract changes.
- A model-generated plan or passing self-test is evidence to review, not authority. The integrator reruns checks and inspects the diff.
- Changes to golden traces, public events, recovery semantics, or plugin permissions require an explicit decision record.
