from __future__ import annotations

import json
import os
from pathlib import Path

from sasori import Harness, Message, ModelReply, SQLiteStore, Tool, ToolCall


class IncidentModel:
    async def complete(
        self, messages: tuple[Message, ...], tools: tuple[Tool, ...]
    ) -> ModelReply:
        results = [message for message in messages if message.role == "tool"]
        if not results:
            summary = next(
                message.content for message in reversed(messages) if message.role == "user"
            )[:500]
            return ModelReply(
                tool_calls=(
                    ToolCall("inspect-1", "inspect_incident", {"summary": summary}),
                )
            )
        if len(results) == 1:
            return ModelReply(
                tool_calls=(
                    ToolCall(
                        "record-1",
                        "record_action",
                        {"summary": f"Operator review: {results[-1].content}"[:500]},
                    ),
                )
            )
        return ModelReply(content=f"Incident action recorded: {results[-1].content}")


def inspect_incident(summary: str) -> str:
    normalized = " ".join(summary.split())
    return f"diagnostic captured for {normalized or 'unspecified incident'}"


def record_action(summary: str) -> str:
    path = Path(os.environ.get("SASORI_ACTION_LOG", "sasori-actions.jsonl"))
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"summary": summary}, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return summary


def create_harness(store: SQLiteStore) -> Harness:
    return Harness(
        IncidentModel(),
        (
            Tool(
                "inspect_incident",
                inspect_incident,
                "Capture a deterministic diagnostic summary.",
                effect="read_only",
            ),
            Tool(
                "record_action",
                record_action,
                "Append an operator-approved incident action to the local audit log.",
                effect="side_effecting",
                tool_revision="1",
            ),
        ),
        store=store,
    )


__all__ = ["IncidentModel", "create_harness", "inspect_incident", "record_action"]
