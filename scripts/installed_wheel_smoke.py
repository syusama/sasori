from __future__ import annotations

import importlib
import importlib.metadata
import importlib.resources
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path


PACKAGES = ("sasori", "sasori_apps", "sasori_market", "sasori_plugins", "sasori_web")
SCRIPTS = ("sasori", "sasori-server", "sasori-catalog")
WEB_RESOURCES = (
    "index.html",
    "app.0.1.0.css",
    "app.0.1.1.js",
    "event-reducer.0.1.0.js",
    "app.0.1.2.js",
    "mark.0.1.0.svg",
)


def _require_under_prefix(path: str | os.PathLike[str], prefix: Path, label: str) -> Path:
    try:
        resolved = Path(path).resolve(strict=True)
        resolved_prefix = prefix.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"installed {label} path is missing or unreadable") from exc
    if not resolved.is_relative_to(resolved_prefix):
        raise RuntimeError(f"installed {label} resolved outside the consumer environment")
    return resolved


def main() -> int:
    with (Path(__file__).resolve().parents[1] / "pyproject.toml").open("rb") as stream:
        expected_version = tomllib.load(stream)["project"]["version"]
    distribution = importlib.metadata.distribution("sasori")
    if distribution.metadata["Name"] != "sasori" or distribution.version != expected_version:
        raise RuntimeError("installed Sasori identity is invalid")
    if distribution.requires:
        raise RuntimeError("installed Sasori unexpectedly declares runtime dependencies")

    prefix = Path(sys.prefix)
    _require_under_prefix(distribution.locate_file(""), prefix, "distribution")

    for package in PACKAGES:
        module = importlib.import_module(package)
        if module.__file__ is None:
            raise RuntimeError(f"installed package has no origin: {package}")
        _require_under_prefix(module.__file__, prefix, f"package {package}")
    resources = importlib.resources.files("sasori_web")
    if any(not (resources / name).is_file() or not (resources / name).read_bytes() for name in WEB_RESOURCES):
        raise RuntimeError("installed Workbench resources are missing or empty")

    executable_dir = os.fspath(os.path.dirname(sys.executable))
    for name in SCRIPTS:
        command = shutil.which(name, path=executable_dir)
        if command is None:
            raise RuntimeError(f"installed entry point is missing: {name}")
        completed = subprocess.run(
            [command, "--help"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
        if completed.returncode:
            raise RuntimeError(f"installed entry point failed: {name}")

    print("installed wheel smoke: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
