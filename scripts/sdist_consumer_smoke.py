from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


COMMAND_TIMEOUT_SECONDS = 180


def _regular_file(path: Path, label: str) -> Path:
    if path.is_symlink():
        raise RuntimeError(f"{label} must not be a symlink")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"{label} is missing or unreadable") from exc
    if not resolved.is_file():
        raise RuntimeError(f"{label} is not a regular file")
    return resolved


def _venv_python(root: Path, platform: str = os.name) -> Path:
    if platform == "nt":
        return root / "Scripts" / "python.exe"
    return root / "bin" / "python"


def _run(
    command: list[str | os.PathLike[str]],
    *,
    cwd: Path,
    environment: dict[str, str],
    accepted: frozenset[int] = frozenset({0}),
) -> int:
    completed = subprocess.run(
        [os.fspath(item) for item in command],
        cwd=cwd,
        env=environment,
        stdin=subprocess.DEVNULL,
        timeout=COMMAND_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode not in accepted:
        raise subprocess.CalledProcessError(completed.returncode, completed.args)
    return completed.returncode


def _single_wheel(root: Path) -> Path:
    wheels = sorted(root.glob("*.whl"))
    if len(wheels) != 1 or wheels[0].is_symlink() or not wheels[0].is_file():
        raise RuntimeError("sdist rebuild must produce exactly one regular wheel")
    return wheels[0].resolve(strict=True)


def _single_build_wheel(root_path: Path) -> tuple[Path, Path]:
    if root_path.is_symlink():
        raise RuntimeError("build wheelhouse must not be a symlink")
    try:
        root = root_path.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("build wheelhouse is missing or unreadable") from exc
    if not root.is_dir():
        raise RuntimeError("build wheelhouse is not a directory")
    try:
        entries = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise RuntimeError("build wheelhouse is unreadable") from exc
    if len(entries) != 1 or not entries[0].name.endswith("-py3-none-any.whl"):
        raise RuntimeError("build wheelhouse must contain exactly one regular wheel")
    return root, _regular_file(entries[0], "build wheel")


def run_smoke(
    sdist_path: Path,
    core_wheel_path: Path,
    build_lock_path: Path,
    build_wheelhouse_path: Path,
    consumer_check_path: Path,
    wheel_repacker_path: Path,
    release_verifier_path: Path,
    source_root_path: Path,
) -> dict[str, str | int]:
    sdist = _regular_file(sdist_path, "source archive")
    core_wheel = _regular_file(core_wheel_path, "sasori-core wheel")
    build_lock = _regular_file(build_lock_path, "build lock")
    build_wheelhouse, build_wheel = _single_build_wheel(build_wheelhouse_path)
    consumer_check = _regular_file(consumer_check_path, "consumer check")
    wheel_repacker = _regular_file(wheel_repacker_path, "wheel repacker")
    release_verifier = _regular_file(release_verifier_path, "release verifier")
    if source_root_path.is_symlink():
        raise RuntimeError("source root must not be a symlink")
    try:
        source_root = source_root_path.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("source root is missing or unreadable") from exc
    if not source_root.is_dir():
        raise RuntimeError("source root is not a directory")
    if not re.fullmatch(r"sasori-[A-Za-z0-9][A-Za-z0-9._+-]*\.tar\.gz", sdist.name):
        raise RuntimeError("source archive filename is invalid")
    version = sdist.name[len("sasori-") : -len(".tar.gz")]
    if core_wheel.name != f"sasori_core-{version}-py3-none-any.whl":
        raise RuntimeError("sasori-core wheel version does not match the bundle sdist")

    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    for name in (
        "PIP_INDEX_URL",
        "PIP_EXTRA_INDEX_URL",
        "PIP_FIND_LINKS",
        "PIP_TRUSTED_HOST",
    ):
        environment.pop(name, None)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PIP_NO_INDEX"] = "1"
    with tempfile.TemporaryDirectory(prefix="sasori-sdist-smoke-") as directory:
        root = Path(directory)
        build_root = root / "build"
        consumer_root = root / "consumer"
        wheel_root = root / "wheel"
        wheel_root.mkdir()

        _run([sys.executable, "-m", "venv", build_root], cwd=root, environment=environment)
        build_python = _venv_python(build_root)
        _run(
            [
                build_python,
                "-m",
                "pip",
                "--isolated",
                "--no-cache-dir",
                "install",
                "--no-index",
                "--find-links",
                build_wheelhouse,
                "--only-binary=:all:",
                "--require-hashes",
                "-r",
                build_lock,
            ],
            cwd=root,
            environment=environment,
        )
        _run(
            [
                build_python,
                "-m",
                "pip",
                "--isolated",
                "--no-cache-dir",
                "wheel",
                "--no-index",
                "--no-build-isolation",
                "--no-deps",
                "--wheel-dir",
                wheel_root,
                sdist,
            ],
            cwd=root,
            environment=environment,
        )
        wheel = _single_wheel(wheel_root)
        _run(
            [sys.executable, wheel_repacker, "--wheel", wheel],
            cwd=root,
            environment=environment,
        )
        verifier_code = _run(
            [
                sys.executable,
                release_verifier,
                "--wheel",
                wheel,
                "--sdist",
                sdist,
                "--source-root",
                source_root,
                "--output",
                root / "release-metadata",
                "--allow-dirty-local",
            ],
            cwd=root,
            environment=environment,
            accepted=frozenset({0, 5}),
        )

        _run([sys.executable, "-m", "venv", consumer_root], cwd=root, environment=environment)
        consumer_python = _venv_python(consumer_root)
        _run(
            [
                consumer_python,
                "-m",
                "pip",
                "--isolated",
                "--no-cache-dir",
                "install",
                "--no-index",
                "--no-deps",
                core_wheel,
                wheel,
            ],
            cwd=root,
            environment=environment,
        )
        _run([consumer_python, consumer_check], cwd=root, environment=environment)

    return {
        "source_archive": sdist.name,
        "core_wheel": core_wheel.name,
        "build_wheel": build_wheel.name,
        "rebuilt_wheel": wheel.name,
        "release_verifier_exit": verifier_code,
    }


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild a verified Sasori sdist and smoke it in a clean consumer environment."
    )
    parser.add_argument("--sdist", required=True, type=Path)
    parser.add_argument("--core-wheel", required=True, type=Path)
    parser.add_argument("--build-lock", required=True, type=Path)
    parser.add_argument("--build-wheelhouse", required=True, type=Path)
    parser.add_argument("--consumer-check", required=True, type=Path)
    parser.add_argument("--wheel-repacker", required=True, type=Path)
    parser.add_argument("--release-verifier", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    options = parser.parse_args(arguments)
    evidence = run_smoke(
        options.sdist,
        options.core_wheel,
        options.build_lock,
        options.build_wheelhouse,
        options.consumer_check,
        options.wheel_repacker,
        options.release_verifier,
        options.source_root,
    )
    print(json.dumps(evidence, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
