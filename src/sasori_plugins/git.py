from __future__ import annotations

import hashlib
import fnmatch
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Iterable

from sasori import PluginRegistration, Tool
from sasori.plugins import PluginManifest, parse_manifest


PLUGIN_ID = "com.sasori.git"
PLUGIN_VERSION = "0.1.0.dev0"
GIT_ROOT_ENV = "SASORI_GIT_ROOT"

_MAX_OUTPUT_BYTES = 512 * 1024
_MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
_MAX_SNAPSHOT_FILE_BYTES = 64 * 1024 * 1024
_MAX_PATHS = 128
_MAX_PATH_CHARS = 4096
_MAX_COMMIT_MESSAGE_BYTES = 16 * 1024
_COMMAND_TIMEOUT = 20.0
_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_SNAPSHOT = re.compile(r"[0-9a-f]{64}\Z")
_SENSITIVE_DIRECTORIES = frozenset({".ssh", ".aws", ".azure", ".kube", ".gnupg"})
_SENSITIVE_NAMES = frozenset({".git-credentials", ".netrc", ".npmrc", ".pypirc", "id_rsa", "id_ed25519"})
_SENSITIVE_PATTERNS = (".env", ".env.*", "credentials", "credentials.*", "secrets", "secrets.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.kdbx")


class GitPluginError(Exception):
    pass


class GitConfigurationError(GitPluginError):
    pass


class GitValidationError(GitPluginError):
    pass


class GitCommandError(GitPluginError):
    pass


class GitOutputLimitError(GitPluginError):
    pass


def _sensitive_path(value: str) -> bool:
    parts = tuple(part.lower() for part in value.replace("\\", "/").split("/"))
    name = parts[-1]
    return (
        any(part in _SENSITIVE_DIRECTORIES for part in parts)
        or name in _SENSITIVE_NAMES
        or any(fnmatch.fnmatchcase(name, pattern) for pattern in _SENSITIVE_PATTERNS)
    )


def _safe_text(value: object, name: str, *, byte_limit: int) -> str:
    if not isinstance(value, str) or not value:
        raise GitValidationError(f"{name} must be non-empty text")
    if any(
        ord(character) < 32 and character not in "\t\n\r"
        or ord(character) == 127
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    ):
        raise GitValidationError(f"{name} contains invalid characters")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise GitValidationError(f"{name} must be valid UTF-8 text") from None
    if len(encoded) > byte_limit:
        raise GitValidationError(f"{name} exceeds the size limit")
    return value


class _GitRepository:
    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        git_executable: str | os.PathLike[str] | None = None,
        command_timeout: float = _COMMAND_TIMEOUT,
    ) -> None:
        try:
            resolved = Path(root).resolve(strict=True)
        except (OSError, TypeError, ValueError):
            raise GitConfigurationError("Git root is unavailable") from None
        if not resolved.is_dir():
            raise GitConfigurationError("Git root must be a directory")
        executable = str(git_executable) if git_executable is not None else shutil.which("git")
        if not executable:
            raise GitConfigurationError("Git executable was not found")
        try:
            executable_path = Path(executable).resolve(strict=True)
        except OSError:
            raise GitConfigurationError("Git executable is unavailable") from None
        if type(command_timeout) not in (int, float) or not 0 < command_timeout <= 120:
            raise GitConfigurationError("Git command timeout is invalid")
        self.root = resolved
        self.git = executable_path
        self.timeout = float(command_timeout)
        code, output = self._run(("rev-parse", "--show-toplevel"), limit=8192)
        if code != 0:
            raise GitConfigurationError("configured Git root is not a repository")
        try:
            top_level = Path(self._decode(output)).resolve(strict=True)
        except (OSError, ValueError):
            raise GitConfigurationError("Git repository root is invalid") from None
        if top_level != self.root:
            raise GitConfigurationError("configured Git root must be the repository root")
        code, output = self._run(("rev-parse", "--is-bare-repository"), limit=32)
        if code != 0 or self._decode(output) != "false":
            raise GitConfigurationError("bare Git repositories are unsupported")
        code, output = self._run(("rev-parse", "--absolute-git-dir"), limit=8192)
        if code != 0:
            raise GitConfigurationError("Git metadata directory is unavailable")
        try:
            self.git_dir = Path(self._decode(output)).resolve(strict=True)
        except (OSError, ValueError):
            raise GitConfigurationError("Git metadata directory is invalid") from None
        if not self.git_dir.is_dir():
            raise GitConfigurationError("Git metadata directory is invalid")

    def _environment(self, empty_home: str) -> dict[str, str]:
        environment = {
            key: os.environ[key]
            for key in ("SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP")
            if key in os.environ
        }
        environment.update(
            {
                "HOME": empty_home,
                "XDG_CONFIG_HOME": empty_home,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_ATTR_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "GCM_INTERACTIVE": "Never",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_PAGER": "cat",
                "PAGER": "cat",
                "LC_ALL": "C",
                "LANG": "C",
            }
        )
        return environment

    def _run(
        self,
        arguments: Iterable[str],
        *,
        limit: int = _MAX_OUTPUT_BYTES,
    ) -> tuple[int, bytes]:
        with tempfile.TemporaryDirectory(prefix="sasori-git-") as empty_home:
            command = (
                str(self.git),
                "--no-pager",
                "-c",
                f"core.hooksPath={empty_home}",
                "-c",
                "credential.helper=",
                "-c",
                "commit.gpgSign=false",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                "-c",
                "gc.auto=0",
                "-C",
                str(self.root),
                "--literal-pathspecs",
                *arguments,
            )
            with tempfile.TemporaryFile() as output:
                try:
                    process = subprocess.Popen(
                        command,
                        stdin=subprocess.DEVNULL,
                        stdout=output,
                        stderr=subprocess.DEVNULL,
                        env=self._environment(empty_home),
                        shell=False,
                    )
                except OSError:
                    raise GitCommandError("Git command could not start") from None
                deadline = time.monotonic() + self.timeout
                exceeded = False
                while process.poll() is None:
                    if output.tell() > limit:
                        exceeded = True
                        process.kill()
                        break
                    if time.monotonic() >= deadline:
                        process.kill()
                        process.wait()
                        raise GitCommandError("Git command timed out")
                    time.sleep(0.01)
                process.wait()
                output.seek(0, os.SEEK_END)
                size = output.tell()
                if exceeded or size > limit:
                    raise GitOutputLimitError("Git output exceeds the configured limit")
                output.seek(0)
                return process.returncode, output.read()

    @staticmethod
    def _decode(value: bytes) -> str:
        try:
            return value.decode("utf-8").rstrip("\r\n")
        except UnicodeDecodeError:
            raise GitCommandError("Git output is not valid UTF-8") from None

    def _checked(self, arguments: Iterable[str], *, limit: int = _MAX_OUTPUT_BYTES) -> bytes:
        code, output = self._run(arguments, limit=limit)
        if code != 0:
            raise GitCommandError("Git command failed")
        return output

    def _path(self, value: object) -> str:
        if not isinstance(value, str) or not value or len(value) > _MAX_PATH_CHARS:
            raise GitValidationError("Git path is invalid")
        if (
            value.startswith(("/", "\\"))
            or "\\" in value
            or ":" in value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise GitValidationError("Git path must be a safe relative POSIX path")
        parts = value.split("/")
        if any(part in ("", ".", "..") for part in parts) or parts[0].lower() == ".git":
            raise GitValidationError("Git path must stay inside the worktree")
        if _sensitive_path(value):
            raise GitValidationError("Git path is classified as sensitive")
        candidate = self.root.joinpath(*parts)
        existing = candidate
        while not existing.exists():
            if existing == self.root:
                break
            existing = existing.parent
        try:
            resolved = existing.resolve(strict=True)
            resolved.relative_to(self.root)
        except (OSError, ValueError):
            raise GitValidationError("Git path escapes the worktree") from None
        if candidate.is_dir():
            raise GitValidationError("Git paths must identify files, not directories")
        if not candidate.exists() and not candidate.is_symlink():
            code, _ = self._run(("ls-files", "--error-unmatch", "--", value), limit=256)
            if code != 0:
                raise GitValidationError("missing Git paths must already be tracked")
        return "/".join(parts)

    def _paths(self, values: object) -> tuple[str, ...]:
        if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= _MAX_PATHS:
            raise GitValidationError("paths must be a non-empty bounded array")
        paths = tuple(self._path(value) for value in values)
        if len(set(paths)) != len(paths):
            raise GitValidationError("paths must not contain duplicates")
        return paths

    def _head(self) -> str | None:
        code, output = self._run(("rev-parse", "--verify", "--quiet", "HEAD"), limit=256)
        if code == 1:
            return None
        if code != 0:
            raise GitCommandError("Git HEAD could not be read")
        head = self._decode(output)
        if _OBJECT_ID.fullmatch(head) is None:
            raise GitCommandError("Git HEAD is invalid")
        return head

    def _branch(self) -> str | None:
        code, output = self._run(("symbolic-ref", "--quiet", "--short", "HEAD"), limit=4096)
        if code == 1:
            return None
        if code != 0:
            raise GitCommandError("Git branch could not be read")
        return self._decode(output)

    @staticmethod
    def _status_records(value: bytes) -> tuple[tuple[str, tuple[bytes, ...]], ...]:
        fields = value.split(b"\0")
        if not fields or fields[-1] != b"":
            raise GitCommandError("Git status output is malformed")
        records = []
        index = 0
        while index < len(fields) - 1:
            record = fields[index]
            index += 1
            if len(record) < 4 or record[2:3] != b" ":
                raise GitCommandError("Git status output is malformed")
            try:
                status = record[:2].decode("ascii")
            except UnicodeDecodeError:
                raise GitCommandError("Git status output is malformed") from None
            paths = [record[3:]]
            if "R" in status or "C" in status:
                if index >= len(fields) - 1 or not fields[index]:
                    raise GitCommandError("Git status output is malformed")
                paths.append(fields[index])
                index += 1
            if any(not path for path in paths):
                raise GitCommandError("Git status output is malformed")
            records.append((status, tuple(paths)))
        return tuple(records)

    def _worktree_fingerprint(self, value: bytes) -> tuple[str, str]:
        digest = hashlib.sha256(b"sasori-git-worktree-v1\0" + value)
        total = 0
        lines = []
        seen: set[bytes] = set()
        for status, paths in self._status_records(value):
            rendered = [json.dumps(os.fsdecode(path), ensure_ascii=True)[1:-1] for path in paths]
            lines.append(f"{status} " + " -> ".join(rendered))
            for raw_path in paths:
                if raw_path in seen:
                    continue
                seen.add(raw_path)
                decoded = os.fsdecode(raw_path)
                digest.update(b"\0path\0" + raw_path)
                if _sensitive_path(decoded):
                    digest.update(b"\0sensitive\0")
                    continue
                parts = decoded.replace("\\", "/").split("/")
                if any(part in ("", ".", "..") for part in parts):
                    raise GitCommandError("Git status path is unsafe")
                candidate = self.root.joinpath(*parts)
                try:
                    candidate.parent.resolve(strict=True).relative_to(self.root)
                    metadata = candidate.lstat()
                except FileNotFoundError:
                    digest.update(b"\0missing\0")
                    continue
                except (OSError, ValueError):
                    raise GitCommandError("Git status path escapes the worktree") from None
                if candidate.is_symlink():
                    target = os.fsencode(os.readlink(candidate))
                    digest.update(b"\0symlink\0" + target)
                    continue
                if not candidate.is_file():
                    digest.update(b"\0non-file\0" + str(metadata.st_mode).encode("ascii"))
                    continue
                digest.update(
                    b"\0file\0"
                    + str(metadata.st_size).encode("ascii")
                    + b"\0exec\0"
                    + str(metadata.st_mode & 0o111).encode("ascii")
                    + b"\0"
                )
                try:
                    with candidate.open("rb") as stream:
                        while chunk := stream.read(128 * 1024):
                            total += len(chunk)
                            if total > _MAX_SNAPSHOT_FILE_BYTES:
                                raise GitOutputLimitError("Git snapshot files exceed the configured limit")
                            digest.update(chunk)
                except GitPluginError:
                    raise
                except OSError:
                    raise GitCommandError("Git worktree file could not be read") from None
        return digest.hexdigest(), "\n".join(lines)

    def _state(self, worktree: bytes | None = None) -> dict[str, str | None]:
        head = self._head()
        branch = self._branch()
        index = self._checked(("ls-files", "--stage", "-z"), limit=_MAX_SNAPSHOT_BYTES)
        if worktree is None:
            worktree = self._checked(
                ("status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored=no", "--ignore-submodules=none"),
                limit=_MAX_SNAPSHOT_BYTES,
            )
        index_sha256 = hashlib.sha256(index).hexdigest()
        worktree_sha256, _ = self._worktree_fingerprint(worktree)
        snapshot = hashlib.sha256(
            json.dumps(
                ["sasori-git-snapshot-v1", head, branch, index_sha256, worktree_sha256],
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("ascii")
        ).hexdigest()
        return {
            "head": head,
            "branch": branch,
            "index_sha256": index_sha256,
            "worktree_sha256": worktree_sha256,
            "snapshot": snapshot,
        }

    def _expected(self, value: object) -> tuple[dict[str, str | None], bool]:
        if not isinstance(value, str) or _SNAPSHOT.fullmatch(value) is None:
            raise GitValidationError("expected_snapshot must be a SHA-256 value")
        state = self._state()
        return state, state["snapshot"] == value

    def _operation_in_progress(self) -> bool:
        return any(
            (self.git_dir / name).exists()
            for name in (
                "MERGE_HEAD",
                "CHERRY_PICK_HEAD",
                "REVERT_HEAD",
                "rebase-apply",
                "rebase-merge",
            )
        )

    def _has_unmerged_paths(self) -> bool:
        return bool(
            self._checked(
                ("diff", "--name-only", "--diff-filter=U", "-z", "--"),
                limit=_MAX_SNAPSHOT_BYTES,
            )
        )

    def _has_filters(self, paths: tuple[str, ...]) -> bool:
        output = self._checked(("check-attr", "-z", "filter", "--", *paths), limit=64 * 1024)
        fields = output.split(b"\0")
        if not fields or fields[-1] != b"" or (len(fields) - 1) % 3:
            raise GitCommandError("Git attribute output is malformed")
        return any(fields[index] != b"unspecified" for index in range(2, len(fields) - 1, 3))

    def _staged_paths(self) -> tuple[str, ...]:
        output = self._checked(("diff", "--cached", "--name-only", "-z", "--"), limit=_MAX_SNAPSHOT_BYTES)
        if not output:
            return ()
        fields = output.split(b"\0")
        if fields[-1] != b"" or any(not field for field in fields[:-1]):
            raise GitCommandError("Git staged path output is malformed")
        return tuple(os.fsdecode(field).replace("\\", "/") for field in fields[:-1])

    def git_status(self) -> dict[str, object]:
        worktree = self._checked(
            ("status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored=no", "--ignore-submodules=none"),
            limit=_MAX_SNAPSHOT_BYTES,
        )
        _, porcelain = self._worktree_fingerprint(worktree)
        return {**self._state(worktree), "porcelain": porcelain}

    def git_diff(self, path: str, staged: bool = False) -> dict[str, object]:
        if type(staged) is not bool:
            raise GitValidationError("staged must be a boolean")
        normalized = self._path(path)
        arguments = ["diff", "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames", "--patch"]
        if staged:
            arguments.append("--cached")
        arguments.extend(("--", normalized))
        return {"path": normalized, "staged": staged, "patch": self._decode(self._checked(arguments))}

    def git_log(self, limit: int = 20) -> dict[str, str]:
        if type(limit) is not int or not 1 <= limit <= 50:
            raise GitValidationError("limit must be an integer from 1 to 50")
        output = self._checked(
            (
                "log",
                f"--max-count={limit}",
                "--no-decorate",
                "--encoding=UTF-8",
                "--format=%H%x09%P%x09%aI",
                "--",
            )
        )
        return {"log": self._decode(output)}

    def git_show(self, object_id: str, path: str) -> dict[str, str]:
        if not isinstance(object_id, str) or _OBJECT_ID.fullmatch(object_id) is None:
            raise GitValidationError("object_id must be a full lowercase Git object ID")
        normalized = self._path(path)
        output = self._checked(
            (
                "show",
                "--no-ext-diff",
                "--no-textconv",
                "--no-color",
                "--no-renames",
                "--encoding=UTF-8",
                "--format=",
                object_id,
                "--",
                normalized,
            )
        )
        return {"object_id": object_id, "path": normalized, "content": self._decode(output)}

    def git_stage(self, paths: list[str], expected_snapshot: str) -> dict[str, object]:
        try:
            normalized = self._paths(paths)
            before, current = self._expected(expected_snapshot)
        except GitValidationError:
            return {"operation": "stage", "outcome": "invalid_arguments"}
        if not current:
            return {
                "operation": "stage",
                "outcome": "stale_snapshot",
                "paths": normalized,
                "expected_snapshot": expected_snapshot,
                "actual": before,
            }
        if self._operation_in_progress() or self._has_unmerged_paths():
            return {"operation": "stage", "outcome": "unsupported_repository_state", "before": before}
        if self._has_filters(normalized):
            return {"operation": "stage", "outcome": "unsupported_filter", "paths": normalized, "before": before}
        self._checked(("add", "--all", "--", *normalized))
        after = self._state()
        return {
            "operation": "stage",
            "outcome": "changed" if after["snapshot"] != before["snapshot"] else "no_change",
            "paths": normalized,
            "before": before,
            "after": after,
        }

    def git_commit(self, message: str, expected_snapshot: str) -> dict[str, object]:
        try:
            message = _safe_text(message, "message", byte_limit=_MAX_COMMIT_MESSAGE_BYTES)
            before, current = self._expected(expected_snapshot)
        except GitValidationError:
            return {"operation": "commit", "outcome": "invalid_arguments", "commit": None}
        if not current:
            return {
                "operation": "commit",
                "outcome": "stale_snapshot",
                "commit": None,
                "expected_snapshot": expected_snapshot,
                "actual": before,
            }
        if self._operation_in_progress() or self._has_unmerged_paths():
            return {"operation": "commit", "outcome": "unsupported_repository_state", "commit": None, "before": before}
        staged_paths = self._staged_paths()
        if any(_sensitive_path(path) for path in staged_paths):
            return {"operation": "commit", "outcome": "sensitive_path", "commit": None, "before": before}
        code, _ = self._run(
            ("diff", "--cached", "--quiet", "--no-ext-diff", "--no-textconv", "--")
        )
        if code == 0:
            return {
                "operation": "commit",
                "outcome": "no_change",
                "commit": None,
                "before": before,
                "after": before,
            }
        if code != 1:
            raise GitCommandError("Git index could not be inspected")
        self._checked(
            (
                "commit",
                "--no-verify",
                "--no-gpg-sign",
                "--cleanup=verbatim",
                "-m",
                message,
                "--",
            )
        )
        after = self._state()
        commit = after["head"]
        if commit is None or commit == before["head"]:
            raise GitCommandError("Git commit could not be verified")
        return {
            "operation": "commit",
            "outcome": "changed",
            "commit": commit,
            "before": before,
            "after": after,
        }


def git_registration(
    root: str | os.PathLike[str],
    *,
    _git_executable: str | os.PathLike[str] | None = None,
    _command_timeout: float = _COMMAND_TIMEOUT,
) -> PluginRegistration:
    repository = _GitRepository(
        root,
        git_executable=_git_executable,
        command_timeout=_command_timeout,
    )
    return PluginRegistration(
        api_version=1,
        plugin_id=PLUGIN_ID,
        version=PLUGIN_VERSION,
        tools=(
            Tool("git_status", repository.git_status, "Read bounded local repository status and a mutation snapshot.", effect="read_only"),
            Tool("git_diff", repository.git_diff, "Read a bounded worktree diff without external diff drivers.", effect="read_only"),
            Tool("git_log", repository.git_log, "Read a bounded local commit log.", effect="read_only"),
            Tool("git_show", repository.git_show, "Read one full local Git object ID with bounded output.", effect="read_only"),
            Tool("git_stage", repository.git_stage, "Stage explicit worktree paths when the approved repository snapshot is current.", effect="side_effecting", tool_revision="1"),
            Tool("git_commit", repository.git_commit, "Create a local commit when the approved repository snapshot is current.", effect="side_effecting", tool_revision="1"),
        ),
    )


_GIT_MANIFEST_JSON = """
{
  "schema_version": 1,
  "plugin_id": "com.sasori.git",
  "name": "Sasori Git",
  "version": "0.1.0.dev0",
  "summary": "Bounded local Git inspection, staging, and commit tools.",
  "distribution": "sasori",
  "execution": {
    "mode": "trusted_process",
    "entry_point_group": "sasori.plugins",
    "entry_point_name": "com.sasori.git",
    "entry_point_value": "sasori_plugins.git:register"
  },
  "permissions": {
    "filesystem_read": ["workspace:**/*"],
    "filesystem_write": ["workspace:**/*"],
    "network_egress": [],
    "host_process": ["git:local-repository"],
    "secrets": []
  },
  "tools": [
    {"name":"git_status","effect":"read_only","tool_revision":null,"schema_sha256":"c7c69ffea7e3e6af99994c9347a2f29c4a558bdf8854ac3b61df817cbadcd1f7"},
    {"name":"git_diff","effect":"read_only","tool_revision":null,"schema_sha256":"e90a6209f99e7f8d36e3f939d64982718f4cc7228982bc2d488fe25bd9f18059"},
    {"name":"git_log","effect":"read_only","tool_revision":null,"schema_sha256":"da883a35c30aea87034ddc202953f51b2854b0c9c256e7e1a2f599bf175a5fef"},
    {"name":"git_show","effect":"read_only","tool_revision":null,"schema_sha256":"c7f452a2ef8999e67c1980be379b05c54b748321de84a095a9600b0bd00855d9"},
    {"name":"git_stage","effect":"side_effecting","tool_revision":"1","schema_sha256":"7c8b0bb6b083b89d7cd16ccc962eea9d83b6184796ff579afede68b2e0fc17ce"},
    {"name":"git_commit","effect":"side_effecting","tool_revision":"1","schema_sha256":"0a3442f18bc48aacba27f79312e65b200e1df57ee5c3c3ceb4b9100ae5d8bb77"}
  ],
  "skills": [],
  "workers": [],
  "dependencies": []
}
"""


def git_manifest() -> PluginManifest:
    return parse_manifest(_GIT_MANIFEST_JSON)


def register() -> PluginRegistration:
    """Trusted entry point for the explicitly configured local repository."""
    root = os.environ.get(GIT_ROOT_ENV, "").strip() or str(Path.cwd())
    return git_registration(root)


__all__ = [
    "GIT_ROOT_ENV",
    "GitCommandError",
    "GitConfigurationError",
    "GitOutputLimitError",
    "GitPluginError",
    "GitValidationError",
    "PLUGIN_ID",
    "PLUGIN_VERSION",
    "git_manifest",
    "git_registration",
    "register",
]
