from __future__ import annotations

import hashlib
import importlib.metadata
import inspect
import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal, Union, get_args, get_origin, get_type_hints

from .contracts import (
    PluginRegistration,
    SkillSpec,
    Tool,
    ToolEffect,
    WorkerSpec,
)


PLUGIN_API_VERSION = 1
PLUGIN_ENTRY_POINT_GROUP = "sasori.plugins"
FULL_HOST_PROCESS_PRIVILEGES = "FULL HOST PROCESS PRIVILEGES"

_MAX_MANIFEST_BYTES = 256 * 1024
_MAX_SUMMARY_CHARS = 4096
_MAX_SKILL_BYTES = 256 * 1024
_MAX_CAPABILITIES = 128
_PLUGIN_ID = re.compile(r"[a-z0-9]+(?:[.-][a-z0-9]+)+\Z")
_LOCAL_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_DISTRIBUTION = re.compile(r"[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*\Z")
_SAFE_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ENTRY_POINT_VALUE = re.compile(
    r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*\Z"
)
_LOGICAL_ROOTS = ("workspace:", "artifacts:", "plugin_state:", "temp:")
_EFFECT_RANK = {"read_only": 0, "idempotent": 1, "side_effecting": 2}
_MODE_RANK = {"container": 0, "supervised_process": 1, "trusted_process": 2}


class PluginError(Exception):
    pass


class ManifestError(PluginError):
    pass


class RegistrationError(PluginError):
    pass


class PluginLoadError(PluginError):
    pass


class PluginPermissionError(PluginError):
    pass


@dataclass(frozen=True, slots=True)
class PermissionSet:
    filesystem_read: tuple[str, ...] = ()
    filesystem_write: tuple[str, ...] = ()
    network_egress: tuple[str, ...] = ()
    host_process: tuple[str, ...] = ()
    secrets: tuple[str, ...] = ()
    full_host: bool = False

    def __post_init__(self) -> None:
        if type(self.full_host) is not bool:
            raise TypeError("full_host must be a boolean")
        for field_name in (
            "filesystem_read",
            "filesystem_write",
            "network_egress",
            "host_process",
            "secrets",
        ):
            value = getattr(self, field_name)
            if isinstance(value, (str, bytes)):
                raise TypeError(f"{field_name} must be a sequence of permissions")
            try:
                normalized = tuple(value)
            except TypeError:
                raise TypeError(
                    f"{field_name} must be a sequence of permissions"
                ) from None
            if (
                len(normalized) > _MAX_CAPABILITIES
                or any(
                    not isinstance(item, str)
                    or not item
                    or len(item) > 512
                    or any(
                        ord(character) < 32
                        or ord(character) == 127
                        or 0xD800 <= ord(character) <= 0xDFFF
                        for character in item
                    )
                    for item in normalized
                )
                or len(set(normalized)) != len(normalized)
                or field_name in {"filesystem_read", "filesystem_write"}
                and any(not _logical_path(item) for item in normalized)
            ):
                raise ValueError(f"{field_name} contains invalid permissions")
            object.__setattr__(self, field_name, normalized)

    @classmethod
    def full_host_access(cls) -> PermissionSet:
        return cls(full_host=True)

    def covers(self, other: PermissionSet) -> bool:
        if self.full_host:
            return True
        if other.full_host:
            return False
        return all(
            set(getattr(other, field_name)).issubset(getattr(self, field_name))
            for field_name in (
                "filesystem_read",
                "filesystem_write",
                "network_egress",
                "host_process",
                "secrets",
            )
        )


@dataclass(frozen=True, slots=True)
class ExecutionSpec:
    mode: str
    entry_point_group: str
    entry_point_name: str
    entry_point_value: str


@dataclass(frozen=True, slots=True)
class ManifestTool:
    name: str
    effect: ToolEffect
    tool_revision: str | None
    schema_sha256: str


@dataclass(frozen=True, slots=True)
class ManifestSkill:
    skill_id: str
    version: str
    content_sha256: str


@dataclass(frozen=True, slots=True)
class ManifestWorker:
    worker_id: str
    version: str
    content_sha256: str


@dataclass(frozen=True, slots=True)
class ManifestDependency:
    name: str
    version: str
    filename: str
    sha256: str


@dataclass(frozen=True, slots=True)
class PluginManifest:
    schema_version: int
    plugin_id: str
    name: str
    version: str
    summary: str
    distribution: str
    execution: ExecutionSpec
    permissions: PermissionSet
    tools: tuple[ManifestTool, ...]
    skills: tuple[ManifestSkill, ...]
    workers: tuple[ManifestWorker, ...]
    dependencies: tuple[ManifestDependency, ...]


@dataclass(frozen=True, slots=True)
class PermissionDisclosure:
    requested: PermissionSet
    granted: PermissionSet
    effective: PermissionSet
    effective_access: str
    enforced: bool


@dataclass(frozen=True, slots=True)
class UpgradeChange:
    kind: str
    path: str
    before: str | None
    after: str | None
    requires_approval: bool


@dataclass(frozen=True, slots=True)
class DiscoveredPlugin:
    plugin_id: str
    entry_point_group: str
    entry_point_value: str
    distribution: str
    version: str
    effective_access: str = FULL_HOST_PROCESS_PRIVILEGES


@dataclass(frozen=True, slots=True)
class LoadedPlugin:
    manifest: PluginManifest
    registration: PluginRegistration
    permissions: PermissionDisclosure


def _reject_json_constant(value: str) -> object:
    raise ValueError("non-finite JSON number")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite JSON number")
    return result


def _reject_surrogates(value: object) -> None:
    if isinstance(value, str):
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise ValueError("JSON strings must contain Unicode scalar values")
        return
    if isinstance(value, list):
        for item in value:
            _reject_surrogates(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_surrogates(key)
            _reject_surrogates(item)


def _strict_json(value: str | bytes) -> object:
    if not isinstance(value, (str, bytes)):
        raise ManifestError("manifest must be UTF-8 JSON text")
    try:
        raw = value.encode("utf-8") if isinstance(value, str) else value
    except UnicodeEncodeError:
        raise ManifestError("manifest must be UTF-8 JSON text") from None
    if len(raw) > _MAX_MANIFEST_BYTES:
        raise ManifestError("manifest exceeds the size limit")
    try:
        text = raw.decode("utf-8")
        decoded = json.loads(
            text,
            parse_constant=_reject_json_constant,
            parse_float=_finite_float,
            object_pairs_hook=_reject_duplicate_keys,
        )
        _reject_surrogates(decoded)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ManifestError("manifest is not strict JSON") from None
    return decoded


def _object(value: object, fields: frozenset[str], name: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ManifestError(f"{name} has missing or unknown fields")
    return value


def _text(value: object, name: str, *, limit: int = 256) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > limit
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ManifestError(f"{name} is invalid")
    return value


def _version(value: object, name: str) -> str:
    result = _text(value, name, limit=128)
    if _SAFE_VERSION.fullmatch(result) is None:
        raise ManifestError(f"{name} is invalid")
    return result


def _sha(value: object, name: str) -> str:
    result = _text(value, name, limit=64)
    if _SHA256.fullmatch(result) is None:
        raise ManifestError(f"{name} is invalid")
    return result


def _plugin_id(value: object) -> str:
    result = _text(value, "plugin_id", limit=128)
    if _PLUGIN_ID.fullmatch(result) is None:
        raise ManifestError("plugin_id is invalid")
    return result


def _contribution_id(value: object, plugin_id: str, name: str) -> str:
    result = _text(value, name, limit=256)
    prefix = plugin_id + "/"
    if not result.startswith(prefix) or _LOCAL_ID.fullmatch(result[len(prefix) :]) is None:
        raise ManifestError(f"{name} must use the plugin namespace")
    return result


def _unique_text_array(
    value: object,
    name: str,
    *,
    logical_filesystem: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > _MAX_CAPABILITIES:
        raise ManifestError(f"{name} must be a bounded array")
    result = tuple(_text(item, name, limit=512) for item in value)
    if len(set(result)) != len(result):
        raise ManifestError(f"{name} contains duplicates")
    if logical_filesystem and any(not _logical_path(item) for item in result):
        raise ManifestError(f"{name} must use a logical filesystem root")
    return result


def _logical_path(value: str) -> bool:
    root = next((item for item in _LOGICAL_ROOTS if value.startswith(item)), None)
    if root is None:
        return False
    suffix = value[len(root) :]
    if not suffix:
        return True
    return not (
        suffix.startswith("/")
        or "\\" in suffix
        or ":" in suffix
        or any(part in {"", ".", ".."} for part in suffix.split("/"))
    )


def _parse_execution(value: object, plugin_id: str) -> ExecutionSpec:
    data = _object(
        value,
        frozenset(
            {
                "mode",
                "entry_point_group",
                "entry_point_name",
                "entry_point_value",
            }
        ),
        "execution",
    )
    mode = _text(data["mode"], "execution.mode", limit=32)
    if mode not in _MODE_RANK:
        raise ManifestError("execution.mode is unsupported")
    group = _text(data["entry_point_group"], "entry point group", limit=128)
    name = _text(data["entry_point_name"], "entry point name", limit=128)
    target = _text(data["entry_point_value"], "entry point value", limit=256)
    if group != PLUGIN_ENTRY_POINT_GROUP or name != plugin_id:
        raise ManifestError("entry point identity does not match the plugin")
    if _ENTRY_POINT_VALUE.fullmatch(target) is None:
        raise ManifestError("entry point value is invalid")
    return ExecutionSpec(mode, group, name, target)


def _parse_permissions(value: object) -> PermissionSet:
    data = _object(
        value,
        frozenset(
            {
                "filesystem_read",
                "filesystem_write",
                "network_egress",
                "host_process",
                "secrets",
            }
        ),
        "permissions",
    )
    return PermissionSet(
        filesystem_read=_unique_text_array(
            data["filesystem_read"],
            "permissions.filesystem_read",
            logical_filesystem=True,
        ),
        filesystem_write=_unique_text_array(
            data["filesystem_write"],
            "permissions.filesystem_write",
            logical_filesystem=True,
        ),
        network_egress=_unique_text_array(
            data["network_egress"], "permissions.network_egress"
        ),
        host_process=_unique_text_array(
            data["host_process"], "permissions.host_process"
        ),
        secrets=_unique_text_array(data["secrets"], "permissions.secrets"),
    )


def _parse_tools(value: object) -> tuple[ManifestTool, ...]:
    if not isinstance(value, list) or len(value) > 256:
        raise ManifestError("tools must be a bounded array")
    result: list[ManifestTool] = []
    for item in value:
        data = _object(
            item,
            frozenset({"name", "effect", "tool_revision", "schema_sha256"}),
            "tool",
        )
        name = _text(data["name"], "tool.name", limit=64)
        if _TOOL_NAME.fullmatch(name) is None:
            raise ManifestError("tool.name is invalid")
        effect = _text(data["effect"], "tool.effect", limit=32)
        if effect not in _EFFECT_RANK:
            raise ManifestError("tool.effect is invalid")
        revision_value = data["tool_revision"]
        revision = (
            None
            if revision_value is None
            else _text(revision_value, "tool.tool_revision", limit=128)
        )
        if effect != "read_only" and revision is None:
            raise ManifestError("non-read-only tools require tool_revision")
        result.append(
            ManifestTool(
                name,
                effect,  # type: ignore[arg-type]
                revision,
                _sha(data["schema_sha256"], "tool.schema_sha256"),
            )
        )
    if len({item.name for item in result}) != len(result):
        raise ManifestError("tool IDs must be unique")
    return tuple(result)


def _parse_skills(value: object, plugin_id: str) -> tuple[ManifestSkill, ...]:
    if not isinstance(value, list) or len(value) > 256:
        raise ManifestError("skills must be a bounded array")
    result = []
    for item in value:
        data = _object(
            item,
            frozenset({"skill_id", "version", "content_sha256"}),
            "skill",
        )
        result.append(
            ManifestSkill(
                _contribution_id(data["skill_id"], plugin_id, "skill_id"),
                _version(data["version"], "skill.version"),
                _sha(data["content_sha256"], "skill.content_sha256"),
            )
        )
    if len({item.skill_id for item in result}) != len(result):
        raise ManifestError("skill IDs must be unique")
    return tuple(result)


def _parse_workers(value: object, plugin_id: str) -> tuple[ManifestWorker, ...]:
    if not isinstance(value, list) or len(value) > 256:
        raise ManifestError("workers must be a bounded array")
    result = []
    for item in value:
        data = _object(
            item,
            frozenset({"worker_id", "version", "content_sha256"}),
            "worker",
        )
        result.append(
            ManifestWorker(
                _contribution_id(data["worker_id"], plugin_id, "worker_id"),
                _version(data["version"], "worker.version"),
                _sha(data["content_sha256"], "worker.content_sha256"),
            )
        )
    if len({item.worker_id for item in result}) != len(result):
        raise ManifestError("worker IDs must be unique")
    return tuple(result)


def _parse_dependencies(value: object) -> tuple[ManifestDependency, ...]:
    if not isinstance(value, list) or len(value) > 256:
        raise ManifestError("dependencies must be a bounded array")
    result = []
    for item in value:
        data = _object(
            item,
            frozenset({"name", "version", "filename", "sha256"}),
            "dependency",
        )
        name = _text(data["name"], "dependency.name", limit=128)
        filename = _text(data["filename"], "dependency.filename", limit=256)
        if _DISTRIBUTION.fullmatch(name) is None:
            raise ManifestError("dependency.name is invalid")
        if "/" in filename or "\\" in filename or filename in {".", ".."}:
            raise ManifestError("dependency.filename must be a basename")
        result.append(
            ManifestDependency(
                name,
                _version(data["version"], "dependency.version"),
                filename,
                _sha(data["sha256"], "dependency.sha256"),
            )
        )
    if len({item.name for item in result}) != len(result):
        raise ManifestError("dependency IDs must be unique")
    return tuple(result)


def parse_manifest(value: str | bytes) -> PluginManifest:
    data = _object(
        _strict_json(value),
        frozenset(
            {
                "schema_version",
                "plugin_id",
                "name",
                "version",
                "summary",
                "distribution",
                "execution",
                "permissions",
                "tools",
                "skills",
                "workers",
                "dependencies",
            }
        ),
        "manifest",
    )
    schema_version = data["schema_version"]
    if type(schema_version) is not int or schema_version != PLUGIN_API_VERSION:
        raise ManifestError("manifest schema version is unsupported")
    plugin_id = _plugin_id(data["plugin_id"])
    distribution = _text(data["distribution"], "distribution", limit=128)
    if _DISTRIBUTION.fullmatch(distribution) is None:
        raise ManifestError("distribution is invalid")
    summary = data["summary"]
    if (
        not isinstance(summary, str)
        or len(summary) > _MAX_SUMMARY_CHARS
        or any(ord(character) < 32 or ord(character) == 127 for character in summary)
    ):
        raise ManifestError("summary is invalid")
    return PluginManifest(
        schema_version=schema_version,
        plugin_id=plugin_id,
        name=_text(data["name"], "name", limit=128),
        version=_version(data["version"], "version"),
        summary=summary,
        distribution=distribution,
        execution=_parse_execution(data["execution"], plugin_id),
        permissions=_parse_permissions(data["permissions"]),
        tools=_parse_tools(data["tools"]),
        skills=_parse_skills(data["skills"], plugin_id),
        workers=_parse_workers(data["workers"], plugin_id),
        dependencies=_parse_dependencies(data["dependencies"]),
    )


def _annotation_name(annotation: object) -> object:
    if annotation is inspect.Parameter.empty:
        return None
    if annotation is None or annotation is type(None):
        return "builtins.None"
    origin = get_origin(annotation)
    if origin is not None:
        origin_name = _annotation_name(origin)
        return {
            "origin": origin_name,
            "arguments": [_annotation_name(item) for item in get_args(annotation)],
        }
    module = getattr(annotation, "__module__", None)
    qualname = getattr(annotation, "__qualname__", None)
    if isinstance(module, str) and isinstance(qualname, str):
        return f"{module}.{qualname}"
    if annotation is Literal:
        return "typing.Literal"
    if annotation is Union:
        return "typing.Union"
    raise RegistrationError("tool annotations must have a stable identity")


def _default_value(value: object) -> object:
    if value is inspect.Parameter.empty:
        return {"required": True}
    if value is None or type(value) in (bool, int, float, str):
        if type(value) is float and not math.isfinite(value):
            raise RegistrationError("tool defaults must be finite JSON scalars")
        return {"required": False, "default": value}
    raise RegistrationError("tool defaults must be JSON scalars")


def tool_schema_sha256(tool: Tool) -> str:
    try:
        signature = inspect.signature(tool.handler)
        annotations = get_type_hints(tool.handler)
    except (NameError, TypeError, ValueError, RecursionError):
        raise RegistrationError("tool signature cannot be inspected") from None
    parameters = []
    for parameter in signature.parameters.values():
        if parameter.name == "idempotency_key" and tool.effect == "idempotent":
            continue
        parameters.append(
            {
                "name": parameter.name,
                "kind": parameter.kind.name,
                "annotation": _annotation_name(
                    annotations.get(parameter.name, parameter.annotation)
                ),
                **_default_value(parameter.default),
            }
        )
    canonical = json.dumps(
        {"parameters": parameters},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def skill_content_sha256(instructions: str) -> str:
    if not isinstance(instructions, str):
        raise RegistrationError("skill instructions must be UTF-8 text")
    try:
        encoded = instructions.encode("utf-8")
    except UnicodeEncodeError:
        raise RegistrationError("skill instructions must be UTF-8 text") from None
    if len(encoded) > _MAX_SKILL_BYTES:
        raise RegistrationError("skill instructions exceed the size limit")
    return hashlib.sha256(encoded).hexdigest()


def worker_content_sha256(worker: WorkerSpec) -> str:
    if not isinstance(worker, WorkerSpec):
        raise RegistrationError("worker content must be a WorkerSpec")
    try:
        canonical = json.dumps(
            {
                "system_prompt": worker.system_prompt,
                "model_slot": worker.model_slot,
                "skill_ids": worker.skill_ids,
                "tool_names": worker.tool_names,
                "max_steps": worker.max_steps,
                "model_timeout": worker.model_timeout,
                "tool_timeout": worker.tool_timeout,
                "allowed_effects": worker.allowed_effects,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise RegistrationError("worker content is not canonical UTF-8 data") from None
    return hashlib.sha256(canonical).hexdigest()


def _registration_text(
    value: object,
    name: str,
    *,
    limit: int,
    multiline: bool = False,
    byte_limit: int | None = None,
) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise RegistrationError(f"{name} is invalid")
    allowed_controls = "\t\n\r" if multiline else ""
    if any(
        (ord(character) < 32 and character not in allowed_controls)
        or ord(character) == 127
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    ):
        raise RegistrationError(f"{name} is invalid")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise RegistrationError(f"{name} is invalid") from None
    if byte_limit is not None and len(encoded) > byte_limit:
        raise RegistrationError(f"{name} is invalid")
    return value


def _references(
    values: tuple[object, ...], allowed: Mapping[str, object], name: str
) -> bool:
    return (
        all(isinstance(value, str) and value in allowed for value in values)
        and len(set(values)) == len(values)
    )


def _registration_id(value: object, plugin_id: str, name: str) -> str:
    try:
        return _contribution_id(value, plugin_id, name)
    except ManifestError as error:
        raise RegistrationError(str(error)) from None


def _registration_version(value: object, name: str) -> str:
    try:
        return _version(value, name)
    except ManifestError as error:
        raise RegistrationError(str(error)) from None


def _unique_map(items: Iterable[object], attribute: str, name: str) -> dict[str, object]:
    result: dict[str, object] = {}
    for item in items:
        key = getattr(item, attribute, None)
        if not isinstance(key, str) or not key or key in result:
            raise RegistrationError(f"{name} IDs must be non-empty and unique")
        result[key] = item
    return result


def validate_registration(
    manifest: PluginManifest, registration: PluginRegistration
) -> PluginRegistration:
    if not isinstance(manifest, PluginManifest):
        raise RegistrationError("manifest must be a PluginManifest")
    if not isinstance(registration, PluginRegistration):
        raise RegistrationError("plugin factory must return PluginRegistration")
    if (
        type(registration.api_version) is not int
        or registration.api_version != PLUGIN_API_VERSION
        or registration.plugin_id != manifest.plugin_id
        or registration.version != manifest.version
    ):
        raise RegistrationError("registration identity does not match the manifest")

    tools = _unique_map(registration.tools, "name", "tool")
    manifest_tools = {item.name: item for item in manifest.tools}
    if set(tools) != set(manifest_tools):
        raise RegistrationError("registered tools do not match the manifest")
    for name, value in tools.items():
        if not isinstance(value, Tool):
            raise RegistrationError("registered tools must be Tool values")
        expected = manifest_tools[name]
        if (
            value.effect != expected.effect
            or value.tool_revision != expected.tool_revision
            or tool_schema_sha256(value) != expected.schema_sha256
        ):
            raise RegistrationError(f"registered tool contract mismatch: {name}")

    skills = _unique_map(registration.skills, "skill_id", "skill")
    manifest_skills = {item.skill_id: item for item in manifest.skills}
    if set(skills) != set(manifest_skills):
        raise RegistrationError("registered skills do not match the manifest")
    for skill_id, value in skills.items():
        if not isinstance(value, SkillSpec):
            raise RegistrationError("registered skills must be SkillSpec values")
        _registration_id(value.skill_id, manifest.plugin_id, "skill_id")
        _registration_version(value.version, "skill.version")
        _registration_text(value.title, "skill.title", limit=128)
        _registration_text(
            value.description, "skill.description", limit=4096, multiline=True
        )
        _registration_text(
            value.instructions,
            "skill.instructions",
            limit=_MAX_SKILL_BYTES,
            multiline=True,
            byte_limit=_MAX_SKILL_BYTES,
        )
        if not _references(value.tool_names, tools, "skill.tool_names"):
            raise RegistrationError(f"skill has a dangling tool reference: {skill_id}")
        actual_hash = skill_content_sha256(value.instructions)
        expected = manifest_skills[skill_id]
        if (
            value.version != expected.version
            or value.content_sha256 != actual_hash
            or value.content_sha256 != expected.content_sha256
        ):
            raise RegistrationError(f"registered skill contract mismatch: {skill_id}")

    workers = _unique_map(registration.workers, "worker_id", "worker")
    manifest_workers = {item.worker_id: item for item in manifest.workers}
    if set(workers) != set(manifest_workers):
        raise RegistrationError("registered workers do not match the manifest")
    for worker_id, value in workers.items():
        if not isinstance(value, WorkerSpec):
            raise RegistrationError("registered workers must be WorkerSpec values")
        _registration_id(value.worker_id, manifest.plugin_id, "worker_id")
        _registration_version(value.version, "worker.version")
        _registration_text(value.title, "worker.title", limit=128)
        _registration_text(
            value.description, "worker.description", limit=4096, multiline=True
        )
        _registration_text(
            value.system_prompt,
            "worker.system_prompt",
            limit=_MAX_SKILL_BYTES,
            multiline=True,
            byte_limit=_MAX_SKILL_BYTES,
        )
        _registration_text(value.model_slot, "worker.model_slot", limit=128)
        if (
            not _references(value.skill_ids, skills, "worker.skill_ids")
            or not _references(value.tool_names, tools, "worker.tool_names")
        ):
            raise RegistrationError(f"worker has a dangling reference: {worker_id}")
        if (
            type(value.max_steps) is not int
            or value.max_steps < 1
            or value.max_steps > 10_000
            or isinstance(value.model_timeout, bool)
            or not isinstance(value.model_timeout, (int, float))
            or not math.isfinite(value.model_timeout)
            or value.model_timeout <= 0
            or value.model_timeout > 86_400
            or isinstance(value.tool_timeout, bool)
            or not isinstance(value.tool_timeout, (int, float))
            or not math.isfinite(value.tool_timeout)
            or value.tool_timeout <= 0
            or value.tool_timeout > 86_400
            or not value.allowed_effects
            or any(
                not isinstance(effect, str) or effect not in _EFFECT_RANK
                for effect in value.allowed_effects
            )
            or len(set(value.allowed_effects)) != len(value.allowed_effects)
            or any(tools[name].effect not in value.allowed_effects for name in value.tool_names)
        ):
            raise RegistrationError(f"worker limits or effects are invalid: {worker_id}")
        actual_hash = worker_content_sha256(value)
        expected = manifest_workers[worker_id]
        if (
            value.version != expected.version
            or value.content_sha256 != actual_hash
            or value.content_sha256 != expected.content_sha256
        ):
            raise RegistrationError(f"registered worker contract mismatch: {worker_id}")
    return registration


def disclose_permissions(
    manifest: PluginManifest,
    granted: PermissionSet,
) -> PermissionDisclosure:
    if not isinstance(granted, PermissionSet):
        raise PluginPermissionError("permission sets must use PermissionSet values")
    if manifest.execution.mode != "trusted_process":
        raise PluginPermissionError(
            "permission enforcement for isolated execution is not implemented"
        )
    if not granted.covers(manifest.permissions):
        raise PluginPermissionError("requested permissions were not fully granted")
    return PermissionDisclosure(
        manifest.permissions,
        granted,
        PermissionSet.full_host_access(),
        FULL_HOST_PROCESS_PRIVILEGES,
        False,
    )


def _change(
    kind: str,
    path: str,
    before: object,
    after: object,
    *,
    approval: bool,
) -> UpgradeChange:
    def render(value: object) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    return UpgradeChange(kind, path, render(before), render(after), approval)


def manifest_upgrade_diff(
    before: PluginManifest, after: PluginManifest
) -> tuple[UpgradeChange, ...]:
    if before.plugin_id != after.plugin_id:
        raise ValueError("upgrade manifests must use the same plugin_id")
    changes: list[UpgradeChange] = []
    if before.execution.mode != after.execution.mode:
        changes.append(
            _change(
                "execution_mode",
                "execution.mode",
                before.execution.mode,
                after.execution.mode,
                approval=_MODE_RANK[after.execution.mode]
                > _MODE_RANK[before.execution.mode],
            )
        )
    for field_name in (
        "entry_point_group",
        "entry_point_name",
        "entry_point_value",
    ):
        old = getattr(before.execution, field_name)
        new = getattr(after.execution, field_name)
        if old != new:
            changes.append(
                _change(
                    "entry_point_identity",
                    f"execution.{field_name}",
                    old,
                    new,
                    approval=True,
                )
            )
    if before.distribution != after.distribution:
        changes.append(
            _change(
                "distribution_identity",
                "distribution",
                before.distribution,
                after.distribution,
                approval=True,
            )
        )
    for field_name in (
        "filesystem_read",
        "filesystem_write",
        "network_egress",
        "host_process",
        "secrets",
    ):
        old = set(getattr(before.permissions, field_name))
        new = set(getattr(after.permissions, field_name))
        for value in sorted(new - old):
            changes.append(
                _change(
                    "permission_added",
                    f"permissions.{field_name}",
                    None,
                    value,
                    approval=True,
                )
            )
        for value in sorted(old - new):
            changes.append(
                _change(
                    "permission_removed",
                    f"permissions.{field_name}",
                    value,
                    None,
                    approval=False,
                )
            )

    old_tools = {item.name: item for item in before.tools}
    new_tools = {item.name: item for item in after.tools}
    for name in sorted(set(old_tools) | set(new_tools)):
        old = old_tools.get(name)
        new = new_tools.get(name)
        if old is None:
            changes.append(_change("tool_added", f"tools.{name}", None, new.name, approval=True))
            continue
        if new is None:
            changes.append(
                _change(
                    "tool_removed",
                    f"tools.{name}",
                    old.name,
                    None,
                    approval=False,
                )
            )
            continue
        if old.effect != new.effect and old.tool_revision == new.tool_revision:
            raise ValueError(
                f"tool effect changes require a new tool_revision: {name}"
            )
        for field_name in ("effect", "tool_revision", "schema_sha256"):
            old_value = getattr(old, field_name)
            new_value = getattr(new, field_name)
            if old_value != new_value:
                changes.append(
                    _change(
                        "tool_contract",
                        f"tools.{name}.{field_name}",
                        old_value,
                        new_value,
                        approval=True,
                    )
                )

    for collection_name, id_field in (
        ("skills", "skill_id"),
        ("workers", "worker_id"),
    ):
        old_items = {
            getattr(item, id_field): item for item in getattr(before, collection_name)
        }
        new_items = {
            getattr(item, id_field): item for item in getattr(after, collection_name)
        }
        kind = collection_name[:-1]
        for contribution_id in sorted(set(old_items) | set(new_items)):
            old = old_items.get(contribution_id)
            new = new_items.get(contribution_id)
            if old == new:
                continue
            before_value = (
                None
                if old is None
                else {"version": old.version, "content_sha256": old.content_sha256}
            )
            after_value = (
                None
                if new is None
                else {"version": new.version, "content_sha256": new.content_sha256}
            )
            change_kind = (
                "added" if old is None else "removed" if new is None else "contract"
            )
            changes.append(
                _change(
                    f"{kind}_{change_kind}",
                    f"{collection_name}.{contribution_id}",
                    before_value,
                    after_value,
                    approval=new is not None,
                )
            )

    old_dependencies = {item.name: item for item in before.dependencies}
    new_dependencies = {item.name: item for item in after.dependencies}

    def dependency_data(item: ManifestDependency) -> dict[str, str]:
        return {
            "name": item.name,
            "version": item.version,
            "filename": item.filename,
            "sha256": item.sha256,
        }

    for name in sorted(set(old_dependencies) | set(new_dependencies)):
        old = old_dependencies.get(name)
        new = new_dependencies.get(name)
        if old != new:
            changes.append(
                _change(
                    "dependency_identity",
                    f"dependencies.{name}",
                    None if old is None else dependency_data(old),
                    None if new is None else dependency_data(new),
                    approval=True,
                )
            )
    return tuple(changes)


def _entry_point_inventory(
    entry_points: Iterable[object] | None,
) -> tuple[tuple[DiscoveredPlugin, object], ...]:
    selected = (
        importlib.metadata.entry_points(group=PLUGIN_ENTRY_POINT_GROUP)
        if entry_points is None
        else entry_points
    )
    result: list[tuple[DiscoveredPlugin, object]] = []
    seen: set[str] = set()
    for entry_point in selected:
        group = getattr(entry_point, "group", PLUGIN_ENTRY_POINT_GROUP)
        name = getattr(entry_point, "name", None)
        value = getattr(entry_point, "value", None)
        distribution = getattr(entry_point, "dist", None)
        distribution_name = getattr(distribution, "name", None)
        distribution_version = getattr(distribution, "version", None)
        if (
            group != PLUGIN_ENTRY_POINT_GROUP
            or not isinstance(name, str)
            or _PLUGIN_ID.fullmatch(name) is None
            or not isinstance(value, str)
            or _ENTRY_POINT_VALUE.fullmatch(value) is None
            or not isinstance(distribution_name, str)
            or _DISTRIBUTION.fullmatch(distribution_name) is None
            or not isinstance(distribution_version, str)
            or _SAFE_VERSION.fullmatch(distribution_version) is None
        ):
            raise PluginLoadError("installed plugin entry-point metadata is invalid")
        if name in seen:
            raise PluginLoadError("duplicate installed plugin ID")
        seen.add(name)
        result.append(
            (
                DiscoveredPlugin(
                    name,
                    group,
                    value,
                    distribution_name,
                    distribution_version,
                ),
                entry_point,
            )
        )
    return tuple(sorted(result, key=lambda item: item[0].plugin_id))


def discover_plugins(
    *, entry_points: Iterable[object] | None = None
) -> tuple[DiscoveredPlugin, ...]:
    return tuple(item for item, _ in _entry_point_inventory(entry_points))


def load_enabled_plugins(
    enabled: Mapping[str, PluginManifest],
    *,
    grants: Mapping[str, PermissionSet],
    entry_points: Iterable[object] | None = None,
) -> tuple[LoadedPlugin, ...]:
    inventory = _entry_point_inventory(entry_points)
    installed = {item.plugin_id: (item, entry_point) for item, entry_point in inventory}
    if set(enabled) != set(grants):
        raise PluginPermissionError("enabled plugins require one explicit grant each")

    preflight: list[
        tuple[PluginManifest, PermissionDisclosure, object]
    ] = []
    for plugin_id in sorted(enabled):
        manifest = enabled[plugin_id]
        if not isinstance(manifest, PluginManifest):
            raise PluginLoadError("enabled plugin manifest is invalid")
        if plugin_id != manifest.plugin_id:
            raise PluginLoadError("enabled plugin key does not match its manifest")
        if manifest.execution.mode != "trusted_process":
            raise PluginLoadError("only trusted_process plugins are implemented")
        candidate = installed.get(plugin_id)
        if candidate is None:
            raise PluginLoadError("enabled plugin is not installed")
        discovered, entry_point = candidate
        if (
            discovered.entry_point_group != manifest.execution.entry_point_group
            or discovered.entry_point_value != manifest.execution.entry_point_value
            or discovered.distribution != manifest.distribution
            or discovered.version != manifest.version
        ):
            raise PluginLoadError("installed plugin identity does not match the manifest")
        preflight.append(
            (manifest, disclose_permissions(manifest, grants[plugin_id]), entry_point)
        )

    loaded = []
    for manifest, permissions, entry_point in preflight:
        try:
            factory = entry_point.load()  # type: ignore[attr-defined]
        except Exception as error:
            raise PluginLoadError("enabled plugin factory could not be imported") from error
        if not callable(factory):
            raise PluginLoadError("enabled plugin entry point must be callable")
        try:
            registration = factory()
        except Exception as error:
            raise PluginLoadError("enabled plugin factory failed") from error
        try:
            validated = validate_registration(manifest, registration)
        except RegistrationError as error:
            raise PluginLoadError(str(error)) from None
        loaded.append(LoadedPlugin(manifest, validated, permissions))
    return tuple(loaded)


__all__ = [
    "DiscoveredPlugin",
    "ExecutionSpec",
    "FULL_HOST_PROCESS_PRIVILEGES",
    "LoadedPlugin",
    "ManifestDependency",
    "ManifestError",
    "ManifestSkill",
    "ManifestTool",
    "ManifestWorker",
    "PLUGIN_API_VERSION",
    "PLUGIN_ENTRY_POINT_GROUP",
    "PermissionDisclosure",
    "PermissionSet",
    "PluginError",
    "PluginLoadError",
    "PluginManifest",
    "PluginPermissionError",
    "RegistrationError",
    "UpgradeChange",
    "disclose_permissions",
    "discover_plugins",
    "load_enabled_plugins",
    "manifest_upgrade_diff",
    "parse_manifest",
    "skill_content_sha256",
    "tool_schema_sha256",
    "validate_registration",
    "worker_content_sha256",
]
