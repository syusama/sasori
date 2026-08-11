# sasori-core

The small, dependency-free mechanism at the center of Sasori.

```bash
pip install sasori-core
```

```python
import asyncio

from sasori_core import Harness, Message, ModelReply


class Echo:
    async def complete(self, messages, tools):
        return ModelReply(content=messages[-1].content)


async def main():
    with Harness(Echo()) as agent:
        result = await agent.run((Message("user", "one precise core"),))
        print(result.final_message.content)


asyncio.run(main())
```

`sasori-core` contains the immutable contracts, one single-agent Loop/Harness,
storage-neutral state ports, versioned public event/run projection, an explicit
non-durable ephemeral store, and deterministic offline test helpers. It does
not contain a provider SDK, SQLite, HTTP server, RAG, Workflow, plugin market,
or UI.

The Harness is the high-level admission and recovery API; every drive uses the
same exported low-level `run_agent_loop()` implementation. Complete-only models
remain the smallest contract. A model can also expose a provider-neutral,
bounded `complete_stream()` sequence:

```python
from sasori_core import ModelReply, ModelStreamEvent


class StreamingEcho:
    async def complete(self, messages, tools):
        raise AssertionError("the runtime prefers complete_stream")

    async def complete_stream(self, messages, tools):
        yield ModelStreamEvent("start")
        yield ModelStreamEvent("text_delta", delta=messages[-1].content)
        yield ModelStreamEvent(
            "done", reply=ModelReply(content=messages[-1].content)
        )
```

The grammar is `start -> deltas* -> exactly one done/error/aborted -> end`.
Partial tool-call deltas are transient and can never execute. Stream observer
failures cannot rewrite durable facts. `await harness.wait_for_idle()` waits
until admitted drives have unwound; it does not claim that a remote provider or
synchronous tool was forcibly stopped.

For SQLite durability, providers, CLI, HTTP, first-party tools, Workflow,
Memory, applications and the Workbench, install the `sasori` bundle after its
next split-package prerelease is published.

The recovery contract is step-boundary recovery, not exactly-once execution.
Side-effecting tools require idempotency or explicit manual recovery. Installed
Python entry points are trusted code, not a sandbox.

See the repository
[boundary decision](https://github.com/syusama/sasori/blob/main/docs/ADR-0018-SASORI-CORE-PACKAGE-BOUNDARY.md)
,
[Loop/stream/settlement decision](https://github.com/syusama/sasori/blob/main/docs/ADR-0019-CORE-LOOP-STREAM-SETTLEMENT.md),
and
[Pi/Proma benchmark](https://github.com/syusama/sasori/blob/main/docs/BENCHMARK-PI-PROMA.md).
