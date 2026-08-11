from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path


PROJECT = "sasori-core"
INDEX = "https://test.pypi.org/pypi/sasori-core/json"
MAX_JSON = 4 * 1024 * 1024


class GateError(RuntimeError):
    pass


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _version(root: Path) -> str:
    with (root / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)["project"]
    version = project.get("version")
    if (
        project.get("name") != PROJECT
        or not isinstance(version, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", version) is None
        or project.get("dependencies") != []
    ):
        raise GateError("sasori-core project metadata is invalid")
    return version


def _expected(version: str) -> set[str]:
    return {
        f"sasori_core-{version}-py3-none-any.whl",
        f"sasori_core-{version}.tar.gz",
    }


def _inventory(root: Path, version: str) -> dict[str, dict[str, object]]:
    if root.is_symlink() or not root.is_dir():
        raise GateError("core artifact directory is invalid")
    files = list(root.iterdir())
    if {path.name for path in files} != _expected(version):
        raise GateError("core artifact inventory is not exact")
    result = {}
    for path in files:
        if path.is_symlink() or not path.is_file():
            raise GateError("core artifact must be a regular file")
        result[path.name] = {"sha256": _sha(path), "size": path.stat().st_size}
    return result


def _downloaded(root: Path, filename: str) -> dict[str, dict[str, object]]:
    if root.is_symlink() or not root.is_dir():
        raise GateError("core download directory is invalid")
    files = list(root.iterdir())
    if len(files) != 1 or files[0].name != filename:
        raise GateError("core download directory inventory is not exact")
    path = files[0]
    if path.is_symlink() or not path.is_file():
        raise GateError("downloaded core artifact must be a regular file")
    return {filename: {"sha256": _sha(path), "size": path.stat().st_size}}


def _index(*, allow_missing: bool) -> dict[str, object] | None:
    request = urllib.request.Request(
        INDEX, headers={"Accept": "application/json", "User-Agent": "sasori-core-gate/1"}
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            value = response.read(MAX_JSON + 1)
    except urllib.error.HTTPError as exc:
        if allow_missing and exc.code == 404:
            return None
        raise GateError(f"TestPyPI returned HTTP {exc.code}") from exc
    except (OSError, urllib.error.URLError) as exc:
        raise GateError("TestPyPI could not be queried") from exc
    if len(value) > MAX_JSON:
        raise GateError("TestPyPI response exceeds the bound")
    try:
        decoded = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GateError("TestPyPI returned invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise GateError("TestPyPI response is not an object")
    return decoded


def _release(value: dict[str, object] | None, version: str) -> list[dict[str, object]]:
    if value is None:
        return []
    releases = value.get("releases")
    if not isinstance(releases, dict):
        raise GateError("TestPyPI release index is invalid")
    items = releases.get(version, [])
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise GateError("TestPyPI version inventory is invalid")
    return items


def _version_present(value: dict[str, object] | None, version: str) -> bool:
    if value is None:
        return False
    releases = value.get("releases")
    if not isinstance(releases, dict):
        raise GateError("TestPyPI release index is invalid")
    return version in releases


def _indexed_inventory(items: list[dict[str, object]]) -> dict[str, str]:
    result = {}
    for item in items:
        filename = item.get("filename")
        digests = item.get("digests")
        if not isinstance(filename, str) or not isinstance(digests, dict):
            raise GateError("TestPyPI file record is invalid")
        sha256 = digests.get("sha256")
        if not isinstance(sha256, str) or re.fullmatch(r"[0-9a-f]{64}", sha256) is None:
            raise GateError("TestPyPI file digest is invalid")
        if filename in result:
            raise GateError("TestPyPI returned a duplicate filename")
        result[filename] = sha256
    return result


def _write(path: Path, value: dict[str, object]) -> None:
    if path.exists() or path.is_symlink():
        raise GateError("evidence output already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def preflight(args: argparse.Namespace) -> dict[str, object]:
    version = _version(args.project_root)
    if version != args.expected_version:
        raise GateError("requested core version does not match source")
    artifacts = _inventory(args.artifact_dir, version)
    if _version_present(_index(allow_missing=True), version):
        raise GateError("sasori-core version already exists on TestPyPI")
    return {
        "schema_version": 1,
        "kind": "sasori.core-testpypi-preflight",
        "project": PROJECT,
        "version": version,
        "index": INDEX,
        "version_absent": True,
        "artifacts": artifacts,
    }


def wait(args: argparse.Namespace) -> dict[str, object]:
    version = args.expected_version
    expected = _inventory(args.artifact_dir, version)
    actual = {}
    for attempt in range(1, args.attempts + 1):
        actual = _indexed_inventory(_release(_index(allow_missing=True), version))
        if set(actual) == set(expected) and all(
            actual[name] == item["sha256"] for name, item in expected.items()
        ):
            return {
                "schema_version": 1,
                "kind": "sasori.core-testpypi-index",
                "project": PROJECT,
                "version": version,
                "attempt": attempt,
                "artifacts": expected,
            }
        if attempt < args.attempts:
            time.sleep(args.delay_seconds)
    raise GateError("exact sasori-core hashes did not become visible on TestPyPI")


def finalize(args: argparse.Namespace) -> dict[str, object]:
    version = args.expected_version
    uploaded = _inventory(args.artifact_dir, version)
    wheel = _downloaded(
        args.wheel_dir, f"sasori_core-{version}-py3-none-any.whl"
    )
    sdist = _downloaded(args.sdist_dir, f"sasori_core-{version}.tar.gz")
    downloaded = {**wheel, **sdist}
    if uploaded != downloaded:
        raise GateError("downloaded sasori-core bytes do not match uploaded bytes")
    for marker in (args.wheel_smoke_marker, args.sdist_smoke_marker):
        if marker.is_symlink() or marker.read_text("utf-8") != "PASS\n":
            raise GateError("sasori-core smoke marker is invalid")
    return {
        "schema_version": 1,
        "kind": "sasori.core-testpypi-roundtrip",
        "project": PROJECT,
        "version": version,
        "artifacts": uploaded,
        "wheel_smoke": True,
        "sdist_smoke": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "wait", "finalize"):
        command = commands.add_parser(name)
        command.add_argument("--artifact-dir", type=Path, required=True)
        command.add_argument("--expected-version", required=True)
        command.add_argument("--output", type=Path, required=True)
        if name == "preflight":
            command.add_argument("--project-root", type=Path, required=True)
        elif name == "wait":
            command.add_argument("--attempts", type=int, default=18)
            command.add_argument("--delay-seconds", type=float, default=10)
        else:
            command.add_argument("--wheel-dir", type=Path, required=True)
            command.add_argument("--sdist-dir", type=Path, required=True)
            command.add_argument("--wheel-smoke-marker", type=Path, required=True)
            command.add_argument("--sdist-smoke-marker", type=Path, required=True)
    args = parser.parse_args()
    try:
        value = {"preflight": preflight, "wait": wait, "finalize": finalize}[
            args.command
        ](args)
        _write(args.output, value)
    except (GateError, OSError, KeyError, tomllib.TOMLDecodeError) as exc:
        print(f"sasori-core TestPyPI gate failed: {exc}")
        return 2
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
