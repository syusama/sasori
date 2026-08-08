from __future__ import annotations

import argparse
import asyncio
import json
import math

from sasori import (
    AnthropicMessagesModel,
    Harness,
    Message,
    Model,
    OpenAIResponsesModel,
    Tool,
)


MARKER = "sasori-live-smoke"
FINAL = "sasori-live-smoke-ok"


def echo(text: str) -> str:
    return text


async def smoke(model: Model) -> None:
    tool = Tool("echo", echo, "Return the supplied smoke marker.", effect="read_only")
    with Harness(model, (tool,), max_steps=4) as harness:
        result = await harness.run(
            (
                Message(
                    "system",
                    f"Call echo exactly once with text {MARKER}, then reply exactly {FINAL}.",
                ),
                Message("user", "Run the live provider smoke now."),
            )
        )
    calls = [
        message
        for message in result.messages
        if message.role == "tool" and message.tool_name == "echo"
    ]
    if len(calls) != 1 or calls[0].content != MARKER:
        raise RuntimeError("provider did not complete exactly one expected tool call")
    if result.final_message is None or result.final_message.content.strip() != FINAL:
        raise RuntimeError("provider final response did not match the smoke contract")


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one secret-safe real-provider tool smoke.")
    parser.add_argument("--provider", required=True, choices=("openai", "anthropic"))
    parser.add_argument("--model", required=True)
    parser.add_argument("--timeout", type=float, default=60.0)
    options = parser.parse_args(arguments)
    model_name = options.model.strip()
    if not model_name:
        parser.error("--model cannot be empty")
    if not math.isfinite(options.timeout) or not 0 < options.timeout <= 600:
        parser.error("--timeout must be finite and between 0 and 600 seconds")
    model = (
        OpenAIResponsesModel(model_name, timeout=options.timeout)
        if options.provider == "openai"
        else AnthropicMessagesModel(model_name, timeout=options.timeout)
    )
    asyncio.run(smoke(model))
    print(json.dumps({"provider": options.provider, "status": "passed", "tool_calls": 1}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
