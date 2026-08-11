"""Compatibility contracts plus product-layer plugin and worker metadata."""

from dataclasses import dataclass

from sasori_core.contracts import *  # noqa: F401,F403
from sasori_core.contracts import __all__ as _CORE_ALL


@dataclass(frozen=True, slots=True)
class WorkerSpec:
    """Declarative digital-worker metadata owned outside the single-agent core."""

    worker_id: str
    version: str
    title: str
    description: str
    system_prompt: str
    model_slot: str
    skill_ids: tuple[str, ...]
    tool_names: tuple[str, ...]
    max_steps: int
    model_timeout: float
    tool_timeout: float
    allowed_effects: tuple[ToolEffect, ...] = ("read_only",)
    content_sha256: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "skill_ids", tuple(self.skill_ids))
        object.__setattr__(self, "tool_names", tuple(self.tool_names))
        object.__setattr__(self, "allowed_effects", tuple(self.allowed_effects))


@dataclass(frozen=True, slots=True)
class PluginRegistration:
    """Trusted installed-plugin registration owned by the bundle plugin SDK."""

    api_version: int
    plugin_id: str
    version: str
    tools: tuple[Tool, ...] = ()
    skills: tuple[SkillSpec, ...] = ()
    workers: tuple[WorkerSpec, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "tools", tuple(self.tools))
        object.__setattr__(self, "skills", tuple(self.skills))
        object.__setattr__(self, "workers", tuple(self.workers))


__all__ = [*_CORE_ALL, "PluginRegistration", "WorkerSpec"]
