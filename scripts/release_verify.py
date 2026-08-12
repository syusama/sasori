from __future__ import annotations

import argparse
import base64
import bz2
import configparser
import csv
import hashlib
import io
import json
import platform
import re
import stat
import subprocess
import sys
import tarfile
import tomllib
import zipfile
import zlib
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from pathlib import Path, PurePosixPath


VERIFIER_VERSION = "15"
SOURCE_TREE_ALGORITHM = "sasori-source-tree-v12"
MAX_WHEEL_BYTES = 250 * 1024
MAX_MEMBER_BYTES = 8 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 16 * 1024 * 1024
MAX_MEMBERS = 2_000
EXPECTED_SCRIPTS = {
    "sasori": "sasori.cli:main",
    "sasori-server": "sasori.server:main",
    "sasori-catalog": "sasori_market.catalog:main",
}
EXPECTED_PLUGINS = {
    "com.sasori.workspace": "sasori_plugins.workspace:register",
    "com.sasori.web-fetch": "sasori_plugins.web_fetch:register",
    "com.sasori.rag-sqlite": "sasori_plugins.rag_sqlite:register",
    "com.sasori.git": "sasori_plugins.git:register",
}
LICENSE_FILES = (
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "licenses/CPYTHON-3.12-LICENSE.txt",
)
RELEASE_DOCS = (
    "SECURITY.md",
    "docs/ADR-0001-PLUGIN-TRUST.md",
    "docs/ADR-0002-WEB-FETCH-BOUNDARY.md",
    "docs/ADR-0003-RAG-SQLITE-BOUNDARY.md",
    "docs/ADR-0004-GIT-PLUGIN-BOUNDARY.md",
    "docs/ADR-0005-MCP-STDIO-BOUNDARY.md",
    "docs/ADR-0006-MULTI-APP-RUN-BINDING.md",
    "docs/ADR-0007-TRUSTED-EXTERNAL-PLUGIN-HOST.md",
    "docs/ADR-0008-WORKBENCH-EVENT-REDUCER.md",
    "docs/ADR-0009-CONTEXT-PROJECTION-BOUNDARY.md",
    "docs/ADR-0010-ARTIFACT-REF-BOUNDARY.md",
    "docs/ADR-0011-SEMANTIC-COMPACTION-BOUNDARY.md",
    "docs/ADR-0012-DURABLE-BOUNDED-MEMORY.md",
    "docs/ADR-0013-TYPED-WORKFLOW-BOUNDARY.md",
    "docs/ADR-0014-STATIC-SERIAL-AUTHORING-PUBLIC-PROJECTION.md",
    "docs/ADR-0015-STATIC-WORKFLOW-MANIFEST-PREFLIGHT.md",
    "docs/ADR-0016-STATIC-SERIAL-WORKFLOW-STUDIO.md",
    "docs/ADR-0017-DURABLE-SAVED-WORKFLOW-CATALOG.md",
    "docs/ADR-0018-SASORI-CORE-PACKAGE-BOUNDARY.md",
    "docs/ADR-0019-CORE-LOOP-STREAM-SETTLEMENT.md",
    "docs/ARTIFACTS.md",
    "docs/BENCHMARK-LEAGENT-TOFU.md",
    "docs/BENCHMARK-PI-PROMA.md",
    "docs/CONTEXT.md",
    "docs/FOUNDATION.md",
    "docs/HTTP_API.md",
    "docs/MEMORY.md",
    "docs/PROVIDERS.md",
    "docs/RELEASE.md",
    "docs/WORKFLOWS.md",
)
RELEASE_ASSETS = (
    "README_zh.md",
    "README_ja.md",
    "README_ko.md",
    "docs/assets/sasori-logo.jpg",
    "docs/assets/screenshots-manifest.json",
    "docs/assets/screenshots/workbench-completed-1600x1000-e816cf7.jpg",
    "docs/assets/screenshots/workbench-mobile-390x844-e816cf7.jpg",
    "docs/assets/screenshots/workbench-mobile-inspector-390x844-e816cf7.jpg",
    "docs/assets/screenshots/workbench-workflow-studio-1600x1000-e816cf7.jpg",
)
TOP_LEVEL_PACKAGES = (
    "sasori",
    "sasori_apps",
    "sasori_artifacts",
    "sasori_context",
    "sasori_flow",
    "sasori_market",
    "sasori_memory",
    "sasori_plugins",
    "sasori_web",
)
PROJECT_URLS = {
    "Documentation": "https://github.com/syusama/sasori/blob/main/docs/FOUNDATION.md",
    "Homepage": "https://github.com/syusama/sasori",
    "Issues": "https://github.com/syusama/sasori/issues",
    "Repository": "https://github.com/syusama/sasori",
}
FORBIDDEN_PARTS = {
    ".git",
    ".github",
    ".venv",
    "venv",
    ".omc",
    ".secrets",
    ".ssh",
    ".aws",
    ".gnupg",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "build",
    "dist",
    "tests",
    "catalog",
}
FORBIDDEN_NAMES = {
    ".env",
    ".coverage",
    "coverage.xml",
    "sasori-actions.jsonl",
    "incident-actions.jsonl",
    "id_rsa",
    "id_ed25519",
    "known_hosts",
    "authorized_keys",
    ".netrc",
    ".npmrc",
    ".ds_store",
    "thumbs.db",
    "agents.md",
    "claude.md",
}
FORBIDDEN_SUFFIXES = (
    ".pyc",
    ".pyo",
    ".pyd",
    ".sqlite",
    ".sqlite3",
    ".sqlite3-lock",
    ".sqlite3.lock",
    ".db",
    ".log",
    ".pem",
    ".key",
    ".crt",
    ".cer",
    ".p12",
    ".pfx",
)
SDIST_EGG_INFO_FILES = {
    "PKG-INFO",
    "SOURCES.txt",
    "dependency_links.txt",
    "entry_points.txt",
    "requires.txt",
    "top_level.txt",
}
SPDX_PACKAGE_PURPOSES = {
    "APPLICATION",
    "ARCHIVE",
    "CONTAINER",
    "DEVICE",
    "FILE",
    "FIRMWARE",
    "FRAMEWORK",
    "INSTALL",
    "LIBRARY",
    "OPERATING-SYSTEM",
    "OTHER",
}


class ReleaseVerificationError(ValueError):
    def __init__(self, message: str, code: int = 2) -> None:
        super().__init__(message)
        self.code = code


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_member_name(name: str) -> tuple[str, ...]:
    if not name or "\\" in name or "\0" in name:
        raise ReleaseVerificationError(f"unsafe archive member: {name!r}")
    if any(ord(character) < 32 or ord(character) == 127 for character in name):
        raise ReleaseVerificationError(f"control character in archive member: {name!r}")
    trimmed = name[:-1] if name.endswith("/") else name
    parts = trimmed.split("/")
    if (
        not trimmed
        or PurePosixPath(trimmed).is_absolute()
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise ReleaseVerificationError(f"unsafe archive member: {name!r}")
    return tuple(parts)


def _reject_forbidden(parts: tuple[str, ...], kind: str) -> None:
    lowered = tuple(part.casefold() for part in parts)
    basename = lowered[-1]
    egg_indexes = [index for index, part in enumerate(lowered) if part.endswith(".egg-info")]
    allowed_egg = (
        kind == "sdist"
        and len(egg_indexes) == 1
        and egg_indexes[0] == 2
        and lowered[1:3] == ("src", "sasori.egg-info")
        and (
            len(parts) == 3
            or (len(parts) == 4 and parts[-1] in SDIST_EGG_INFO_FILES)
        )
    )
    if egg_indexes and not allowed_egg:
        raise ReleaseVerificationError(f"generated egg-info is not allowed in {kind}")
    if any(part in FORBIDDEN_PARTS for part in lowered):
        raise ReleaseVerificationError(f"forbidden path in {kind}: {'/'.join(parts)}")
    if (
        basename in FORBIDDEN_NAMES
        or basename.startswith(".env.")
        or basename.endswith(FORBIDDEN_SUFFIXES)
    ):
        raise ReleaseVerificationError(f"forbidden file in {kind}: {'/'.join(parts)}")


def _inventory(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256(b"sasori-artifact-member-inventory-v1\0")
    for name in sorted(files):
        value = files[name]
        digest.update(name.encode("utf-8") + b"\0")
        digest.update(str(len(value)).encode("ascii") + b"\0")
        digest.update(_sha256_bytes(value).encode("ascii") + b"\n")
    return digest.hexdigest()


def _metadata(
    value: bytes, where: str, expected_readme: bytes
) -> tuple[str, str, str]:
    message = BytesParser(policy=policy.default).parsebytes(value)
    for field in (
        "Name",
        "Version",
        "Requires-Python",
        "License-Expression",
        "Description-Content-Type",
    ):
        if len(message.get_all(field, [])) != 1:
            raise ReleaseVerificationError(
                f"{where} must declare exactly one {field}", 3
            )
    name = str(message.get("Name", ""))
    version = str(message.get("Version", ""))
    requires_python = str(message.get("Requires-Python", ""))
    if name != "sasori" or not version:
        raise ReleaseVerificationError(f"invalid {where} name/version", 3)
    if requires_python.replace(" ", "") not in (">=3.11,<3.14", "<3.14,>=3.11"):
        raise ReleaseVerificationError(f"invalid {where} Requires-Python", 3)
    expected_dependency = f"sasori-core=={version}"
    if message.get_all("Requires-Dist", []) != [expected_dependency]:
        raise ReleaseVerificationError(
            f"{where} must depend on exact {expected_dependency}", 3
        )
    if str(message.get("License-Expression", "")) != "MIT":
        raise ReleaseVerificationError(f"invalid {where} license", 3)
    if str(message.get("Description-Content-Type", "")) != "text/markdown":
        raise ReleaseVerificationError(
            f"invalid {where} Description-Content-Type", 3
        )
    if [str(item) for item in message.get_all("License-File", [])] != list(
        LICENSE_FILES
    ):
        raise ReleaseVerificationError(
            f"{where} license files do not match the release contract", 3
        )
    project_urls = [str(item) for item in message.get_all("Project-URL", [])]
    if len(project_urls) != len(PROJECT_URLS) or set(project_urls) != {
        f"{name}, {url}" for name, url in PROJECT_URLS.items()
    }:
        raise ReleaseVerificationError(
            f"{where} project URLs do not match the release contract", 3
        )
    if b"\r\n\r\n" in value:
        description = value.split(b"\r\n\r\n", 1)[1]
    elif b"\n\n" in value:
        description = value.split(b"\n\n", 1)[1]
    else:
        raise ReleaseVerificationError(
            f"{where} is missing its description body", 3
        )
    try:
        normalized_description = description.decode("utf-8").replace("\r\n", "\n")
        normalized_readme = expected_readme.decode("utf-8").replace("\r\n", "\n")
    except UnicodeDecodeError as exc:
        raise ReleaseVerificationError(
            f"{where} description must be strict UTF-8", 3
        ) from exc
    if "\r" in normalized_description or "\r" in normalized_readme:
        raise ReleaseVerificationError(
            f"{where} description has unsupported line endings", 3
        )
    if normalized_description != normalized_readme:
        raise ReleaseVerificationError(
            f"{where} description does not match README.md", 3
        )
    return name, version, requires_python


def _source_payload(source_root: Path) -> dict[str, bytes]:
    source = source_root / "src"
    if source.is_symlink():
        raise ReleaseVerificationError("source package directory must not be a symlink", 3)
    for path in source.rglob("*"):
        if path.is_symlink():
            raise ReleaseVerificationError(
                f"source package contains a symlink: {path.relative_to(source).as_posix()}",
                3,
            )
    files = {
        path.relative_to(source).as_posix(): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
        and ".egg-info" not in path.parts
        and path.relative_to(source).parts[0] != "sasori_core"
        and path.suffix.casefold() in {".py", ".html", ".css", ".js", ".jpg"}
    }
    required = {f"{package}/__init__.py" for package in TOP_LEVEL_PACKAGES}
    if not required.issubset(files):
        raise ReleaseVerificationError("source package inventory is incomplete", 3)
    return files


def _parse_entry_points(value: bytes) -> dict[str, dict[str, str]]:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    try:
        parser.read_string(value.decode("utf-8"))
    except (UnicodeDecodeError, configparser.Error) as exc:
        raise ReleaseVerificationError("invalid wheel entry_points.txt", 3) from exc
    return {section: dict(parser.items(section)) for section in parser.sections()}


def _verify_record(files: dict[str, bytes], dist_info: str) -> None:
    record_name = f"{dist_info}/RECORD"
    try:
        rows = list(csv.reader(io.StringIO(files[record_name].decode("utf-8"))))
    except (KeyError, UnicodeDecodeError, csv.Error) as exc:
        raise ReleaseVerificationError("wheel RECORD is missing or invalid", 3) from exc
    records: dict[str, tuple[str, str]] = {}
    for row in rows:
        if len(row) != 3 or row[0] in records:
            raise ReleaseVerificationError("wheel RECORD has malformed or duplicate rows", 3)
        _safe_member_name(row[0])
        records[row[0]] = (row[1], row[2])
    if set(records) != set(files):
        raise ReleaseVerificationError("wheel RECORD inventory does not match archive", 3)
    for name, value in files.items():
        encoded_hash, encoded_size = records[name]
        if name == record_name:
            if encoded_hash or encoded_size:
                raise ReleaseVerificationError("wheel RECORD must not hash itself", 3)
            continue
        expected_hash = base64.urlsafe_b64encode(hashlib.sha256(value).digest()).rstrip(b"=").decode()
        if encoded_hash != f"sha256={expected_hash}" or encoded_size != str(len(value)):
            raise ReleaseVerificationError(f"wheel RECORD mismatch: {name}", 3)


def _canonical_wheel_compression(value: bytes) -> tuple[int, bytes]:
    compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
    deflated = compressor.compress(value) + compressor.flush()
    bzip2 = bz2.compress(value, compresslevel=9)
    if len(bzip2) < len(deflated):
        return zipfile.ZIP_BZIP2, bzip2
    return zipfile.ZIP_DEFLATED, deflated


def _compressed_wheel_member(stream, info: zipfile.ZipInfo) -> bytes:
    stream.seek(info.header_offset)
    header = stream.read(30)
    if len(header) != 30 or header[:4] != b"PK\x03\x04":
        raise ReleaseVerificationError("wheel local member header is invalid", 3)
    flags = int.from_bytes(header[6:8], "little")
    method = int.from_bytes(header[8:10], "little")
    compressed_size = int.from_bytes(header[18:22], "little")
    file_size = int.from_bytes(header[22:26], "little")
    name_size = int.from_bytes(header[26:28], "little")
    extra_size = int.from_bytes(header[28:30], "little")
    if (
        flags != info.flag_bits
        or method != info.compress_type
        or compressed_size != info.compress_size
        or file_size != info.file_size
    ):
        raise ReleaseVerificationError("wheel local member header is inconsistent", 3)
    stream.seek(name_size + extra_size, 1)
    compressed = stream.read(info.compress_size)
    if len(compressed) != info.compress_size:
        raise ReleaseVerificationError("wheel compressed member is truncated", 3)
    return compressed


def verify_wheel(path: Path, source_root: Path, project: dict[str, str]) -> dict[str, object]:
    if path.stat().st_size >= MAX_WHEEL_BYTES:
        raise ReleaseVerificationError(f"wheel is not below {MAX_WHEEL_BYTES} bytes")
    expected_name = f"sasori-{project['version']}-py3-none-any.whl"
    if path.name != expected_name:
        raise ReleaseVerificationError(f"wheel filename must be {expected_name}", 3)
    files: dict[str, bytes] = {}
    seen: set[str] = set()
    compression_counts = {"deflate": 0, "bzip2": 0}
    try:
        with path.open("rb") as raw, zipfile.ZipFile(path) as archive:
            if archive.comment:
                raise ReleaseVerificationError("wheel archive comment is not allowed", 3)
            infos = archive.infolist()
            if len(infos) > MAX_MEMBERS:
                raise ReleaseVerificationError("wheel has too many members")
            total = 0
            dist_info_started = False
            for info in infos:
                parts = _safe_member_name(info.filename)
                _reject_forbidden(parts, "wheel")
                folded = info.filename.rstrip("/").casefold()
                if folded in seen:
                    raise ReleaseVerificationError("wheel has duplicate/colliding members")
                seen.add(folded)
                mode = (info.external_attr >> 16) & 0o170000
                if info.is_dir() or (mode and mode != stat.S_IFREG):
                    raise ReleaseVerificationError("wheel contains a non-regular member")
                if (
                    info.compress_type
                    not in (zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2)
                    or info.flag_bits & 0x9
                    or info.file_size > MAX_MEMBER_BYTES
                ):
                    raise ReleaseVerificationError(
                        "wheel contains unsupported compression, flags, or size"
                    )
                is_dist_info = ".dist-info/" in info.filename
                if dist_info_started and not is_dist_info:
                    raise ReleaseVerificationError(
                        "wheel dist-info members must be physically last", 3
                    )
                dist_info_started = dist_info_started or is_dist_info
                total += info.file_size
                if total > MAX_UNCOMPRESSED_BYTES:
                    raise ReleaseVerificationError("wheel expands beyond the verification limit")
                value = archive.read(info)
                expected_method, expected_compressed = _canonical_wheel_compression(
                    value
                )
                if (
                    info.compress_type != expected_method
                    or _compressed_wheel_member(raw, info) != expected_compressed
                ):
                    raise ReleaseVerificationError(
                        "wheel member is not canonically repacked", 3
                    )
                compression_counts[
                    "bzip2"
                    if info.compress_type == zipfile.ZIP_BZIP2
                    else "deflate"
                ] += 1
                files[info.filename] = value
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise ReleaseVerificationError("wheel is unreadable", 1) from exc

    dist_infos = {name.split("/", 1)[0] for name in files if ".dist-info/" in name}
    if not all(compression_counts.values()):
        raise ReleaseVerificationError(
            "wheel must contain canonical Deflate and BZIP2 members", 3
        )
    expected_dist_info = f"sasori-{project['version']}.dist-info"
    if dist_infos != {expected_dist_info}:
        raise ReleaseVerificationError("wheel must have one exact dist-info directory", 3)
    name, version, requires_python = _metadata(
        files.get(f"{expected_dist_info}/METADATA", b""),
        "wheel METADATA",
        (source_root / "README.md").read_bytes(),
    )
    expected_dist_info_files = {
        f"{expected_dist_info}/METADATA",
        f"{expected_dist_info}/WHEEL",
        f"{expected_dist_info}/entry_points.txt",
        f"{expected_dist_info}/top_level.txt",
        f"{expected_dist_info}/RECORD",
        *(f"{expected_dist_info}/licenses/{relative}" for relative in LICENSE_FILES),
    }
    actual_dist_info_files = {
        item for item in files if item.startswith(f"{expected_dist_info}/")
    }
    if actual_dist_info_files != expected_dist_info_files:
        raise ReleaseVerificationError(
            "wheel dist-info inventory does not match the release contract", 3
        )
    wheel_headers = BytesParser(policy=policy.default).parsebytes(
        files.get(f"{expected_dist_info}/WHEEL", b"")
    )
    if set(wheel_headers.keys()) != {
        "Wheel-Version",
        "Generator",
        "Root-Is-Purelib",
        "Tag",
    } or any(
        len(wheel_headers.get_all(field, [])) != 1
        for field in ("Wheel-Version", "Generator", "Root-Is-Purelib", "Tag")
    ):
        raise ReleaseVerificationError("wheel headers do not match the release contract", 3)
    if (
        str(wheel_headers.get("Wheel-Version", "")) != "1.0"
        or
        str(wheel_headers.get("Root-Is-Purelib", "")).casefold() != "true"
        or wheel_headers.get_all("Tag", []) != ["py3-none-any"]
    ):
        raise ReleaseVerificationError("wheel is not exactly py3-none-any", 3)
    top_level = files[f"{expected_dist_info}/top_level.txt"]
    if top_level.decode("utf-8").splitlines() != list(TOP_LEVEL_PACKAGES):
        raise ReleaseVerificationError("wheel top_level.txt is invalid", 3)
    entry_points = _parse_entry_points(files.get(f"{expected_dist_info}/entry_points.txt", b""))
    if entry_points != {"console_scripts": EXPECTED_SCRIPTS, "sasori.plugins": EXPECTED_PLUGINS}:
        raise ReleaseVerificationError("wheel entry points do not match the public contract", 3)
    for relative in LICENSE_FILES:
        license_name = f"{expected_dist_info}/licenses/{relative}"
        if files.get(license_name) != (source_root / relative).read_bytes():
            raise ReleaseVerificationError(f"wheel license file does not match source: {relative}", 3)
    payload = {key: value for key, value in files.items() if not key.startswith(f"{expected_dist_info}/")}
    source_payload = _source_payload(source_root)
    if payload != source_payload:
        raise ReleaseVerificationError("wheel payload does not match the current source tree", 3)
    index = payload.get("sasori_web/index.html", b"").decode("utf-8")
    assets = set(re.findall(r'(?:src|href)=["\']/assets/([^"\']+)', index))
    if not assets or not {".css", ".js", ".jpg"}.issubset({Path(name).suffix for name in assets}):
        raise ReleaseVerificationError("Workbench asset references are incomplete", 3)
    if any(f"sasori_web/{name}" not in payload for name in assets):
        raise ReleaseVerificationError("Workbench references a missing wheel asset", 3)
    _verify_record(files, expected_dist_info)
    return {
        "kind": "wheel",
        "filename": path.name,
        "size": path.stat().st_size,
        "sha256": _sha256_file(path),
        "member_inventory_sha256": _inventory(files),
        "regular_file_count": len(files),
        "archive_member_count": len(infos),
        "compression": {
            "algorithm": "per-member-min-deflate9-bzip2-9-v1",
            "methods": compression_counts,
        },
        "name": name,
        "version": version,
        "requires_python": requires_python,
    }


def verify_sdist(path: Path, source_root: Path, project: dict[str, str]) -> dict[str, object]:
    expected_name = f"sasori-{project['version']}.tar.gz"
    if path.name != expected_name:
        raise ReleaseVerificationError(f"sdist filename must be {expected_name}", 3)
    root = f"sasori-{project['version']}"
    files: dict[str, bytes] = {}
    directories: set[str] = set()
    seen: set[str] = set()
    try:
        with tarfile.open(path, "r:gz") as archive:
            members = archive.getmembers()
            if len(members) > MAX_MEMBERS:
                raise ReleaseVerificationError("sdist has too many members")
            total = 0
            for member in members:
                parts = _safe_member_name(member.name)
                if parts[0] != root:
                    raise ReleaseVerificationError("sdist has more than one top-level directory")
                _reject_forbidden(parts, "sdist")
                folded = member.name.rstrip("/").casefold()
                if folded in seen:
                    raise ReleaseVerificationError("sdist has duplicate/colliding members")
                seen.add(folded)
                if not (member.isfile() or member.isdir()):
                    raise ReleaseVerificationError("sdist contains a non-regular member")
                if member.isfile():
                    if member.size > MAX_MEMBER_BYTES:
                        raise ReleaseVerificationError("sdist contains an oversized member")
                    total += member.size
                    if total > MAX_UNCOMPRESSED_BYTES:
                        raise ReleaseVerificationError("sdist expands beyond the verification limit")
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise ReleaseVerificationError("sdist member cannot be read")
                    files[member.name] = stream.read()
                else:
                    directories.add(member.name.rstrip("/"))
    except (OSError, tarfile.TarError) as exc:
        raise ReleaseVerificationError("sdist is unreadable", 1) from exc

    for filename in (
        "pyproject.toml",
        "MANIFEST.in",
        "README.md",
        *LICENSE_FILES,
        *RELEASE_DOCS,
        *RELEASE_ASSETS,
    ):
        if files.get(f"{root}/{filename}") != (source_root / filename).read_bytes():
            raise ReleaseVerificationError(f"sdist {filename} does not match source", 3)
    name, version, requires_python = _metadata(
        files.get(f"{root}/PKG-INFO", b""),
        "sdist PKG-INFO",
        (source_root / "README.md").read_bytes(),
    )
    source_payload = _source_payload(source_root)
    for relative, value in source_payload.items():
        if files.get(f"{root}/src/{relative}") != value:
            raise ReleaseVerificationError(f"sdist source mismatch: {relative}", 3)
    required = {
        f"{root}/pyproject.toml",
        f"{root}/MANIFEST.in",
        f"{root}/README.md",
        f"{root}/PKG-INFO",
        *(f"{root}/{name}" for name in LICENSE_FILES),
        *(f"{root}/{name}" for name in RELEASE_DOCS),
        *(f"{root}/{name}" for name in RELEASE_ASSETS),
        *(f"{root}/src/{name}" for name in source_payload),
    }
    optional = {
        f"{root}/setup.cfg",
        *(f"{root}/src/sasori.egg-info/{name}" for name in SDIST_EGG_INFO_FILES),
    }
    allowed_files = required | optional
    allowed_directories = {root}
    for name in allowed_files:
        parts = PurePosixPath(name).parts
        allowed_directories.update(
            PurePosixPath(*parts[:index]).as_posix()
            for index in range(1, len(parts))
        )
    if (
        not required.issubset(files)
        or not set(files).issubset(allowed_files)
        or not directories.issubset(allowed_directories)
    ):
        raise ReleaseVerificationError("sdist contains missing or unexpected release files", 3)
    return {
        "kind": "sdist",
        "filename": path.name,
        "size": path.stat().st_size,
        "sha256": _sha256_file(path),
        "member_inventory_sha256": _inventory(files),
        "regular_file_count": len(files),
        "archive_member_count": len(members),
        "name": name,
        "version": version,
        "requires_python": requires_python,
    }


def _project(source_root: Path) -> dict[str, str]:
    try:
        value = tomllib.loads((source_root / "pyproject.toml").read_text(encoding="utf-8"))
        project = value["project"]
    except (OSError, KeyError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseVerificationError("pyproject.toml is unreadable or incomplete", 1) from exc
    result = {
        "name": str(project.get("name", "")),
        "version": str(project.get("version", "")),
        "requires_python": str(project.get("requires-python", "")),
        "license_expression": str(project.get("license", "")),
    }
    if result != {
        "name": "sasori",
        "version": result["version"],
        "requires_python": ">=3.11,<3.14",
        "license_expression": "MIT",
    } or not result["version"] or project.get("dependencies") != [
        f"sasori-core=={result['version']}"
    ] or project.get("license-files") != [
        "LICENSE", "THIRD_PARTY_NOTICES.md", "licenses/*"
    ] or project.get("readme") != "README.md":
        raise ReleaseVerificationError("project metadata violates the release contract", 3)
    if project.get("urls") != PROJECT_URLS:
        raise ReleaseVerificationError("project URLs violate the release contract", 3)
    return result


def _build_inputs(source_root: Path) -> dict[str, object]:
    lock_path = source_root / "requirements-build.txt"
    docker_path = source_root / "Dockerfile"
    dockerignore_path = source_root / ".dockerignore"
    lock = lock_path.read_text(encoding="utf-8").replace("\\\r\n", " ").replace("\\\n", " ")
    match = re.fullmatch(
        r"\s*setuptools==([0-9][A-Za-z0-9.]*)\s+--hash=sha256:([0-9a-f]{64})\s*", lock
    )
    docker = docker_path.read_text(encoding="utf-8")
    dockerignore = dockerignore_path.read_text(encoding="utf-8")
    dockerignore_patterns = {
        line.strip().rstrip("/")
        for line in dockerignore.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    if ".secrets" not in dockerignore_patterns or any(
        pattern.startswith("!.secrets") for pattern in dockerignore_patterns
    ):
        raise ReleaseVerificationError(
            "Docker context must exclude .secrets without negation", 3
        )
    if "/downloads" not in dockerignore_patterns or any(
        pattern.startswith("!/downloads") for pattern in dockerignore_patterns
    ):
        raise ReleaseVerificationError(
            "Docker context must exclude root downloads without negation", 3
        )
    recursive_cache_exclusions = {
        "**/__pycache__",
        "**/__pycache__/**",
        "**/*.py[cod]",
        "**/*.egg-info",
        "**/*.egg-info/**",
    }
    if not recursive_cache_exclusions.issubset(dockerignore_patterns):
        raise ReleaseVerificationError(
            "Docker context must recursively exclude Python build caches", 3
        )
    if not all(
        marker in docker
        for marker in (
            "COPY pyproject.toml MANIFEST.in ",
            'find src packages -type d \\( -name "*.egg-info" -o -name "__pycache__" \\)',
            'find src packages -type f \\( -name "*.pyc" -o -name "*.pyo" \\) -delete',
            "--wheel-dir /wheels packages/sasori-core",
        )
    ):
        raise ReleaseVerificationError(
            "Docker builder must prune generated Python build state", 3
        )
    base = re.search(r"^ARG PYTHON_BASE=(\S+@sha256:[0-9a-f]{64})$", docker, re.MULTILINE)
    index = re.search(r"^ARG PYTHON_INDEX_URL=(https://\S+)$", docker, re.MULTILINE)
    if not match or not base or not index:
        raise ReleaseVerificationError("build lock or Docker release configuration is invalid", 3)
    if not base.group(1).startswith("docker.m.daocloud.io/"):
        raise ReleaseVerificationError("Docker base is not on the configured mainland registry", 3)
    if index.group(1) != "https://pypi.tuna.tsinghua.edu.cn/simple":
        raise ReleaseVerificationError("default Python index is not the locked mainland mirror", 3)
    if "apt-get" in docker:
        mirror_position = docker.find("mirrors.aliyun.com")
        if mirror_position < 0 or mirror_position > docker.find("apt-get"):
            raise ReleaseVerificationError("APT source must switch before apt-get", 3)
    return {
        "pyproject_toml_sha256": _sha256_file(source_root / "pyproject.toml"),
        "requirements_build_sha256": _sha256_file(lock_path),
        "build_requirements": [
            {"name": "setuptools", "version": match.group(1), "sha256": match.group(2)}
        ],
        "python_index_url": index.group(1),
        "container_base_image": base.group(1),
    }


def _git(source_root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes] | None:
    try:
        return subprocess.run(
            ["git", "-C", str(source_root), *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _source_state(source_root: Path, expected_tag: str) -> dict[str, object]:
    head_result = _git(source_root, "rev-parse", "HEAD")
    status_result = _git(source_root, "status", "--porcelain=v1", "--untracked-files=all")
    if not head_result or head_result.returncode or not status_result or status_result.returncode:
        return {
            "state": "unversioned_local_candidate",
            "git_clean": False,
            "head_baseline": None,
            "head_is_artifact_source_identity": False,
            "exact_tags": [],
            "expected_release_tag": expected_tag,
            "matching_release_tag": None,
            "tracked_changed_count": None,
            "untracked_count": None,
            "git_status_porcelain_sha256": None,
        }
    head = head_result.stdout.decode("ascii", "strict").strip()
    status = status_result.stdout
    lines = [line for line in status.splitlines() if line]
    untracked = sum(line.startswith(b"?? ") for line in lines)
    tracked = len(lines) - untracked
    tag_result = _git(source_root, "tag", "--points-at", "HEAD")
    tags = sorted(tag_result.stdout.decode("utf-8").split()) if tag_result and not tag_result.returncode else []
    clean = not status
    matching_tag = expected_tag if expected_tag in tags else None
    if clean and matching_tag:
        state = "clean_release_tag_candidate"
    elif clean and tags:
        state = "clean_wrong_tag_local_candidate"
    elif clean:
        state = "clean_untagged_local_candidate"
    else:
        state = "dirty_or_untracked_local_candidate"
    return {
        "state": state,
        "git_clean": clean,
        "head_baseline": head,
        "head_is_artifact_source_identity": clean,
        "exact_tags": tags,
        "expected_release_tag": expected_tag,
        "matching_release_tag": matching_tag,
        "tracked_changed_count": tracked,
        "untracked_count": untracked,
        "git_status_porcelain_sha256": _sha256_bytes(status),
    }


def _validate_trigger_tag(
    project: dict[str, str], trigger_tag: str | None
) -> str | None:
    if trigger_tag is None:
        return None
    expected_tag = f"v{project['version']}"
    if trigger_tag != expected_tag:
        raise ReleaseVerificationError(
            f"workflow trigger tag {trigger_tag!r} does not match {expected_tag!r}",
            4,
        )
    return trigger_tag


def _source_tree(source_root: Path) -> tuple[str, int]:
    paths = [
        source_root / name
        for name in (
            "pyproject.toml",
            "MANIFEST.in",
            "README.md",
            *LICENSE_FILES,
            *RELEASE_DOCS,
            *RELEASE_ASSETS,
            "requirements-build.txt",
            "Dockerfile",
        )
    ]
    paths.extend(source_root / "src" / name for name in _source_payload(source_root))
    for path in paths:
        if path.is_symlink():
            raise ReleaseVerificationError(
                f"release source input must not be a symlink: {path.relative_to(source_root).as_posix()}",
                3,
            )
    digest = hashlib.sha256((SOURCE_TREE_ALGORITHM + "\0").encode("ascii"))
    for path in sorted(paths, key=lambda item: item.relative_to(source_root).as_posix()):
        relative = path.relative_to(source_root).as_posix()
        digest.update(relative.encode("utf-8") + b"\0" + _sha256_file(path).encode("ascii") + b"\n")
    return digest.hexdigest(), len(paths)


def _spdx(
    project: dict[str, str], artifacts: list[dict[str, object]], build: dict[str, object], created: str
) -> dict[str, object]:
    wheel = next(item for item in artifacts if item["kind"] == "wheel")
    sdist = next(item for item in artifacts if item["kind"] == "sdist")
    requirement = build["build_requirements"][0]
    identity = _sha256_bytes(
        (str(wheel["sha256"]) + str(sdist["sha256"])).encode("ascii")
    )
    sasori_package = {
        "name": "sasori",
        "SPDXID": "SPDXRef-Package-sasori",
        "versionInfo": project["version"],
        "downloadLocation": "NOASSERTION",
        "filesAnalyzed": False,
        "licenseConcluded": "MIT",
        "licenseDeclared": "MIT",
        "copyrightText": "Copyright (c) 2026 sasaky",
        "primaryPackagePurpose": "FRAMEWORK",
    }
    archive_packages = [
        {
            "name": item["filename"],
            "SPDXID": f"SPDXRef-Package-sasori-{item['kind']}",
            "versionInfo": project["version"],
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False,
            "licenseConcluded": "MIT",
            "licenseDeclared": "MIT",
            "copyrightText": "Copyright (c) 2026 sasaky",
            "primaryPackagePurpose": "ARCHIVE",
            "checksums": [
                {"algorithm": "SHA256", "checksumValue": item["sha256"]}
            ],
        }
        for item in (wheel, sdist)
    ]
    build_package = {
        "name": requirement["name"],
        "SPDXID": "SPDXRef-Package-setuptools-build",
        "versionInfo": requirement["version"],
        "downloadLocation": f"{build['python_index_url']}/setuptools/",
        "filesAnalyzed": False,
        "licenseConcluded": "MIT",
        "licenseDeclared": "MIT",
        "copyrightText": "NOASSERTION",
        "primaryPackagePurpose": "OTHER",
        "checksums": [{"algorithm": "SHA256", "checksumValue": requirement["sha256"]}],
    }
    return {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"sasori-{project['version']}",
        "documentNamespace": f"https://spdx.org/spdxdocs/sasori-{project['version']}-{identity}",
        "creationInfo": {"created": created, "creators": [f"Tool: sasori-release-verify-{VERIFIER_VERSION}"]},
        "packages": [sasori_package, *archive_packages, build_package],
        "relationships": [
            {
                "spdxElementId": "SPDXRef-DOCUMENT",
                "relationshipType": "DESCRIBES",
                "relatedSpdxElement": "SPDXRef-Package-sasori",
            },
            *(
                {
                    "spdxElementId": item["SPDXID"],
                    "relationshipType": "GENERATED_FROM",
                    "relatedSpdxElement": "SPDXRef-Package-sasori",
                }
                for item in archive_packages
            ),
            {
                "spdxElementId": "SPDXRef-Package-setuptools-build",
                "relationshipType": "BUILD_DEPENDENCY_OF",
                "relatedSpdxElement": "SPDXRef-Package-sasori",
            },
        ],
    }


def _validate_spdx(
    spdx: dict[str, object], artifacts: list[dict[str, object]]
) -> None:
    packages = spdx.get("packages")
    if not isinstance(packages, list):
        raise ReleaseVerificationError("SPDX packages are missing", 3)
    by_id: dict[str, dict[str, object]] = {}
    for package in packages:
        if not isinstance(package, dict) or not isinstance(package.get("SPDXID"), str):
            raise ReleaseVerificationError("SPDX package is structurally invalid", 3)
        package_id = package["SPDXID"]
        if package_id in by_id or package.get("primaryPackagePurpose") not in SPDX_PACKAGE_PURPOSES:
            raise ReleaseVerificationError("SPDX package identity or purpose is invalid", 3)
        checksums = package.get("checksums", [])
        if not isinstance(checksums, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("algorithm"), str)
            for item in checksums
        ):
            raise ReleaseVerificationError("SPDX package checksums are invalid", 3)
        algorithms = [item["algorithm"] for item in checksums]
        if len(algorithms) != len(set(algorithms)):
            raise ReleaseVerificationError(
                "SPDX package repeats a checksum algorithm", 3
            )
        by_id[package_id] = package
    for artifact in artifacts:
        package = by_id.get(f"SPDXRef-Package-sasori-{artifact['kind']}")
        if package is None or package.get("name") != artifact["filename"] or package.get(
            "checksums"
        ) != [{"algorithm": "SHA256", "checksumValue": artifact["sha256"]}]:
            raise ReleaseVerificationError(
                "SPDX artifact subjects do not match the manifest", 3
            )


def verify_release(
    wheel_path: Path,
    sdist_path: Path,
    source_root: Path,
    output_dir: Path,
    *,
    trigger_tag: str | None = None,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    source_root = source_root.resolve(strict=True)
    for artifact in (wheel_path, sdist_path):
        if artifact.is_symlink() or not artifact.is_file():
            raise ReleaseVerificationError(f"artifact is not a regular file: {artifact.name}", 1)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", artifact.name):
            raise ReleaseVerificationError(f"unsafe artifact filename: {artifact.name}", 1)
    project = _project(source_root)
    trigger_tag = _validate_trigger_tag(project, trigger_tag)
    build = _build_inputs(source_root)
    wheel = verify_wheel(wheel_path, source_root, project)
    sdist = verify_sdist(sdist_path, source_root, project)
    if any(item["version"] != project["version"] for item in (wheel, sdist)):
        raise ReleaseVerificationError("artifact versions disagree with pyproject.toml", 3)
    artifacts = [
        {key: value for key, value in item.items() if key not in {"name", "version", "requires_python"}}
        for item in (wheel, sdist)
    ]
    expected_tag = f"v{project['version']}"
    source = _source_state(source_root, expected_tag)
    source_hash, source_count = _source_tree(source_root)
    source.update({
        "artifact_source_binding": "verified_to_current_working_tree",
        "included_source_tree_algorithm": SOURCE_TREE_ALGORITHM,
        "included_source_tree_sha256": source_hash,
        "included_source_file_count": source_count,
        "workflow_trigger_tag": trigger_tag,
        "workflow_trigger_tag_matches_expected": (
            trigger_tag == expected_tag if trigger_tag is not None else None
        ),
    })
    release_eligible = bool(
        source["git_clean"] and source["matching_release_tag"] == expected_tag
    )
    created = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    manifest = {
        "schema_version": 1,
        "kind": "sasori.artifact-manifest",
        "project": {
            **project,
            "runtime_dependencies": [f"sasori-core=={project['version']}"],
        },
        "artifacts": artifacts,
        "build_inputs": build,
        "verification": {
            "release_eligible": release_eligible,
            "verified_at_utc": created,
            "verifier": {"name": "sasori-release-verify", "version": VERIFIER_VERSION},
        },
    }
    provenance = {
        "schema_version": 1,
        "kind": "sasori.local-provenance",
        "release_eligible": release_eligible,
        "reason": None if release_eligible else source["state"],
        "signed": False,
        "claim": "local verification record; not a trusted-build attestation",
        "source": source,
        "builder": {
            "python_implementation": platform.python_implementation(),
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "declared_release_configuration": build,
        },
        "artifacts": [
            {"filename": item["filename"], "sha256": item["sha256"]} for item in artifacts
        ],
        "created_at_utc": created,
    }
    spdx = _spdx(project, artifacts, build, created)
    _validate_spdx(spdx, artifacts)
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, value in (
        ("artifact-manifest.json", manifest),
        (f"sasori-{project['version']}.spdx.json", spdx),
        ("provenance.local.json", provenance),
    ):
        destination = output_dir / name
        temporary = destination.with_name(destination.name + ".tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(destination)
    return manifest, spdx, provenance


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify Sasori wheel/sdist and write local release records.")
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--sdist", required=True, type=Path)
    parser.add_argument("--source-root", default=Path.cwd(), type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--trigger-tag",
        help="bind a tagged workflow invocation to the exact v{project.version} tag",
    )
    parser.add_argument(
        "--allow-dirty-local",
        action="store_true",
        help="write explicitly non-release local records; exits 5 after successful verification",
    )
    options = parser.parse_args(arguments)
    try:
        manifest, _, provenance = verify_release(
            options.wheel,
            options.sdist,
            options.source_root,
            options.output,
            trigger_tag=options.trigger_tag,
        )
        if not provenance["release_eligible"]:
            if not options.allow_dirty_local:
                raise ReleaseVerificationError(
                    f"source is not a clean exact-tag release: {provenance['reason']}", 4
                )
            print(json.dumps({
                "artifacts": len(manifest["artifacts"]),
                "release_eligible": False,
                "source_state": provenance["source"]["state"],
            }, separators=(",", ":")))
            return 5
        print(json.dumps({"artifacts": len(manifest["artifacts"]), "release_eligible": True}))
        return 0
    except ReleaseVerificationError as exc:
        print(f"release verification failed: {exc}", file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    raise SystemExit(main())
