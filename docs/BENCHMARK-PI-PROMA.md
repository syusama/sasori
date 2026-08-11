# Sasori benchmark: Pi core and Proma product surface

This document records source-grounded design inputs. It is not a claim that an
upstream README feature is shipped by Sasori, nor permission to copy upstream
code or media.

## Locked research snapshots

| Project | Snapshot | License | How Sasori uses it |
|---|---|---|---|
| Pi | [`452923b`](https://github.com/earendil-works/pi/tree/452923b54a6c8b2f95b80157a8f6c7963f183101), 2026-08-11, packages 0.84.1 | MIT | Core boundaries, Loop/event/tool/session behavior; no TypeScript port |
| Proma | [`4cbde97`](https://github.com/proma-ai/Proma/tree/4cbde97d6361db1948fc738d1177b9be413b3295), 2026-08-11 | AGPL-3.0-only | Abstract product IA and interaction benchmark only; no source, style, copy, logo, or screenshot reuse |

Both upstream branches moved during the audit. These immutable commits, not a
floating `main`, define the comparison below.

## Core gap matrix

| Capability | Pi evidence | Sasori current baseline | Sasori target / acceptance |
|---|---|---|---|
| Canonical Loop | `packages/agent/src/agent-loop.ts` is the low-level path | Shipped: one exported `run_agent_loop()` under the executable Harness; core store is injected | Retain one path across Python, CLI, HTTP and Workbench adapters |
| Model stream | Unified provider stream protocol with deltas and terminal outcomes | Shipped in core: bounded `start -> deltas* -> done/error/aborted -> end`; current bundled providers may still aggregate upstream SSE | Add adapter-native transient deltas only with the shared terminal conformance; partial calls never execute |
| Incomplete calls | `stopReason=length` calls fail and do not execute | Already fail-closed | Retain regression for truncated and structurally invalid calls |
| Tool order | lookup → prepare → validate → before → abort → execute → after → result | Lookup/validation/effect/recovery exists | Add explicit hook contract; replacement args must be revalidated and immutable to observers |
| Tool failure | Most tool failures become error results | Already explicit | Preserve cancellation as a separate BaseException boundary |
| Tool parallelism | Completion events may finish out of order; result messages preserve source order | Serial, deterministic | Remain serial until approval/effect/cancel semantics for a mixed batch have an ADR |
| Steering/follow-up | Separate FIFO queues and insertion points | Not shipped | Add only after persistence scope and races are specified and tested |
| Finish/settle | Product session distinguishes `agent_end` and `agent_settled` | Shipped process boundary: durable terminal means finished; drive unwind means settled; `wait_for_idle()` observes no admitted drives | Keep transient idle separate from durable event truth; never claim forced remote cancellation |
| Harness truth | New Pi Harness exposes 22 operations that still raise `HarnessNotImplemented`; production uses old AgentSession | Sasori Harness is executable | Stable exports must never contain placeholder operations |
| Session backend | Memory/JSONL/SQLite conformance; SQLite lease/fencing; JSONL cross-process claim is weaker than comments | Shipped: storage-neutral port, non-durable ephemeral default, external SQLite lock and step recovery | Expand the same conformance over stale revision, call identity, effect ambiguity, cancellation and ownership |
| Core size | `pi-agent-core` 49 TS / ~12.4k lines; install closure includes provider SDKs through `pi-ai` | Independent zero-dependency `sasori-core` wheel; bundle depends on the exact same version | Keep the verified wheel below the provisional 128 KiB ceiling; no provider SDK/DB/HTTP/UI/RAG dependency |
| Offline tests | Large suite, but complete Windows run depends on hydrated model JSON and POSIX assumptions | Deterministic Python fakes; 488-test prior baseline | Offline core tests on Windows/Linux/macOS; provider/live tests remain supplemental |

Pi validation performed during the audit:

- focused agent loop/event/reducer/compaction/memory/JSONL suites: 293 tests
  passed at the core-equivalent snapshot;
- SQLite backend suites: 82 tests passed;
- the broader Windows checkout was not fully green because model catalog
  hydration was unavailable and several execution-environment tests assumed
  `/bin/bash` or symlink privileges. Sasori must not turn provider catalog
  hydration into a prerequisite for deterministic core tests.

## Product surface gap matrix

Proma's exact snapshot implements a dense desktop shell with project/session
navigation, central tabs, Chat and Agent workspaces, file/diff/preview panels,
Planning, Skills/MCP/Memory management, Settings, and a newly source-present
managed browser. The strengths are contextual density and keeping background
work visible beside conversation.

| Surface | Proma source reality | Sasori direction | Sasori acceptance advantage |
|---|---|---|---|
| Shell | Resizable three-column Electron desktop shell | Shipped candidate: original Red Sand Atelier three-column Web command center | Exact 360/390px responsive mode, keyboard/pointer separators, focus restoration and reduced-motion acceptance |
| Chat/Agent | Attachments, files, tools, models, task process in context | One conversation canvas backed by Sasori public events | Approval/recovery/effect-unknown visible from live and cold history through one reducer |
| Capability center | Skills, MCP, Memory are a full-screen view | Shipped read-only Workbench surface over the real app catalog for skills, tools, providers, plugins and evidenced MCP | Add install/update/revoke only after real Marketplace APIs, permissions and provenance exist |
| Planning | Todo, calendar and automation surfaces | Workflow Studio and durable saved workflow catalog | Saved/reopen/run/recover evidence, not only local UI state |
| Files/diff/preview | Side panel and split previews | Artifacts, evidence, workflow and trace inspectors | Immutable hashes, media/type verification and run binding |
| Managed browser | Source-present with URL policy, profiles, CDP actions and trace; no new tests | Future adapter, not a current claim | No shipped label before deterministic policy tests and real browser acceptance |
| Marketplace | Community marketplace is explicitly “coming soon” | Curated local metadata exists; public marketplace is planned | No marketplace claim until install/update/revoke/signature/provenance paths work |
| Accessibility | Reduced motion is partial; narrow-screen strategy is weak | WCAG 2.2 AA target | Keyboard flow, focus visibility, 360px layout and reduced-motion browser tests |
| E2E/screenshots | 68 test files found, no Playwright/Cypress E2E; README screenshots are partly older UI | Automated browser journey already exists | Exact-commit seeded captures plus screenshot provenance and visual regression |

## Screenshot evidence policy

Proma's repository contains real PNG screenshots, but several show older tab
and Skills/MCP layouts than the locked source. Sasori therefore treats a README
screenshot as evidence only when all of the following are recorded:

1. exact Sasori commit;
2. deterministic seed scenario and application binding;
3. actual local server and browser journey, never a static mock API;
4. viewport, theme and reduced-motion state;
5. capture script and browser acceptance result;
6. no third-party character art, logo, screenshot, or unlicensed font.

## Brand boundary

“Red Sand”, puppet threads, modular mechanisms and enduring art are original
metaphors for a composable Agent framework. Sasori is an independent open-source
project and is not affiliated with, endorsed by, or licensed by Naruto,
Masashi Kishimoto, Shueisha, TV Tokyo, Studio Pierrot, or their owners. The UI
and documentation use original mechanical-scorpion geometry and do not use
official character art, anime frames, logos or fonts.
