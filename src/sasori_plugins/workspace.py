"""Bounded workspace tools for trusted installed plugins.

Path checks reject static escapes in model-supplied paths. They do not defend
against another local actor replacing a checked component with a symlink or
junction before use. ``trusted_process`` has full host privileges; this module
is not a sandbox.
"""

from __future__ import annotations

import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from sasori.contracts import PluginRegistration, Tool
from sasori.plugins import PluginManifest, parse_manifest


PLUGIN_ID = "com.sasori.workspace"
PLUGIN_VERSION = "0.1.0.dev0"
_RESERVED_WINDOWS_NAMES = {
    "CON",
    "CONIN$",
    "CONOUT$",
    "PRN",
    "AUX",
    "NUL",
    "CLOCK$",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


class WorkspaceError(Exception):
    pass


class WorkspacePathError(WorkspaceError):
    pass


class WorkspaceLimitError(WorkspaceError):
    pass


class WorkspaceFileError(WorkspaceError):
    pass


@dataclass(frozen=True, slots=True)
class WorkspaceLimits:
    max_depth: int = 8
    max_files: int = 1000
    max_file_bytes: int = 1024 * 1024
    max_output_chars: int = 256 * 1024

    def __post_init__(self) -> None:
        for name in (
            "max_depth",
            "max_files",
            "max_file_bytes",
            "max_output_chars",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")


class _Workspace:
    def __init__(self, root: str | Path, limits: WorkspaceLimits) -> None:
        try:
            resolved = Path(root).resolve(strict=True)
        except (OSError, RuntimeError):
            raise WorkspacePathError("workspace root does not exist") from None
        if not resolved.is_dir():
            raise WorkspacePathError("workspace root must be a directory")
        self.root = resolved
        self.limits = limits

    @staticmethod
    def _parts(value: object) -> tuple[str, ...]:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 4096
            or any(
                ord(character) < 32
                or ord(character) == 127
                or 0xD800 <= ord(character) <= 0xDFFF
                for character in value
            )
        ):
            raise WorkspacePathError("workspace path must be bounded relative text")
        if "\x00" in value:
            raise WorkspacePathError("workspace path contains an invalid character")
        normalized = value.replace("\\", "/")
        windows = PureWindowsPath(value)
        posix = PurePosixPath(normalized)
        raw_parts = normalized.split("/")
        if (
            windows.is_absolute()
            or bool(windows.drive)
            or posix.is_absolute()
            or normalized.startswith("//")
            or any(part == ".." for part in raw_parts)
            or any(part == "" for part in raw_parts)
        ):
            raise WorkspacePathError("workspace path must stay relative")
        result = tuple(part for part in raw_parts if part != ".")
        for part in result:
            if (
                ":" in part
                or part != part.rstrip(" .")
                or part.split(".", 1)[0].upper() in _RESERVED_WINDOWS_NAMES
            ):
                raise WorkspacePathError("workspace path uses a device or unsafe name")
        return result

    def _contains(self, path: Path) -> bool:
        try:
            path.relative_to(self.root)
        except ValueError:
            return False
        return True

    @staticmethod
    def _is_reparse(path: Path) -> bool:
        try:
            metadata = path.lstat()
        except OSError:
            return False
        attributes = getattr(metadata, "st_file_attributes", 0)
        return path.is_symlink() or bool(
            attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        )

    def _resolve_existing(self, value: object) -> Path:
        parts = self._parts(value)
        candidate = self.root.joinpath(*parts)
        current = self.root
        for part in parts[:-1]:
            current /= part
            if self._is_reparse(current):
                raise WorkspacePathError(
                    "linked workspace directories are not traversed"
                )
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            raise WorkspacePathError("workspace path does not exist") from None
        if not self._contains(resolved):
            raise WorkspacePathError("workspace path escapes the configured root")
        if resolved.is_dir() and self._is_reparse(candidate):
            raise WorkspacePathError("linked workspace directories are not traversed")
        return resolved

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix() or "."

    def _files(self, value: object) -> tuple[Path, ...]:
        target = self._resolve_existing(value)
        if target.is_file():
            return (target,)
        if not target.is_dir():
            raise WorkspaceFileError("workspace path is not a regular file or directory")

        files: list[Path] = []
        seen_files: set[Path] = set()
        seen_directories: set[Path] = {target}
        pending: list[tuple[Path, int]] = [(target, 0)]
        while pending:
            directory, depth = pending.pop()
            try:
                entries = sorted(os.scandir(directory), key=lambda item: item.name)
            except OSError:
                raise WorkspaceFileError("workspace directory could not be read") from None
            for entry in entries:
                self._parts(entry.name)
                candidate = Path(entry.path)
                try:
                    resolved = candidate.resolve(strict=True)
                except (OSError, RuntimeError):
                    raise WorkspacePathError("workspace entry could not be resolved") from None
                if not self._contains(resolved):
                    raise WorkspacePathError("workspace entry escapes the configured root")
                linked = self._is_reparse(candidate)
                if resolved.is_dir():
                    if linked:
                        raise WorkspacePathError(
                            "linked workspace directories are not traversed"
                        )
                    next_depth = depth + 1
                    if next_depth > self.limits.max_depth:
                        raise WorkspaceLimitError("workspace depth limit exceeded")
                    if resolved not in seen_directories:
                        seen_directories.add(resolved)
                        pending.append((resolved, next_depth))
                    continue
                if not resolved.is_file():
                    raise WorkspaceFileError("workspace contains a non-regular file")
                if resolved in seen_files:
                    continue
                seen_files.add(resolved)
                files.append(resolved)
                if len(files) > self.limits.max_files:
                    raise WorkspaceLimitError("workspace file-count limit exceeded")
        return tuple(sorted(files, key=self._relative))

    def _read(self, path: Path) -> str:
        try:
            with path.open("rb") as stream:
                raw = stream.read(self.limits.max_file_bytes + 1)
        except OSError:
            raise WorkspaceFileError("workspace file could not be read") from None
        if len(raw) > self.limits.max_file_bytes:
            raise WorkspaceLimitError("workspace file-size limit exceeded")
        if b"\x00" in raw or any(
            byte < 32 and byte not in (9, 10, 13) for byte in raw
        ):
            raise WorkspaceFileError("workspace file is binary")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise WorkspaceFileError("workspace file is not valid UTF-8") from None
        if len(text) > self.limits.max_output_chars:
            raise WorkspaceLimitError("workspace output limit exceeded")
        return text

    def list_files(self, path: str = ".") -> list[str]:
        result = [self._relative(item) for item in self._files(path)]
        if sum(len(item) + 1 for item in result) > self.limits.max_output_chars:
            raise WorkspaceLimitError("workspace output limit exceeded")
        return result

    def read_text(self, path: str) -> str:
        target = self._resolve_existing(path)
        if not target.is_file():
            raise WorkspaceFileError("workspace path is not a regular file")
        return self._read(target)

    def search_text(self, query: str, path: str = ".") -> list[str]:
        if (
            not isinstance(query, str)
            or not query
            or len(query) > 1024
            or any(0xD800 <= ord(character) <= 0xDFFF for character in query)
        ):
            raise WorkspaceFileError("search query must be bounded Unicode text")
        target = self._resolve_existing(path)
        direct_file = target.is_file()
        result: list[str] = []
        output_chars = 0
        for file_path in self._files(path):
            try:
                text = self._read(file_path)
            except WorkspaceFileError:
                if direct_file:
                    raise
                continue
            relative = self._relative(file_path)
            for line_number, line in enumerate(text.splitlines(), 1):
                if query not in line:
                    continue
                match = f"{relative}:{line_number}:{line}"
                output_chars += len(match) + 1
                if output_chars > self.limits.max_output_chars:
                    raise WorkspaceLimitError("workspace output limit exceeded")
                result.append(match)
        return result

    def write_text(self, path: str, content: str) -> dict[str, object]:
        if not isinstance(content, str):
            raise WorkspaceFileError("workspace content must be text")
        try:
            encoded = content.encode("utf-8")
        except UnicodeEncodeError:
            raise WorkspaceFileError("workspace content must be valid Unicode") from None
        if len(encoded) > self.limits.max_file_bytes:
            raise WorkspaceLimitError("workspace file-size limit exceeded")
        if b"\x00" in encoded or any(
            byte < 32 and byte not in (9, 10, 13) for byte in encoded
        ):
            raise WorkspaceFileError("workspace content contains binary controls")

        parts = self._parts(path)
        if not parts:
            raise WorkspacePathError("workspace write path must name a file")
        parent_path = "/".join(parts[:-1]) or "."
        try:
            parent = self._resolve_existing(parent_path)
        except WorkspacePathError:
            raise WorkspacePathError("workspace write parent does not exist") from None
        if not parent.is_dir():
            raise WorkspacePathError("workspace write parent is not a directory")
        candidate = parent / parts[-1]
        if candidate.exists() or candidate.is_symlink():
            try:
                resolved = candidate.resolve(strict=True)
            except (OSError, RuntimeError):
                raise WorkspacePathError("workspace write target is invalid") from None
            if not self._contains(resolved):
                raise WorkspacePathError("workspace write target escapes the configured root")
            if self._is_reparse(candidate):
                raise WorkspacePathError("workspace writes do not replace links")
            if not resolved.is_file():
                raise WorkspaceFileError("workspace write target is not a regular file")

        descriptor = -1
        temporary_name = ""
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{candidate.name}.", suffix=".tmp", dir=parent
            )
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                descriptor = -1
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if not self._contains(parent.resolve(strict=True)):
                raise WorkspacePathError("workspace write parent changed")
            os.replace(temporary_name, candidate)
            temporary_name = ""
        except WorkspaceError:
            raise
        except OSError:
            raise WorkspaceFileError("workspace file could not be written") from None
        finally:
            if descriptor != -1:
                os.close(descriptor)
            if temporary_name:
                try:
                    Path(temporary_name).unlink()
                except OSError:
                    pass
        return {"path": "/".join(parts), "bytes": len(encoded)}


def workspace_registration(
    root: str | Path,
    *,
    limits: WorkspaceLimits | None = None,
) -> PluginRegistration:
    workspace = _Workspace(root, limits or WorkspaceLimits())
    return PluginRegistration(
        api_version=1,
        plugin_id=PLUGIN_ID,
        version=PLUGIN_VERSION,
        tools=(
            Tool(
                "list_files",
                workspace.list_files,
                "List bounded files under the configured workspace root.",
                effect="read_only",
            ),
            Tool(
                "read_text",
                workspace.read_text,
                "Read one bounded UTF-8 text file from the workspace.",
                effect="read_only",
            ),
            Tool(
                "search_text",
                workspace.search_text,
                "Search bounded UTF-8 workspace files for literal text.",
                effect="read_only",
            ),
            Tool(
                "write_text",
                workspace.write_text,
                "Atomically replace one UTF-8 text file inside the workspace.",
                effect="side_effecting",
                tool_revision="1",
            ),
        ),
    )


_WORKSPACE_MANIFEST_JSON = """
{
  "schema_version": 1,
  "plugin_id": "com.sasori.workspace",
  "name": "Sasori Workspace",
  "version": "0.1.0.dev0",
  "summary": "Contained first-party workspace text tools.",
  "distribution": "sasori",
  "execution": {
    "mode": "trusted_process",
    "entry_point_group": "sasori.plugins",
    "entry_point_name": "com.sasori.workspace",
    "entry_point_value": "sasori_plugins.workspace:register"
  },
  "permissions": {
    "filesystem_read": ["workspace:**/*"],
    "filesystem_write": ["workspace:**/*"],
    "network_egress": [],
    "host_process": [],
    "secrets": []
  },
  "tools": [
    {
      "name": "list_files",
      "effect": "read_only",
      "tool_revision": null,
      "schema_sha256": "c3a2754c93dd161df274be539239b48678b1af3bf1828f866bd6fc76a4aa25ee"
    },
    {
      "name": "read_text",
      "effect": "read_only",
      "tool_revision": null,
      "schema_sha256": "e3d2acdfc3d823967c5c41f832157dc6ab801a8c24b708af7ae54377764d48bd"
    },
    {
      "name": "search_text",
      "effect": "read_only",
      "tool_revision": null,
      "schema_sha256": "9917f47c58e5736111ae7d8b79fccf0c5fd5f8b5f23e2640df2a8cba086af87e"
    },
    {
      "name": "write_text",
      "effect": "side_effecting",
      "tool_revision": "1",
      "schema_sha256": "0c2011ac7bbda2024b32d43fb42cd73c4dc5b028b638783e22089965b6340ab9"
    }
  ],
  "skills": [],
  "workers": [],
  "dependencies": []
}
"""


def workspace_manifest() -> PluginManifest:
    return parse_manifest(_WORKSPACE_MANIFEST_JSON)


def register() -> PluginRegistration:
    """Trusted entry point; installed code runs with the process's full privileges."""
    return workspace_registration(Path.cwd())


__all__ = [
    "PLUGIN_ID",
    "PLUGIN_VERSION",
    "WorkspaceError",
    "WorkspaceFileError",
    "WorkspaceLimitError",
    "WorkspaceLimits",
    "WorkspacePathError",
    "register",
    "workspace_manifest",
    "workspace_registration",
]
