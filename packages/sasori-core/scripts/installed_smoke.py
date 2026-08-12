from __future__ import annotations

import asyncio
import importlib.metadata
import sys
from pathlib import Path

from sasori_core import (
    Harness,
    Message,
    ModelReply,
    Tool,
    ToolCall,
    ToolExecutionContext,
)


class _Model:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return ModelReply(tool_calls=(ToolCall("smoke-1", "double", {"value": 6}),))
        if messages[-1].content != "12":
            raise RuntimeError("installed core tool result is invalid")
        return ModelReply(content="core-ok")


async def _run() -> None:
    progress = []

    def double(
        value: int, *, tool_context: ToolExecutionContext
    ) -> int:
        if not tool_context.report_progress({"phase": "double", "value": value}):
            raise RuntimeError("installed core progress was not accepted")
        return value * 2

    with Harness(
        _Model(),
        (Tool("double", double, effect="read_only"),),
        tool_progress_sink=progress.append,
    ) as harness:
        result = await harness.run((Message("user", "double six"),), run_id="core-smoke")
    if result.final_message.content != "core-ok":
        raise RuntimeError("installed core final result is invalid")
    if (
        len(progress) != 1
        or progress[0].sequence != 1
        or dict(progress[0].data) != {"phase": "double", "value": 6}
        or any(event.type == "tool.progress" for event in result.events)
    ):
        raise RuntimeError("installed core progress contract is invalid")


def main() -> int:
    distribution = importlib.metadata.distribution("sasori-core")
    if distribution.metadata["Name"] != "sasori-core" or distribution.requires:
        raise RuntimeError("installed core metadata is invalid")
    location = Path(distribution.locate_file("")).resolve(strict=True)
    prefix = Path(sys.prefix).resolve(strict=True)
    if not location.is_relative_to(prefix):
        raise RuntimeError("installed core resolved outside the consumer environment")
    forbidden = [
        name
        for name in sys.modules
        if name == "sqlite3"
        or name == "sasori"
        or name.startswith("sasori.")
        or name.startswith("sasori_web")
    ]
    if forbidden:
        raise RuntimeError(f"installed core imported forbidden adapters: {forbidden}")
    asyncio.run(_run())
    print("installed sasori-core smoke: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
