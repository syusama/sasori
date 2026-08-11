from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


PROJECT_NAME = "sasori"
TESTPYPI_JSON_URL = "https://test.pypi.org/pypi/sasori/json"
GITHUB_API_ROOT = "https://api.github.com"
UNTAGGED_SOURCE_STATE = "clean_untagged_local_candidate"
MAX_JSON_BYTES = 4 * 1024 * 1024
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


class TestPyPIGateError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_file(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise TestPyPIGateError(f"{label} must be one regular file")
    return path


def _read_json(path: Path, label: str) -> dict[str, object]:
    _regular_file(path, label)
    if path.stat().st_size > MAX_JSON_BYTES:
        raise TestPyPIGateError(f"{label} exceeds the bounded JSON size")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TestPyPIGateError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise TestPyPIGateError(f"{label} must be a JSON object")
    return value


def _write_json(path: Path, value: dict[str, object]) -> None:
    if path.exists() or path.is_symlink():
        raise TestPyPIGateError("evidence output already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
    except OSError as exc:
        raise TestPyPIGateError("evidence output could not be created") from exc


def project_version(source_root: Path) -> str:
    path = _regular_file(source_root / "pyproject.toml", "pyproject.toml")
    try:
        with path.open("rb") as stream:
            value = tomllib.load(stream)["project"]
    except (OSError, KeyError, tomllib.TOMLDecodeError) as exc:
        raise TestPyPIGateError("project metadata is invalid") from exc
    if not isinstance(value, dict) or value.get("name") != PROJECT_NAME:
        raise TestPyPIGateError("project name is not sasori")
    version = value.get("version")
    if not isinstance(version, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._+-]*", version
    ):
        raise TestPyPIGateError("project version is invalid")
    return version


def expected_filenames(version: str) -> dict[str, str]:
    return {
        "bdist_wheel": f"sasori-{version}-py3-none-any.whl",
        "sdist": f"sasori-{version}.tar.gz",
    }


def artifact_inventory(directory: Path, version: str) -> dict[str, dict[str, object]]:
    if directory.is_symlink() or not directory.is_dir():
        raise TestPyPIGateError("distribution directory must be one regular directory")
    children = list(directory.iterdir())
    expected = expected_filenames(version)
    expected_names = set(expected.values())
    if {path.name for path in children} != expected_names:
        raise TestPyPIGateError("distribution directory inventory is not exact")
    inventory: dict[str, dict[str, object]] = {}
    for kind, filename in expected.items():
        path = _regular_file(directory / filename, filename)
        inventory[kind] = {
            "filename": filename,
            "sha256": _sha256(path),
            "size": path.stat().st_size,
        }
    return inventory


def _git(source_root: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(source_root), *arguments],
            check=False,
            capture_output=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TestPyPIGateError("git identity check could not run") from exc
    if result.returncode != 0:
        raise TestPyPIGateError("git identity check failed")
    try:
        return result.stdout.decode("utf-8", "strict").strip()
    except UnicodeDecodeError as exc:
        raise TestPyPIGateError("git identity output is not UTF-8") from exc


def source_identity(source_root: Path, expected_commit: str) -> dict[str, object]:
    if COMMIT_RE.fullmatch(expected_commit) is None:
        raise TestPyPIGateError(
            "expected commit must be 40 lowercase hexadecimal digits"
        )
    head = _git(source_root, "rev-parse", "HEAD")
    if head != expected_commit:
        raise TestPyPIGateError(
            "checked-out commit does not match the requested commit"
        )
    if _git(source_root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise TestPyPIGateError("source checkout is not clean")
    tags = tuple(
        filter(None, _git(source_root, "tag", "--points-at", "HEAD").splitlines())
    )
    if tags:
        raise TestPyPIGateError(
            "TestPyPI prerelease must run before any tag exists at HEAD"
        )
    return {"commit_sha": head, "git_clean": True, "tags_at_head": []}


def _metadata_inventory(
    metadata_root: Path,
    version: str,
    expected_commit: str,
    expected_artifacts: dict[str, dict[str, object]],
) -> dict[str, object]:
    manifest = _read_json(metadata_root / "artifact-manifest.json", "artifact manifest")
    provenance = _read_json(metadata_root / "provenance.local.json", "local provenance")
    project = manifest.get("project")
    artifacts = manifest.get("artifacts")
    if not isinstance(project, dict) or project.get("name") != PROJECT_NAME:
        raise TestPyPIGateError("artifact manifest project is invalid")
    if project.get("version") != version or not isinstance(artifacts, list):
        raise TestPyPIGateError("artifact manifest version or inventory is invalid")
    by_kind = {item.get("kind"): item for item in artifacts if isinstance(item, dict)}
    if len(artifacts) != len(expected_artifacts) or set(by_kind) != set(
        expected_artifacts
    ):
        raise TestPyPIGateError("artifact manifest kinds are not exact")
    for kind, expected in expected_artifacts.items():
        item = by_kind[kind]
        if any(
            item.get(field) != expected[field]
            for field in ("filename", "sha256", "size")
        ):
            raise TestPyPIGateError("artifact manifest does not bind the upload bytes")
    source = provenance.get("source")
    if (
        provenance.get("release_eligible") is not False
        or provenance.get("reason") != UNTAGGED_SOURCE_STATE
        or not isinstance(source, dict)
        or source.get("state") != UNTAGGED_SOURCE_STATE
        or source.get("head_baseline") != expected_commit
        or source.get("head_is_artifact_source_identity") is not True
        or source.get("git_clean") is not True
        or source.get("exact_tags") != []
    ):
        raise TestPyPIGateError(
            "local provenance is not the exact clean untagged candidate"
        )
    return {
        "manifest_sha256": _sha256(metadata_root / "artifact-manifest.json"),
        "provenance_sha256": _sha256(metadata_root / "provenance.local.json"),
        "release_eligible": False,
        "source_state": UNTAGGED_SOURCE_STATE,
    }


def _read_json_url(
    url: str,
    *,
    label: str,
    token: str | None = None,
    allow_not_found: bool = False,
) -> dict[str, object] | None:
    headers = {
        "Accept": "application/vnd.github+json" if token else "application/json",
        "User-Agent": "sasori-testpypi-gate/1",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = response.read(MAX_JSON_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if allow_not_found and exc.code == 404:
            return None
        raise TestPyPIGateError(f"{label} request returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TestPyPIGateError(f"{label} request failed") from exc
    if len(payload) > MAX_JSON_BYTES:
        raise TestPyPIGateError(f"{label} response exceeds the bounded JSON size")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TestPyPIGateError(f"{label} response is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise TestPyPIGateError(f"{label} response must be a JSON object")
    return value


def successful_ci_run(
    payload: dict[str, object], expected_commit: str
) -> dict[str, object]:
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list):
        raise TestPyPIGateError("GitHub CI response has no workflow_runs list")
    matching = [
        run
        for run in runs
        if isinstance(run, dict)
        and run.get("head_sha") == expected_commit
        and run.get("head_branch") == "main"
        and run.get("event") == "push"
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
        and run.get("path") == ".github/workflows/ci.yml"
    ]
    if len(matching) != 1:
        raise TestPyPIGateError(
            "exact commit does not have one successful main push CI run"
        )
    run = matching[0]
    run_id = run.get("id")
    run_number = run.get("run_number")
    html_url = run.get("html_url")
    if (
        isinstance(run_id, bool)
        or not isinstance(run_id, int)
        or run_id < 1
        or isinstance(run_number, bool)
        or not isinstance(run_number, int)
        or run_number < 1
        or not isinstance(html_url, str)
        or not html_url.startswith("https://github.com/")
    ):
        raise TestPyPIGateError("successful CI run identity is invalid")
    return {"run_id": run_id, "run_number": run_number, "url": html_url}


def require_ci_success(
    repository: str, expected_commit: str, token: str
) -> dict[str, object]:
    if REPOSITORY_RE.fullmatch(repository) is None or not token:
        raise TestPyPIGateError("GitHub repository or ephemeral token is unavailable")
    workflow = urllib.parse.quote("ci.yml", safe="")
    query = urllib.parse.urlencode(
        {
            "branch": "main",
            "event": "push",
            "head_sha": expected_commit,
            "status": "success",
            "per_page": "20",
        }
    )
    url = f"{GITHUB_API_ROOT}/repos/{repository}/actions/workflows/{workflow}/runs?{query}"
    payload = _read_json_url(url, label="GitHub CI", token=token)
    if payload is None:
        raise TestPyPIGateError("GitHub CI response is unavailable")
    return successful_ci_run(payload, expected_commit)


def require_version_absent(payload: dict[str, object] | None, version: str) -> None:
    if payload is None:
        return
    info = payload.get("info")
    releases = payload.get("releases")
    if (
        not isinstance(info, dict)
        or str(info.get("name", "")).casefold() != PROJECT_NAME
    ):
        raise TestPyPIGateError("TestPyPI project identity is invalid")
    if not isinstance(releases, dict):
        raise TestPyPIGateError("TestPyPI releases inventory is invalid")
    if version in releases:
        raise TestPyPIGateError("exact TestPyPI version already exists")


def testpypi_release(
    payload: dict[str, object],
    version: str,
    expected_artifacts: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    info = payload.get("info")
    releases = payload.get("releases")
    if (
        not isinstance(info, dict)
        or str(info.get("name", "")).casefold() != PROJECT_NAME
    ):
        raise TestPyPIGateError("TestPyPI project identity is invalid")
    if not isinstance(releases, dict) or not isinstance(releases.get(version), list):
        raise TestPyPIGateError("TestPyPI exact version is not indexed")
    files = releases[version]
    by_kind = {
        item.get("packagetype"): item for item in files if isinstance(item, dict)
    }
    if set(by_kind) != set(expected_artifacts) or len(files) != len(expected_artifacts):
        raise TestPyPIGateError("TestPyPI release file inventory is not exact")
    result: list[dict[str, object]] = []
    for kind in sorted(expected_artifacts):
        expected = expected_artifacts[kind]
        item = by_kind[kind]
        digests = item.get("digests")
        if (
            item.get("filename") != expected["filename"]
            or item.get("size") != expected["size"]
            or item.get("yanked") is not False
            or not isinstance(digests, dict)
            or digests.get("sha256") != expected["sha256"]
        ):
            raise TestPyPIGateError("TestPyPI indexed bytes do not match the candidate")
        result.append(
            {
                "filename": expected["filename"],
                "kind": kind,
                "sha256": expected["sha256"],
                "size": expected["size"],
                "yanked": False,
            }
        )
    return result


def wait_for_testpypi(
    version: str,
    artifacts: dict[str, dict[str, object]],
    *,
    attempts: int,
    delay_seconds: float,
    fetch: Callable[[], dict[str, object] | None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict[str, object]]:
    if attempts < 1 or attempts > 60 or delay_seconds < 0 or delay_seconds > 60:
        raise TestPyPIGateError("TestPyPI polling bounds are invalid")
    last_error = "TestPyPI exact version is not indexed"
    fetcher = fetch or (
        lambda: _read_json_url(
            TESTPYPI_JSON_URL, label="TestPyPI", allow_not_found=True
        )
    )
    for attempt in range(attempts):
        try:
            payload = fetcher()
            if payload is not None:
                return testpypi_release(payload, version, artifacts)
        except TestPyPIGateError as exc:
            last_error = str(exc)
        if attempt + 1 < attempts:
            sleep(delay_seconds)
    raise TestPyPIGateError(f"TestPyPI indexing did not converge: {last_error}")


def _download_inventory(
    wheel_directory: Path,
    sdist_directory: Path,
    version: str,
) -> dict[str, dict[str, object]]:
    expected = expected_filenames(version)
    result: dict[str, dict[str, object]] = {}
    for kind, directory in (
        ("bdist_wheel", wheel_directory),
        ("sdist", sdist_directory),
    ):
        if directory.is_symlink() or not directory.is_dir():
            raise TestPyPIGateError("pip download directory is invalid")
        children = list(directory.iterdir())
        if len(children) != 1 or children[0].name != expected[kind]:
            raise TestPyPIGateError("pip download inventory is not exact")
        path = _regular_file(children[0], "pip-downloaded distribution")
        result[kind] = {
            "filename": path.name,
            "sha256": _sha256(path),
            "size": path.stat().st_size,
        }
    return result


def _require_pass_marker(path: Path, label: str) -> None:
    _regular_file(path, label)
    try:
        value = path.read_bytes()
    except OSError as exc:
        raise TestPyPIGateError(f"{label} cannot be read") from exc
    if value != b"PASS\n":
        raise TestPyPIGateError(f"{label} is invalid")


def _release_payload_from_index_evidence(
    evidence: dict[str, object],
    version: str,
) -> dict[str, object]:
    files = evidence.get("files")
    if (
        evidence.get("schema_version") != 1
        or evidence.get("kind") != "sasori.testpypi-index"
        or evidence.get("project") != PROJECT_NAME
        or evidence.get("version") != version
        or evidence.get("index") != TESTPYPI_JSON_URL
        or not isinstance(files, list)
    ):
        raise TestPyPIGateError("TestPyPI index evidence is invalid")
    release_files: list[dict[str, object]] = []
    for item in files:
        if not isinstance(item, dict):
            raise TestPyPIGateError("TestPyPI index evidence is invalid")
        release_files.append(
            {
                "packagetype": item.get("kind"),
                "filename": item.get("filename"),
                "size": item.get("size"),
                "yanked": item.get("yanked"),
                "digests": {"sha256": item.get("sha256")},
            }
        )
    return {
        "info": {"name": PROJECT_NAME},
        "releases": {version: release_files},
    }


def command_preflight(options: argparse.Namespace) -> None:
    source_root = options.source_root.resolve(strict=True)
    version = project_version(source_root)
    if version != options.expected_version:
        raise TestPyPIGateError("requested version does not match pyproject.toml")
    identity = source_identity(source_root, options.expected_commit)
    artifacts = artifact_inventory(options.upload_dir.resolve(strict=True), version)
    metadata = _metadata_inventory(
        options.metadata_root.resolve(strict=True),
        version,
        options.expected_commit,
        artifacts,
    )
    token = os.environ.get(options.github_token_env, "")
    ci = require_ci_success(options.github_repository, options.expected_commit, token)
    existing = _read_json_url(TESTPYPI_JSON_URL, label="TestPyPI", allow_not_found=True)
    require_version_absent(existing, version)
    _write_json(
        options.output,
        {
            "schema_version": 1,
            "kind": "sasori.testpypi-preflight",
            "project": PROJECT_NAME,
            "version": version,
            "index": TESTPYPI_JSON_URL,
            "source": identity,
            "ci": ci,
            "artifacts": [
                artifacts[kind] | {"kind": kind} for kind in sorted(artifacts)
            ],
            "release_verification": metadata,
            "version_absent_before_publish": True,
        },
    )


def command_wait(options: argparse.Namespace) -> None:
    artifacts = artifact_inventory(
        options.upload_dir.resolve(strict=True), options.expected_version
    )
    files = wait_for_testpypi(
        options.expected_version,
        artifacts,
        attempts=options.attempts,
        delay_seconds=options.delay_seconds,
    )
    _write_json(
        options.output,
        {
            "schema_version": 1,
            "kind": "sasori.testpypi-index",
            "project": PROJECT_NAME,
            "version": options.expected_version,
            "index": TESTPYPI_JSON_URL,
            "files": files,
        },
    )


def command_finalize(options: argparse.Namespace) -> None:
    source_root = options.source_root.resolve(strict=True)
    version = project_version(source_root)
    if version != options.expected_version:
        raise TestPyPIGateError("requested version does not match pyproject.toml")
    identity = source_identity(source_root, options.expected_commit)
    uploaded = artifact_inventory(options.upload_dir.resolve(strict=True), version)
    downloaded = _download_inventory(
        options.wheel_download_dir.resolve(strict=True),
        options.sdist_download_dir.resolve(strict=True),
        version,
    )
    if downloaded != uploaded:
        raise TestPyPIGateError(
            "pip-downloaded bytes do not match the uploaded candidate"
        )
    index_evidence = _read_json(
        options.index_evidence.resolve(strict=True), "index evidence"
    )
    testpypi_release(
        _release_payload_from_index_evidence(index_evidence, version),
        version,
        uploaded,
    )
    _require_pass_marker(options.wheel_smoke_marker, "installed-wheel smoke marker")
    _require_pass_marker(options.sdist_smoke_marker, "rebuilt-sdist smoke marker")
    _write_json(
        options.output,
        {
            "schema_version": 1,
            "kind": "sasori.testpypi-roundtrip",
            "project": PROJECT_NAME,
            "version": version,
            "verified_at_utc": datetime.now(timezone.utc).isoformat(),
            "source": identity,
            "index": TESTPYPI_JSON_URL,
            "artifacts": [uploaded[kind] | {"kind": kind} for kind in sorted(uploaded)],
            "checks": {
                "index_hashes_match": True,
                "pip_download_hashes_match": True,
                "installed_wheel_smoke": True,
                "rebuilt_sdist_smoke": True,
            },
        },
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Verify one exact Sasori TestPyPI round trip"
    )
    subparsers = result.add_subparsers(dest="command", required=True)

    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("--source-root", type=Path, required=True)
    preflight.add_argument("--upload-dir", type=Path, required=True)
    preflight.add_argument("--metadata-root", type=Path, required=True)
    preflight.add_argument("--expected-commit", required=True)
    preflight.add_argument("--expected-version", required=True)
    preflight.add_argument("--github-repository", required=True)
    preflight.add_argument("--github-token-env", default="GITHUB_TOKEN")
    preflight.add_argument("--output", type=Path, required=True)
    preflight.set_defaults(handler=command_preflight)

    wait = subparsers.add_parser("wait")
    wait.add_argument("--upload-dir", type=Path, required=True)
    wait.add_argument("--expected-version", required=True)
    wait.add_argument("--attempts", type=int, default=18)
    wait.add_argument("--delay-seconds", type=float, default=10)
    wait.add_argument("--output", type=Path, required=True)
    wait.set_defaults(handler=command_wait)

    finalize = subparsers.add_parser("finalize")
    finalize.add_argument("--source-root", type=Path, required=True)
    finalize.add_argument("--upload-dir", type=Path, required=True)
    finalize.add_argument("--wheel-download-dir", type=Path, required=True)
    finalize.add_argument("--sdist-download-dir", type=Path, required=True)
    finalize.add_argument("--expected-commit", required=True)
    finalize.add_argument("--expected-version", required=True)
    finalize.add_argument("--index-evidence", type=Path, required=True)
    finalize.add_argument("--wheel-smoke-marker", type=Path, required=True)
    finalize.add_argument("--sdist-smoke-marker", type=Path, required=True)
    finalize.add_argument("--output", type=Path, required=True)
    finalize.set_defaults(handler=command_finalize)
    return result


def main(arguments: list[str] | None = None) -> int:
    options = parser().parse_args(arguments)
    try:
        options.handler(options)
    except TestPyPIGateError as exc:
        print(f"TestPyPI gate failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
