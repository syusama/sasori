from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import keyword
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from sasori import PluginRegistration, Tool
from sasori._provider_common import compile_tool_schema, json_values_equal, strict_json_loads
from sasori.plugins import PluginManifest, parse_manifest, tool_schema_sha256, validate_registration


_MAX_SNAPSHOT_BYTES = 256 * 1024
_MAX_LINE_BYTES = 1024 * 1024
_MAX_STDERR_BYTES = 64 * 1024
_MAX_OUTPUT_BYTES = 256 * 1024
_MAX_TOOLS = 128
_MAX_TEXT_BLOCKS = 64
_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")


class MCPStdioError(Exception):
    pass


class MCPConfigurationError(MCPStdioError):
    pass


class MCPProtocolError(MCPStdioError):
    pass


class MCPTimeoutError(MCPStdioError):
    pass


class MCPRemoteToolError(MCPStdioError):
    pass


@dataclass(frozen=True, slots=True)
class _EnvironmentValue:
    literal: str | None = None
    from_env: str | None = None


@dataclass(frozen=True, slots=True)
class _Command:
    argv: tuple[str, ...]
    cwd: str
    environment: tuple[tuple[str, _EnvironmentValue], ...]


@dataclass(frozen=True, slots=True)
class _RemoteTool:
    name: str
    description: str
    schema: dict[str, object]
    annotations: tuple[tuple[str, type | object], ...]
    effect: str


@dataclass(frozen=True, slots=True)
class _Snapshot:
    plugin_id: str
    version: str
    command: _Command
    timeout: float
    initialize_result: dict[str, object]
    tools_list_result: dict[str, object]
    tools: tuple[_RemoteTool, ...]
    sha256: str


def _object(value: object, fields: set[str], name: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != fields:
        raise MCPConfigurationError(f"{name} has missing or unknown fields")
    return value


def _text(value: object, name: str, limit: int = 4096) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > limit
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise MCPConfigurationError(f"{name} is invalid")
    return value


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise MCPConfigurationError("MCP snapshot is not canonical JSON") from None


def _schema_annotation(value: object, depth: int = 0) -> tuple[dict[str, object], object]:
    if depth > 8 or not isinstance(value, dict):
        raise MCPConfigurationError("MCP tool schema is unsupported")
    if set(value) == {"type"} and value["type"] in ("string", "integer", "number", "boolean"):
        annotation = {"string": str, "integer": int, "number": float, "boolean": bool}[value["type"]]
        return {"type": value["type"]}, annotation
    if set(value) == {"type", "items"} and value["type"] == "array":
        item_schema, annotation = _schema_annotation(value["items"], depth + 1)
        return {"type": "array", "items": item_schema}, list[annotation]  # type: ignore[valid-type]
    raise MCPConfigurationError("MCP tool schema is unsupported")


def _input_schema(value: object) -> tuple[dict[str, object], tuple[tuple[str, object], ...]]:
    data = _object(value, {"type", "properties", "required", "additionalProperties"}, "inputSchema")
    if data["type"] != "object" or data["additionalProperties"] is not False:
        raise MCPConfigurationError("MCP tool inputSchema must be a closed object")
    properties = data["properties"]
    required = data["required"]
    if not isinstance(properties, dict) or not isinstance(required, list):
        raise MCPConfigurationError("MCP tool inputSchema is invalid")
    names = sorted(properties)
    if (
        required != sorted(required)
        or required != names
        or len(set(required)) != len(required)
        or any(
            not isinstance(name, str)
            or not name.isidentifier()
            or keyword.iskeyword(name)
            or name == "idempotency_key"
            for name in names
        )
    ):
        raise MCPConfigurationError("MCP tool parameters must all be required safe identifiers")
    normalized_properties: dict[str, object] = {}
    annotations = []
    for name in names:
        normalized, annotation = _schema_annotation(properties[name])
        normalized_properties[name] = normalized
        annotations.append((name, annotation))
    return (
        {
            "type": "object",
            "properties": normalized_properties,
            "required": names,
            "additionalProperties": False,
        },
        tuple(annotations),
    )


def _normalize_tools_result(
    value: object,
    effects: Mapping[str, object],
) -> tuple[dict[str, object], tuple[_RemoteTool, ...]]:
    data = _object(value, {"tools"}, "tools/list result")
    tools = data["tools"]
    if not isinstance(tools, list) or len(tools) > _MAX_TOOLS:
        raise MCPConfigurationError("MCP tool list is invalid")
    normalized = []
    remote_tools = []
    seen: set[str] = set()
    for item in tools:
        tool = _object(item, {"name", "description", "inputSchema"}, "MCP tool")
        name = _text(tool["name"], "MCP tool name", 64)
        if _NAME.fullmatch(name) is None or name in seen:
            raise MCPConfigurationError("MCP tool names must be unique and Sasori-compatible")
        seen.add(name)
        description = _text(tool["description"], "MCP tool description")
        schema, annotations = _input_schema(tool["inputSchema"])
        effect = effects.get(name, "side_effecting")
        if effect not in ("read_only", "side_effecting"):
            raise MCPConfigurationError("MCP tool effect is invalid")
        normalized.append({"name": name, "description": description, "inputSchema": schema})
        remote_tools.append(_RemoteTool(name, description, schema, annotations, effect))
    if set(effects) - seen:
        raise MCPConfigurationError("MCP effects refer to unknown tools")
    order = sorted(range(len(normalized)), key=lambda index: normalized[index]["name"])
    return (
        {"tools": [normalized[index] for index in order]},
        tuple(remote_tools[index] for index in order),
    )


def _command(value: object) -> _Command:
    data = _object(value, {"argv", "cwd", "env"}, "MCP command")
    argv = data["argv"]
    if not isinstance(argv, list) or not 1 <= len(argv) <= 32:
        raise MCPConfigurationError("MCP argv must be a bounded array")
    arguments = tuple(_text(item, "MCP argv item", 8192) for item in argv)
    executable = Path(arguments[0])
    if not executable.is_absolute() or not executable.is_file():
        raise MCPConfigurationError("MCP executable must be an existing absolute file")
    for argument in arguments[1:]:
        if argument.startswith("-"):
            continue
        path = Path(argument)
        if not path.is_absolute() or not path.exists():
            raise MCPConfigurationError("MCP file arguments must be existing absolute paths")
    cwd = Path(_text(data["cwd"], "MCP cwd", 8192))
    if not cwd.is_absolute() or not cwd.is_dir():
        raise MCPConfigurationError("MCP cwd must be an existing absolute directory")
    raw_environment = data["env"]
    if not isinstance(raw_environment, dict) or len(raw_environment) > 64:
        raise MCPConfigurationError("MCP env is invalid")
    environment = []
    for name in sorted(raw_environment):
        if _ENV_NAME.fullmatch(name) is None:
            raise MCPConfigurationError("MCP env name is invalid")
        source = raw_environment[name]
        if not isinstance(source, dict) or len(source) != 1:
            raise MCPConfigurationError("MCP env source is invalid")
        if set(source) == {"literal"}:
            environment.append((name, _EnvironmentValue(literal=_text(source["literal"], "MCP env literal", 8192))))
        elif set(source) == {"from_env"}:
            variable = _text(source["from_env"], "MCP env source", 128)
            if _ENV_NAME.fullmatch(variable) is None:
                raise MCPConfigurationError("MCP env source is invalid")
            environment.append((name, _EnvironmentValue(from_env=variable)))
        else:
            raise MCPConfigurationError("MCP env source is invalid")
    return _Command(tuple(str(Path(item).resolve()) if index == 0 or not item.startswith("-") else item for index, item in enumerate(arguments)), str(cwd.resolve()), tuple(environment))


def _parse_snapshot(value: str | bytes) -> _Snapshot:
    try:
        raw = value.encode("utf-8") if isinstance(value, str) else value
    except (AttributeError, UnicodeEncodeError):
        raise MCPConfigurationError("MCP snapshot must be UTF-8 JSON") from None
    if len(raw) > _MAX_SNAPSHOT_BYTES:
        raise MCPConfigurationError("MCP snapshot exceeds the size limit")
    try:
        decoded = strict_json_loads(raw)
    except (UnicodeDecodeError, ValueError, TypeError, RecursionError):
        raise MCPConfigurationError("MCP snapshot is not strict JSON") from None
    data = _object(
        decoded,
        {"snapshot_version", "plugin_id", "version", "command", "timeout", "initialize_result", "tools_list_result", "effects"},
        "MCP snapshot",
    )
    if data["snapshot_version"] != 1:
        raise MCPConfigurationError("MCP snapshot version is unsupported")
    timeout = data["timeout"]
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 1 <= timeout <= 300:
        raise MCPConfigurationError("MCP timeout must be from 1 to 300 seconds")
    initialize_result = data["initialize_result"]
    if not isinstance(initialize_result, dict):
        raise MCPConfigurationError("MCP initialize result must be an object")
    effects = data["effects"]
    if not isinstance(effects, dict):
        raise MCPConfigurationError("MCP effects must be an object")
    tools_result, tools = _normalize_tools_result(data["tools_list_result"], effects)
    normalized = {
        "snapshot_version": 1,
        "plugin_id": _text(data["plugin_id"], "MCP plugin_id", 128),
        "version": _text(data["version"], "MCP version", 128),
        "command": data["command"],
        "timeout": float(timeout),
        "initialize_result": initialize_result,
        "tools_list_result": tools_result,
        "effects": {tool.name: tool.effect for tool in tools},
    }
    snapshot_hash = hashlib.sha256(_canonical(normalized)).hexdigest()
    return _Snapshot(
        normalized["plugin_id"],  # type: ignore[arg-type]
        normalized["version"],  # type: ignore[arg-type]
        _command(data["command"]),
        float(timeout),
        initialize_result,
        tools_result,
        tools,
        snapshot_hash,
    )


def _matches(value: object, schema: dict[str, object]) -> bool:
    schema_type = schema.get("type")
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "integer":
        return type(value) is int
    if schema_type == "number":
        return type(value) in (int, float) and math.isfinite(value)
    if schema_type == "boolean":
        return type(value) is bool
    if schema_type == "array":
        item_schema = schema.get("items")
        return isinstance(value, list) and isinstance(item_schema, dict) and all(_matches(item, item_schema) for item in value)
    if schema_type == "object":
        properties = schema.get("properties")
        required = schema.get("required")
        return (
            isinstance(value, dict)
            and isinstance(properties, dict)
            and isinstance(required, list)
            and set(value) == set(required)
            and all(isinstance(properties[name], dict) and _matches(value[name], properties[name]) for name in required)
        )
    return False


class _MCPClient:
    def __init__(self, snapshot: _Snapshot, tool: _RemoteTool) -> None:
        self.snapshot = snapshot
        self.tool = tool
        self.deadline = 0.0
        self.process: asyncio.subprocess.Process | None = None
        self.stderr_task: asyncio.Task[int] | None = None
        self.call_sent = False

    def _remaining(self) -> float:
        remaining = self.deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise MCPTimeoutError("MCP child total deadline expired")
        return remaining

    def _environment(self) -> dict[str, str]:
        environment = {}
        if os.name == "nt" and "SYSTEMROOT" in os.environ:
            environment["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
        for name, source in self.snapshot.command.environment:
            if source.literal is not None:
                environment[name] = source.literal
            else:
                assert source.from_env is not None
                value = os.environ.get(source.from_env)
                if value is None:
                    raise MCPConfigurationError("configured MCP environment source is unavailable")
                environment[name] = value
        return environment

    async def _drain_stderr(self, stream: asyncio.StreamReader) -> int:
        total = 0
        while chunk := await stream.read(8192):
            total += len(chunk)
            if total > _MAX_STDERR_BYTES:
                raise MCPProtocolError("MCP stderr exceeds the size limit")
        return total

    async def _write(self, value: object) -> None:
        process = self.process
        if process is None or process.stdin is None or process.stdin.is_closing():
            raise MCPProtocolError("MCP child stdin is unavailable")
        payload = _canonical(value) + b"\n"
        if len(payload) > _MAX_LINE_BYTES:
            raise MCPProtocolError("MCP request exceeds the line limit")
        process.stdin.write(payload)
        try:
            await asyncio.wait_for(process.stdin.drain(), self._remaining())
        except TimeoutError:
            raise MCPTimeoutError("MCP child write timed out") from None

    async def _line(self) -> bytes:
        process = self.process
        if process is None or process.stdout is None:
            raise MCPProtocolError("MCP child stdout is unavailable")
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise asyncio.CancelledError
        line_task = asyncio.create_task(process.stdout.readline())
        deadline_task = asyncio.create_task(asyncio.sleep(self._remaining()))
        try:
            while True:
                tasks: set[asyncio.Task[object]] = {line_task, deadline_task}  # type: ignore[assignment]
                if self.stderr_task is not None:
                    tasks.add(self.stderr_task)  # type: ignore[arg-type]
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise asyncio.CancelledError
                if self.stderr_task is not None and self.stderr_task in done:
                    task = self.stderr_task
                    self.stderr_task = None
                    error = task.exception()
                    if error is not None:
                        raise error
                if line_task in done:
                    return line_task.result()
                if deadline_task in done:
                    raise MCPTimeoutError("MCP child response timed out")
        finally:
            for task in (line_task, deadline_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(line_task, deadline_task, return_exceptions=True)

    async def _response(self, expected_id: int) -> object:
        try:
            line = await self._line()
        except (ValueError, asyncio.LimitOverrunError):
            raise MCPProtocolError("MCP response exceeds the line limit") from None
        if not line:
            raise MCPProtocolError("MCP child ended before its response")
        if not line.endswith(b"\n") or len(line) > _MAX_LINE_BYTES or line.startswith(b"\xef\xbb\xbf"):
            raise MCPProtocolError("MCP response framing is invalid")
        try:
            decoded = strict_json_loads(line[:-1])
        except (UnicodeDecodeError, ValueError, TypeError, RecursionError):
            raise MCPProtocolError("MCP response is not strict JSON") from None
        if not isinstance(decoded, dict) or decoded.get("jsonrpc") != "2.0" or type(decoded.get("id")) is not int or decoded["id"] != expected_id:
            raise MCPProtocolError("MCP response identity is invalid")
        has_result = "result" in decoded
        has_error = "error" in decoded
        if set(decoded) != ({"jsonrpc", "id", "result"} if has_result else {"jsonrpc", "id", "error"}) or has_result == has_error:
            raise MCPProtocolError("MCP response shape is invalid")
        if has_error:
            raise MCPRemoteToolError("MCP server returned a JSON-RPC error")
        return decoded["result"]

    async def _request(self, request_id: int, method: str, params: object) -> object:
        await self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        return await self._response(request_id)

    async def _reap(self) -> None:
        process = self.process
        if process is None:
            return
        if process.stdin is not None and not process.stdin.is_closing():
            process.stdin.close()
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), 0.5)
            except TimeoutError:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), 0.5)
                except TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.wait()
        if self.stderr_task is not None:
            if not self.stderr_task.done():
                self.stderr_task.cancel()
            await asyncio.gather(self.stderr_task, return_exceptions=True)
            self.stderr_task = None

    async def call(self, arguments: dict[str, object]) -> str:
        if not _matches(arguments, self.tool.schema):
            raise MCPConfigurationError("MCP tool arguments do not match the frozen schema")
        self.deadline = asyncio.get_running_loop().time() + self.snapshot.timeout
        environment = self._environment()
        try:
            self.process = await asyncio.create_subprocess_exec(
                *self.snapshot.command.argv,
                cwd=self.snapshot.command.cwd,
                env=environment,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=_MAX_LINE_BYTES + 1,
            )
            assert self.process.stderr is not None
            self.stderr_task = asyncio.create_task(self._drain_stderr(self.process.stderr))
            initialize = await self._request(
                1,
                "initialize",
                {
                    "protocolVersion": self.snapshot.initialize_result.get("protocolVersion"),
                    "capabilities": {},
                    "clientInfo": {"name": "sasori", "version": "0.1.0.dev0"},
                },
            )
            if not json_values_equal(initialize, self.snapshot.initialize_result):
                raise MCPProtocolError("MCP initialize snapshot changed")
            await self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})
            live_tools = await self._request(2, "tools/list", {})
            try:
                normalized, _ = _normalize_tools_result(live_tools, {tool.name: tool.effect for tool in self.snapshot.tools})
            except MCPConfigurationError:
                raise MCPProtocolError("MCP tool snapshot is invalid") from None
            if not json_values_equal(normalized, self.snapshot.tools_list_result):
                raise MCPProtocolError("MCP tool snapshot changed")
            self.call_sent = True
            result = await self._request(3, "tools/call", {"name": self.tool.name, "arguments": arguments})
            data = _object(result, {"content", "isError"}, "MCP tool result")
            if type(data["isError"]) is not bool or not isinstance(data["content"], list) or not len(data["content"]) <= _MAX_TEXT_BLOCKS:
                raise MCPProtocolError("MCP tool result is invalid")
            texts = []
            for block in data["content"]:
                item = _object(block, {"type", "text"}, "MCP text block")
                if item["type"] != "text" or not isinstance(item["text"], str):
                    raise MCPProtocolError("MCP tool result contains unsupported content")
                texts.append(item["text"])
            if data["isError"]:
                raise MCPRemoteToolError("MCP tool reported an error")
            rendered = "[UNTRUSTED MCP OUTPUT]\n" + "\n".join(texts)
            if len(rendered.encode("utf-8")) > _MAX_OUTPUT_BYTES:
                raise MCPProtocolError("MCP tool output exceeds the size limit")
            assert self.process.stdin is not None and self.process.stdout is not None
            self.process.stdin.close()
            try:
                extra = await asyncio.wait_for(self.process.stdout.read(_MAX_LINE_BYTES + 1), self._remaining())
                code = await asyncio.wait_for(self.process.wait(), self._remaining())
            except TimeoutError:
                raise MCPTimeoutError("MCP child did not exit after the response") from None
            if self.stderr_task is not None:
                stderr_task = self.stderr_task
                self.stderr_task = None
                try:
                    await asyncio.wait_for(stderr_task, self._remaining())
                except TimeoutError:
                    raise MCPTimeoutError("MCP stderr did not close after the response") from None
            if extra or code != 0:
                raise MCPProtocolError("MCP child emitted late output or exited unsuccessfully")
            return rendered
        except asyncio.CancelledError:
            if self.call_sent and self.process is not None and self.process.stdin is not None and not self.process.stdin.is_closing():
                try:
                    payload = _canonical({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 3, "reason": "cancelled"}}) + b"\n"
                    self.process.stdin.write(payload)
                except (MCPConfigurationError, OSError, RuntimeError):
                    pass
            raise
        finally:
            await asyncio.shield(self._reap())


def _handler(snapshot: _Snapshot, remote: _RemoteTool):
    async def call(**arguments):
        return await _MCPClient(snapshot, remote).call(arguments)

    call.__name__ = f"mcp_{remote.name.replace('-', '_')}"
    call.__signature__ = inspect.Signature(
        [
            inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, annotation=annotation)
            for name, annotation in remote.annotations
        ]
    )
    return call


def mcp_stdio_plugin(snapshot_json: str | bytes) -> tuple[PluginManifest, PluginRegistration]:
    snapshot = _parse_snapshot(snapshot_json)
    revision = f"mcp-stdio-v1:{snapshot.sha256}"
    tools = []
    for remote in snapshot.tools:
        tool = Tool(
            remote.name,
            _handler(snapshot, remote),
            remote.description,
            effect=remote.effect,  # type: ignore[arg-type]
            tool_revision=revision,
        )
        if not json_values_equal(compile_tool_schema(tool), remote.schema):
            raise MCPConfigurationError("MCP tool schema cannot be represented by Sasori")
        tools.append(tool)
    registration = PluginRegistration(1, snapshot.plugin_id, snapshot.version, tuple(tools))
    secrets = sorted(
        source.from_env
        for _, source in snapshot.command.environment
        if source.from_env is not None
    )
    manifest_value = {
        "schema_version": 1,
        "plugin_id": snapshot.plugin_id,
        "name": f"MCP stdio {snapshot.plugin_id}",
        "version": snapshot.version,
        "summary": "Administrator-frozen MCP stdio tools executed as trusted local child processes.",
        "distribution": "sasori",
        "execution": {
            "mode": "trusted_process",
            "entry_point_group": "sasori.plugins",
            "entry_point_name": snapshot.plugin_id,
            "entry_point_value": "sasori_plugins.mcp_stdio:register",
        },
        "permissions": {
            "filesystem_read": ["workspace:**/*"],
            "filesystem_write": ["workspace:**/*"],
            "network_egress": ["mcp-child:unrestricted"],
            "host_process": [f"mcp-stdio:{Path(snapshot.command.argv[0]).name}"],
            "secrets": secrets,
        },
        "tools": [
            {
                "name": tool.name,
                "effect": tool.effect,
                "tool_revision": tool.tool_revision,
                "schema_sha256": tool_schema_sha256(tool),
            }
            for tool in tools
        ],
        "skills": [],
        "workers": [],
        "dependencies": [],
    }
    manifest = parse_manifest(_canonical(manifest_value))
    validate_registration(manifest, registration)
    return manifest, registration


def load_snapshot_file(path: str | os.PathLike[str]) -> bytes:
    try:
        with Path(path).open("rb") as stream:
            value = stream.read(_MAX_SNAPSHOT_BYTES + 1)
    except OSError:
        raise MCPConfigurationError("MCP snapshot file could not be read") from None
    if len(value) > _MAX_SNAPSHOT_BYTES:
        raise MCPConfigurationError("MCP snapshot exceeds the size limit")
    return value


def register(snapshot_json: str | bytes) -> PluginRegistration:
    """Host-only factory. This module intentionally has no installed entry point."""
    return mcp_stdio_plugin(snapshot_json)[1]


__all__ = [
    "MCPConfigurationError",
    "MCPProtocolError",
    "MCPRemoteToolError",
    "MCPStdioError",
    "MCPTimeoutError",
    "load_snapshot_file",
    "mcp_stdio_plugin",
    "register",
]
