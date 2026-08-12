<h1 align="center">Sasori</h1>

<p align="center"><strong>A small Python agent runtime. A complete framework when you need one.</strong></p>

<p align="center">
  <a href="https://github.com/syusama/sasori/blob/main/README.md"><strong>English</strong></a> ·
  <a href="https://github.com/syusama/sasori/blob/main/README_zh.md">简体中文</a> ·
  <a href="https://github.com/syusama/sasori/blob/main/README_ja.md">日本語</a> ·
  <a href="https://github.com/syusama/sasori/blob/main/README_ko.md">한국어</a>
</p>

<p align="center">
  <a href="https://github.com/syusama/sasori/actions/workflows/ci.yml"><img src="https://github.com/syusama/sasori/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/Python-3.11%20%7C%203.12%20%7C%203.13-3776AB?logo=python&logoColor=white" alt="Python 3.11 to 3.13">
  <img src="https://img.shields.io/badge/sasori--core-runtime%20dependencies%200-2F6F5E" alt="Zero core runtime dependencies">
  <img src="https://img.shields.io/badge/tests-536%20deterministic-B83A2D" alt="536 deterministic tests">
  <a href="https://github.com/syusama/sasori/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-242424" alt="MIT License"></a>
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/syusama/sasori/main/docs/assets/sasori-banner.png" alt="Sasori project hero artwork" width="520">
</p>

Sasori is a Python-first framework for tool-using agents. Its smallest useful
form is a dependency-free Loop/Harness that can be read end to end. When the
product needs more, the same runtime can add providers, SQLite, plugins,
Workflows, Memory, artifacts, HTTP/SSE, and a responsive Workbench—without
introducing a second execution engine behind the UI.

The name carries a deliberate design metaphor: as Sasori controls an adaptable
arsenal of puppets, developers should be able to compose models, Tools, Skills,
Memory, and Workflows with the same freedom and precision. The ambition is not
to theme the product after a character; it is to treat every Agent as an
engineered work of art—modular, expressive, dependable, and built to endure.

> **Current boundary:** `0.1.0.dev1` is a verified, single-machine,
> single-owner prerelease candidate. It is not yet a public multi-tenant
> control plane, distributed executor, untrusted-code sandbox, or public
> plugin market. Install it from a checkout while package publication is on
> hold.

## Why Sasori

Agent frameworks often begin with an elegant loop and become difficult to
reason about once providers, tools, recovery, APIs, and product surfaces are
added. Sasori keeps those responsibilities explicit:

- **Small by default.** `sasori-core` has zero runtime dependencies and owns
  only contracts, one agent loop, one Harness, public event projection, and
  deterministic test helpers.
- **One runtime path.** Python, CLI, HTTP/SSE, Workflow, and Workbench adapters
  use the same Harness and durable event contract.
- **Effects are reviewable.** Tool calls declare their effect class. Approval,
  execution, explicit resume, and manual recovery remain separate facts.
- **Recovery is honest.** Checkpoints recover at step boundaries; they do not
  pretend arbitrary external side effects are exactly-once.
- **Product quality is testable.** The Workbench is the real runtime client,
  not a concept mockup or a second set of business rules.

## Two distributions, one runtime

| | `sasori-core` | `sasori` |
|---|---|---|
| Import | `sasori_core` | `sasori` plus optional top-level modules |
| Use it for | Embedding the canonical single-agent runtime | Building a batteries-included Agent application |
| Owns | Contracts, Loop/Harness, versioned projection, `RunStore`, ephemeral store, test helpers | The exact same-version core plus SQLite, providers, CLI, HTTP/SSE, plugins, Workflow, Memory, artifacts, apps, Workbench, and market scaffolding |
| Runtime dependencies | **0** | Exactly `sasori-core==0.1.0.dev1`; first-party features otherwise prefer the standard library |
| Deliberately outside core | Provider SDKs, persistence, HTTP, RAG, multi-agent, UI, marketplace | No duplicate Loop and no shadow Harness |

The package identities are explicit:

```text
PyPI distribution: sasori-core       Python import: sasori_core
PyPI distribution: sasori            Python import: sasori
```

Install the current candidate from the repository:

```bash
# Smallest runtime
python -m pip install ./packages/sasori-core

# Complete framework
python -m pip install ./packages/sasori-core
python -m pip install --no-deps .
```

## A complete agent in 30 seconds

```python
import asyncio

from sasori_core import Harness, Message, ModelReply, Tool, ToolCall


def inspect(topic: str) -> str:
    return f"verified evidence for {topic}"


class DemoModel:
    async def complete(self, messages, tools):
        if messages[-1].role == "user":
            return ModelReply(
                tool_calls=(ToolCall("inspect-1", "inspect", {"topic": "Sasori"}),)
            )
        return ModelReply(content=f"Grounded: {messages[-1].content}")


async def main():
    with Harness(
        DemoModel(),
        (Tool("inspect", inspect, effect="read_only"),),
    ) as agent:
        result = await agent.run((Message("user", "Inspect the runtime"),))
    print(result.final_message.content)


asyncio.run(main())
```

Complete-only models are the minimal contract. Streaming is optional and
provider-neutral. A truncated, oversized, invalid, or incomplete tool call
fails closed and never executes.

## One line of control

```mermaid
flowchart LR
    PY["Python"] --> H["Harness"]
    CLI["CLI"] --> H
    HTTP["HTTP / SSE"] --> H
    UI["Workbench"] --> H
    WF["Workflow"] --> H
    H --> LOOP["single agent loop"]
    LOOP --> MODEL["Model contract"]
    LOOP --> TOOL["Tool contract"]
    LOOP --> EVENT["versioned events"]
    LOOP --> STORE["RunStore port"]
    MODEL -. optional .-> PROVIDERS["OpenAI / Anthropic"]
    TOOL -. optional .-> PLUGINS["Workspace / Web / RAG / Git / MCP"]
    STORE -. optional .-> SQLITE["SQLite"]
```

The solid path is `sasori-core`. Everything dotted is replaceable and stays
outside core.

## The Workbench is real

These images were captured from the real Sasori server at runtime commit
[`71993de`](https://github.com/syusama/sasori/commit/71993de377a837c85c6cba5bcbf83a36228a1dc2).
The browser journeys use SQLite, approval, explicit resume, two audited effects,
cold-history reconstruction, artifacts, capability projection, strict Workflow
preflight, and durable Catalog save. Exact dimensions, byte counts, SHA-256
digests, browser version, and capture scenarios are recorded in the
[screenshot manifest](docs/assets/screenshots-manifest.json).

<p align="center">
  <img src="https://raw.githubusercontent.com/syusama/sasori/main/docs/assets/screenshots/workbench-completed-1600x1000-71993de.jpg" alt="Sasori completed Workflow and capability inspector" width="100%">
</p>

<p align="center"><sub>A completed typed Workflow with verified output, definition identity, and effective capability disclosure.</sub></p>

<p align="center">
  <img src="https://raw.githubusercontent.com/syusama/sasori/main/docs/assets/screenshots/workbench-workflow-studio-1600x1000-71993de.jpg" alt="Sasori Workflow Studio" width="100%">
</p>

<p align="center"><sub>Workflow Studio saves immutable definitions with strong-ETag CAS and performs server-authoritative preflight with zero model calls and zero tool dispatches.</sub></p>

<table>
  <tr>
    <td width="50%" align="center"><img src="https://raw.githubusercontent.com/syusama/sasori/main/docs/assets/screenshots/workbench-mobile-390x844-71993de.jpg" alt="Sasori mobile task workspace" width="390"></td>
    <td width="50%" align="center"><img src="https://raw.githubusercontent.com/syusama/sasori/main/docs/assets/screenshots/workbench-mobile-inspector-390x844-71993de.jpg" alt="Sasori mobile capability inspector" width="390"></td>
  </tr>
  <tr>
    <td align="center"><sub>Task workspace · exact 390×844 CSS viewport</sub></td>
    <td align="center"><sub>Capability inspector · exact 390×844 CSS viewport</sub></td>
  </tr>
</table>

Proma is a benchmark for information architecture, workspace density, and
three-pane interaction. Sasori's no-build frontend, CSS, copy, logo, screenshots,
and assets are independently implemented against Sasori's own contracts.

## What is included today

| Surface | Delivered in this candidate |
|---|---|
| Core | Zero-dependency contracts, Loop/Harness, strict streaming settlement, approval/recovery, `RunStore`, ephemeral storage, stable public projection, deterministic fakes |
| Durability | SQLite revisions, events, checkpoints, restart recovery, CAS, and single-owner admission |
| Providers | Standard-library OpenAI Responses and Anthropic Messages adapters with shared wire-conformance tests |
| Context & Memory | Bounded context plus a separate, fixed-scope, immutable-revision SQLite Memory extension |
| Tools & plugins | Workspace, allowlisted HTTPS, SQLite/FTS5 RAG, local Git, frozen MCP stdio, trusted entry-point discovery, permission disclosure |
| Workflow | Strict static serial definitions, zero-execution preflight, immutable saved revisions, CAS conflict reconciliation, one Harness execution path |
| Product | Python API, CLI, HTTP/SSE, Incident/Research/Developer apps, artifacts, responsive Workbench, market scaffolding |

Deep contracts live in [Foundation](docs/FOUNDATION.md),
[HTTP API](docs/HTTP_API.md), [Providers](docs/PROVIDERS.md),
[Workflow](docs/WORKFLOWS.md), [Memory](docs/MEMORY.md),
[Artifacts](docs/ARTIFACTS.md), and the
[Pi/Proma benchmark](docs/BENCHMARK-PI-PROMA.md).

## Runtime guarantees

- Public events are versioned semantic projections, never dumps of mutable
  internal state.
- Every Tool is `read_only`, `idempotent`, or `side_effecting`; unsafe work
  crosses explicit approval and resume boundaries.
- Tool exceptions become explicit Tool results. Cancellation is propagated and
  never swallowed.
- Checkpoint/resume is step-boundary recovery. Side-effecting Tools require an
  idempotency key or an explicit manual-recovery policy.
- Installed Python entry points are trusted host code, not a sandbox.
- Mutable inputs cannot rewrite durable arguments, approvals, retries, or
  another store adapter's view.

## Evidence before claims

The current runtime snapshot has passed:

- `536` deterministic `unittest` checks; `5` Windows symlink cases skip when
  the required OS privilege is unavailable;
- `31 / 31` real Chrome Workbench cases at 1600×1000, 390×844, and 360×800,
  including reduced-motion and narrow structured-result checks;
- real-server browser journeys covering approval, explicit resume, exactly two
  audited effects, cold history, artifacts, typed Workflow, and saved Catalog;
- original-wheel, rebuilt-sdist, exact bundle/core, and installed-distribution
  verification;
- mainland-source Docker build and a real non-root container workflow.

The repository does not treat a generated plan, self-test, screenshot, or
upstream README as release authority. Runnable acceptance evidence is the gate.

## Benchmarked, not copied

- **Pi** — readable loop structure and disciplined tool/event ordering; Sasori
  keeps a zero-dependency Python core, executable Harness, strict terminal
  settlement, and explicit recovery boundaries.
- **Proma** — product density and workspace discoverability; Sasori uses only
  architectural and interaction lessons, not Proma's AGPL source or assets.
- **LeAgent / ToFu** — useful product breadth and runtime ideas; Sasori tightens
  effect ambiguity, projection ownership, package boundaries, and evidence.

See [Pi / Proma](docs/BENCHMARK-PI-PROMA.md),
[LeAgent / ToFu](docs/BENCHMARK-LEAGENT-TOFU.md), and
[third-party notices](THIRD_PARTY_NOTICES.md) for fixed commits and license
boundaries.

## Roadmap — not shipped yet

- Signed plugin provenance, compatibility policy, and a governed public market;
- tenant identity, authorization, quotas, durable queues, and distributed workers;
- isolated untrusted Tool execution with explicit CPU, memory, filesystem, and
  egress policies;
- DAG/parallel Workflow and multi-agent orchestration after effect,
  cancellation, approval, and replay semantics are proved;
- team workspaces and digital employees on the same canonical runtime.

## Name and affiliation

The name is inspired by Sasori, the puppet master from *Naruto*: precise
craftsmanship, modular mechanisms, and a preference for work that endures. That
reference is limited to the project name, this short origin note, and the
owner-supplied project brand asset; it is not the Workbench's visual theme.

Sasori is an independent open-source project and is not affiliated with,
authorized by, sponsored by, or endorsed by *Naruto*, Masashi Kishimoto,
Shueisha, TV Tokyo, Studio Pierrot, or their rights holders. The supplied logo
is presented as project branding. This repository does not represent it
as official media or assert ownership of third-party rights in it.

## License and contribution

Sasori code is released under the [MIT License](LICENSE). Third-party plugins
retain their own licenses and run as trusted code. Security boundaries are in
[SECURITY.md](SECURITY.md). Changes to public events, recovery semantics,
golden traces, or plugin permissions should include a decision record and
runnable regression evidence.

**Start small. Add only what the product needs. Keep every important action inspectable.**
