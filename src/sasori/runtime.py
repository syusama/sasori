"""Compatibility runtime backed by the external SQLite adapter by default."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from sasori_core.contracts import Event, Model, ModelStreamEvent, SkillSpec, Tool
from sasori_core.runtime import (
    ApprovalConflict,
    ApprovalMismatch,
    DuplicateToolCallError,
    Harness as CoreHarness,
    InjectedFault,
    MaxStepsExceeded,
    ModelCallError,
    ModelStreamProtocolError,
    ModelTimeoutError,
    RunBusy,
    RunCancelled,
    RunPaused,
    SasoriError,
    run_agent_loop,
)

from .sqlite_store import SQLiteStore


class Harness(CoreHarness):
    """Legacy façade preserving the in-memory SQLite default for ``sasori``."""

    def __init__(
        self,
        model: Model,
        tools: Sequence[Tool] = (),
        *,
        max_steps: int = 8,
        model_timeout: float = 30.0,
        tool_timeout: float = 30.0,
        event_sink: Callable[[Event], None] | None = None,
        model_stream_sink: Callable[[ModelStreamEvent], None] | None = None,
        store: SQLiteStore | None = None,
        fault_injector: Callable[[str], None] | None = None,
        skills: Sequence[SkillSpec] = (),
    ) -> None:
        super().__init__(
            model,
            tools,
            max_steps=max_steps,
            model_timeout=model_timeout,
            tool_timeout=tool_timeout,
            event_sink=event_sink,
            model_stream_sink=model_stream_sink,
            store=store,
            fault_injector=fault_injector,
            skills=skills,
            _store_factory=SQLiteStore,
        )


__all__ = [
    "ApprovalConflict",
    "ApprovalMismatch",
    "DuplicateToolCallError",
    "Harness",
    "InjectedFault",
    "MaxStepsExceeded",
    "ModelCallError",
    "ModelStreamProtocolError",
    "ModelTimeoutError",
    "RunBusy",
    "RunCancelled",
    "RunPaused",
    "SasoriError",
    "run_agent_loop",
]
