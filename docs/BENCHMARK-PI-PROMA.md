# Sasori benchmark: Pi core and Proma product surface

This document records source-grounded design inputs. It does not imply that an
upstream README feature is shipped by Sasori, and it is not permission to copy
upstream code or media.

## Locked research snapshots

| Project | Snapshot | License | How Sasori uses it |
|---|---|---|---|
| Pi | [`452923b`](https://github.com/earendil-works/pi/tree/452923b54a6c8b2f95b80157a8f6c7963f183101), 2026-08-11, packages 0.84.1 | MIT | Core boundaries, Loop/event/tool/session behavior; no TypeScript port |
| Proma | [`73e9d01`](https://github.com/proma-ai/Proma/tree/73e9d014b56dfda7554011bc02cf8ee5af2c5493), 2026-08-12 | AGPL-3.0-only | Information architecture and interaction benchmark only; no source, CSS, copy, logo, screenshot, or asset reuse |

Both upstream branches can move. These immutable commits, not a floating
`main`, define the comparison below.

## Core gap matrix

| Capability | Pi evidence | Sasori current baseline | Sasori acceptance rule |
|---|---|---|---|
| Canonical Loop | `packages/agent/src/agent-loop.ts` is the low-level path | One exported `run_agent_loop()` under an executable Harness; storage is injected | Retain one path across Python, CLI, HTTP, Workflow, and Workbench |
| Model stream | Unified provider stream with deltas and terminal outcomes | Bounded `start -> deltas* -> done/error/aborted -> end`; terminal replies are captured before observers; bundle providers may aggregate upstream SSE | Partial calls never execute; every stream reaches exactly one terminal outcome; observer/provider mutation cannot rewrite the accepted reply |
| Incomplete calls | Length-truncated calls fail and do not execute | Fail-closed parser and Tool dispatch | Keep malformed, truncated, oversized, duplicate, interrupted, and cancelled conformance cases |
| Tool order | Lookup, prepare, validate, hooks, execute, result | Lookup, validation, effect, approval, and recovery are explicit; observers receive detached deeply immutable arguments | Replacement arguments must be revalidated and immutable to observers |
| Tool failure | Most Tool failures become error results | Explicit Tool-result errors | Cancellation remains a separate `BaseException` boundary |
| Parallel Tool calls | Completion may finish out of order while results preserve source order | Serial and deterministic | Remain serial until mixed effect/approval/cancellation semantics have an ADR and acceptance suite |
| Steering/follow-up | Separate queues and insertion points | Not shipped | Add only after persistence scope and race behavior are specified |
| Finish/settle | Product session distinguishes agent end and settlement | Durable terminal means finished; drive unwind means settled; `wait_for_idle()` observes no admitted drives | Never confuse transient idle with durable event truth or forced remote cancellation |
| Harness truth | The audited Pi Harness surface still exposed placeholder operations while the product used another session layer | Sasori Harness is executable | Stable exports never contain placeholder operations |
| Session backend | Memory/JSONL/SQLite variants with different ownership strength | Storage-neutral port, ephemeral default, external SQLite lock, step recovery | Share conformance for revision, call identity, effect ambiguity, cancellation, and ownership |
| Core size | `pi-agent-core` is a larger TypeScript package with provider closure through `pi-ai` | Independent zero-dependency `sasori-core` wheel; bundle pins the exact same version | No Provider SDK, DB, HTTP, UI, RAG, or marketplace dependency in Core |
| Offline tests | Broad suite, with environment-dependent catalog/POSIX cases | Deterministic Python fakes and platform-specific skips | Core gates stay offline across Windows, Linux, and macOS; live providers supplement them |

Pi validation during the audit found strong Loop/event/reducer/session coverage,
plus broader tests whose Windows result depended on model-catalog hydration,
`/bin/bash`, or symlink privileges. Sasori treats those as portability lessons:
provider discovery must not be a prerequisite for deterministic Core tests.

## Product surface gap matrix

Proma's locked snapshot implements a dense desktop shell with project/session
navigation, Chat and Agent workspaces, files/diff/preview, Planning,
Skills/MCP/Memory management, Settings, and a managed-browser surface. Its main
strength is contextual density: background work remains visible beside the
conversation.

| Surface | Proma source reality | Sasori current direction | Sasori acceptance advantage |
|---|---|---|---|
| Shell | Resizable three-column Electron desktop shell | Independent professional-light three-column Web Workbench | Exact 360/390px responsive mode, keyboard/pointer separators, focus restoration, reduced-motion acceptance |
| Chat/Agent | Attachments, files, tools, models, task process in context | One task/conversation canvas backed by Sasori public events | Approval, recovery, and effect ambiguity remain visible in live and cold history through one reducer |
| Capability center | Skills, MCP, and Memory form a broad management surface | Read-only projection of loaded Skills, Tools, MCP transports, Providers, Plugins, and effective trust | Install/update/revoke waits for real market APIs, permissions, and provenance |
| Planning | Todo, calendar, and automation surfaces | Workflow Studio and durable saved Workflow Catalog | Save, reopen, conflict, reconciliation, and recovery evidence—not browser-local state |
| Files/diff/preview | Side panels and split preview | Artifact, evidence, Workflow, and trace inspectors | Immutable digest, media/type validation, and run binding |
| Managed browser | Source-present browser capabilities and policy | Future adapter, not a current claim | No shipped label before deterministic policy tests and real browser acceptance |
| Marketplace | Community marketplace remains planned | Curated local metadata and scaffolding | No public-market claim before install/update/revoke/signature/provenance work |
| Accessibility | Desktop-first product with limited narrow strategy | WCAG 2.2 AA target | Keyboard flow, focus visibility, exact 360px layout, reduced motion, no horizontal result overflow |
| E2E/screenshots | Real screenshots exist but some drift from the locked source | Automated browser acceptance and real server journey | Runtime-commit-bound captures with pixels, hashes, browser, scenario, and semantic checks |

## What Sasori takes from Proma

Sasori adopts the product lessons, not the implementation:

1. keep application/history navigation, active work, and evidence visible at
   the same time on desktop;
2. make task progress, output, files, and capabilities discoverable without
   forcing users through modal stacks;
3. switch the same three surfaces through explicit bottom navigation on narrow
   screens;
4. distinguish authoring, validation, execution, and history instead of
   presenting every action as chat;
5. keep the design dense enough for professional use while retaining a clear
   reading hierarchy.

The current implementation is dependency-free static HTML/CSS/JavaScript. It
uses warm neutral surfaces, editorial typography, restrained cinnabar focus,
precise rules, and explicit trust/status language. It intentionally avoids
purple gradients, cyber glow, anime scenes, puppet strings, particle fields,
and theatrical feature copy.

## Screenshot evidence policy

A README screenshot is accepted only when all of the following are recorded:

1. exact Sasori runtime commit;
2. deterministic scenario and application binding;
3. actual local server and browser journey, never a static mock result;
4. requested viewport, actual pixels, theme, and reduced-motion state;
5. browser identity plus acceptance result;
6. content bytes and SHA-256;
7. no third-party character art or branding inside the product UI.

Separately supplied README brand media is inventoried with dimensions, bytes,
digest, placement, and an independent-project disclaimer. It is not accepted
as product UI evidence.

## Brand and license boundary

The Sasori name origin may appear in a short project introduction or Logo, but
anime lore, puppet-thread decoration, and theatrical copy are not
product-interface themes. The owner-supplied square image is retained as a
repository brand asset and used as the small Workbench brand mark/favicon; the
separate owner-supplied poster is the README hero. Neither asset is used as a
Workbench background, login screen, navigation theme, or decorative product
illustration.

Proma is AGPL-3.0-only at the locked snapshot. Sasori reuses no Proma source,
CSS, wording, Logo, screenshots, or assets. Sasori is an independent open-source
project and does not claim affiliation with or endorsement by Naruto or its
rights holders.
