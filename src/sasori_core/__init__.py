"""Source-checkout bridge to the canonical ``packages/sasori-core`` source."""

from pathlib import Path

_CANONICAL = (
    Path(__file__).resolve().parents[2]
    / "packages"
    / "sasori-core"
    / "src"
    / "sasori_core"
)
if not _CANONICAL.is_dir():
    raise ImportError("canonical sasori_core source directory is missing")
__path__.append(str(_CANONICAL))

from .contracts import (  # noqa: E402
    ApprovalRequest,
    Event,
    MAX_APP_ID_BYTES,
    MAX_RUN_ID_BYTES,
    MAX_TOOL_CALL_ID_BYTES,
    MAX_TOOL_PROGRESS_EVENT_BYTES,
    MAX_TOOL_PROGRESS_EVENTS,
    MAX_TOOL_PROGRESS_TOTAL_BYTES,
    Message,
    Model,
    ModelReply,
    ModelStreamEvent,
    ModelStreamEventType,
    RunResult,
    SkillSpec,
    StreamingModel,
    Tool,
    ToolCall,
    ToolEffect,
    ToolExecutionContext,
    ToolProgressEvent,
    is_valid_app_id,
    is_valid_run_id,
    is_valid_tool_call_id,
    validate_run_id,
)
from .runtime import (  # noqa: E402
    ApprovalConflict,
    ApprovalMismatch,
    DuplicateToolCallError,
    Harness,
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
from .projection import (  # noqa: E402
    ProjectionIntegrityError,
    event_projection,
    run_list_projection,
    run_projection,
)
from .store import (  # noqa: E402
    CallRecord,
    ConcurrentRunError,
    EphemeralRunStore,
    RunAlreadyExists,
    RunNotFound,
    RunStore,
    RunViewSource,
    Snapshot,
    StoredEvent,
    StoreError,
)

__all__ = [
    "ApprovalConflict", "ApprovalMismatch", "ApprovalRequest", "CallRecord",
    "ConcurrentRunError", "DuplicateToolCallError", "EphemeralRunStore", "Event",
    "Harness", "InjectedFault", "MAX_APP_ID_BYTES", "MAX_RUN_ID_BYTES", "MAX_TOOL_CALL_ID_BYTES",
    "MAX_TOOL_PROGRESS_EVENT_BYTES", "MAX_TOOL_PROGRESS_EVENTS", "MAX_TOOL_PROGRESS_TOTAL_BYTES",
    "MaxStepsExceeded", "Message", "Model", "ModelCallError", "ModelReply",
    "ModelStreamEvent", "ModelStreamEventType", "ModelStreamProtocolError",
    "ModelTimeoutError", "ProjectionIntegrityError", "RunAlreadyExists", "RunBusy",
    "RunCancelled", "RunNotFound", "RunPaused", "RunResult", "RunStore",
    "RunViewSource", "SasoriError", "SkillSpec", "StreamingModel", "Snapshot", "StoredEvent",
    "StoreError", "Tool", "ToolCall", "ToolEffect", "ToolExecutionContext", "ToolProgressEvent", "event_projection",
    "is_valid_app_id", "is_valid_run_id", "is_valid_tool_call_id", "run_list_projection",
    "run_agent_loop", "run_projection", "validate_run_id",
]
