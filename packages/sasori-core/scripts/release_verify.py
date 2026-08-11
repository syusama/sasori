from __future__ import annotations

import argparse
import base64
import bz2
import csv
import hashlib
import io
import json
import re
import stat
import tarfile
import tomllib
import zipfile
import zlib
from email import policy
from email.parser import BytesParser
from pathlib import Path, PurePosixPath


VERIFIER_VERSION = "3"
SOURCE_PAYLOAD_ALGORITHM = "sasori-core-source-payload-v1"
MAX_WHEEL_BYTES = 128 * 1024
MAX_MEMBER_BYTES = 128 * 1024
MAX_UNCOMPRESSED_BYTES = 512 * 1024
MAX_MEMBERS = 64
CORE_MODULES = {
    "__init__.py",
    "contracts.py",
    "projection.py",
    "runtime.py",
    "store.py",
    "testing.py",
}
PROJECT_URLS = {
    "Documentation": "https://github.com/syusama/sasori/blob/main/docs/ADR-0018-SASORI-CORE-PACKAGE-BOUNDARY.md",
    "Homepage": "https://github.com/syusama/sasori",
    "Issues": "https://github.com/syusama/sasori/issues",
    "Repository": "https://github.com/syusama/sasori",
}
SDIST_EGG_INFO_FILES = {
    "PKG-INFO",
    "SOURCES.txt",
    "dependency_links.txt",
    "top_level.txt",
}


class CoreReleaseError(ValueError):
    pass


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _inventory(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name in sorted(files):
        encoded = name.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(len(files[name]).to_bytes(8, "big"))
        digest.update(hashlib.sha256(files[name]).digest())
    return digest.hexdigest()


def _safe(name: str) -> tuple[str, ...]:
    if (
        not name
        or "\\" in name
        or "\0" in name
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
    ):
        raise CoreReleaseError(f"unsafe archive member: {name!r}")
    path = PurePosixPath(name.rstrip("/"))
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise CoreReleaseError(f"unsafe archive member: {name!r}")
    lowered = {part.casefold() for part in path.parts}
    if lowered.intersection({".git", "tests", "build", "dist", "__pycache__"}):
        raise CoreReleaseError(f"forbidden archive member: {name}")
    if path.suffix.casefold() in {".pyc", ".pyo", ".pyd", ".so", ".dll"}:
        raise CoreReleaseError(f"binary or cache member is forbidden: {name}")
    return path.parts


def _normalized_utf8(value: bytes, label: str) -> str:
    try:
        normalized = value.decode("utf-8").replace("\r\n", "\n")
    except UnicodeDecodeError as exc:
        raise CoreReleaseError(f"{label} must be strict UTF-8") from exc
    if "\r" in normalized:
        raise CoreReleaseError(f"{label} has unsupported line endings")
    return normalized


def _metadata(value: bytes, version: str, expected_readme: bytes) -> str:
    try:
        message = BytesParser(policy=policy.default).parsebytes(value)
    except (TypeError, ValueError) as exc:
        raise CoreReleaseError("core package metadata is invalid") from exc
    expected = {
        "Name": "sasori-core",
        "Version": version,
        "License-Expression": "MIT",
        "Description-Content-Type": "text/markdown",
    }
    for key, item in expected.items():
        if message.get_all(key, []) != [item]:
            raise CoreReleaseError(f"invalid core metadata field: {key}")
    requires_python = str(message.get("Requires-Python", "")).replace(" ", "")
    if requires_python not in {">=3.11,<3.14", "<3.14,>=3.11"}:
        raise CoreReleaseError("invalid core metadata field: Requires-Python")
    if message.get_all("Requires-Dist", []):
        raise CoreReleaseError("sasori-core must have zero runtime dependencies")
    if [str(item) for item in message.get_all("License-File", [])] != ["LICENSE"]:
        raise CoreReleaseError("core package license inventory is invalid")
    project_urls = [str(item) for item in message.get_all("Project-URL", [])]
    if len(project_urls) != len(PROJECT_URLS) or set(project_urls) != {
        f"{name}, {url}" for name, url in PROJECT_URLS.items()
    }:
        raise CoreReleaseError("core package project URLs are invalid")
    normalized = _normalized_utf8(value, "core package metadata")
    if "\n\n" not in normalized:
        raise CoreReleaseError("core package metadata is missing its description body")
    description = normalized.split("\n\n", 1)[1]
    readme = _normalized_utf8(expected_readme, "core README.md")
    if description != readme:
        raise CoreReleaseError("core package description does not match README.md")
    return _sha256(normalized.encode("utf-8"))


def _project(root: Path) -> str:
    try:
        project = tomllib.loads((root / "pyproject.toml").read_text("utf-8"))[
            "project"
        ]
    except (OSError, KeyError, tomllib.TOMLDecodeError) as exc:
        raise CoreReleaseError("core pyproject is unreadable") from exc
    version = project.get("version")
    if (
        project.get("name") != "sasori-core"
        or not isinstance(version, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", version)
        or project.get("requires-python") != ">=3.11,<3.14"
        or project.get("license") != "MIT"
        or project.get("readme") != "README.md"
        or project.get("dependencies") != []
        or project.get("urls") != PROJECT_URLS
    ):
        raise CoreReleaseError("core project metadata violates the contract")
    return version


def _source_payload(root: Path) -> dict[str, bytes]:
    source = root / "src" / "sasori_core"
    if source.is_symlink() or not source.is_dir():
        raise CoreReleaseError("canonical core source directory is invalid")
    files: dict[str, bytes] = {}
    for path in source.rglob("*"):
        if path.is_symlink():
            raise CoreReleaseError("canonical core source must not contain symlinks")
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(source).as_posix()
        if "/" in relative or path.suffix.casefold() != ".py":
            raise CoreReleaseError(f"unexpected canonical core source: {relative}")
        files[f"sasori_core/{relative}"] = path.read_bytes()
    expected = {f"sasori_core/{name}" for name in CORE_MODULES}
    if set(files) != expected:
        raise CoreReleaseError("canonical core source inventory is invalid")
    return files


def _verify_record(files: dict[str, bytes], dist_info: str) -> None:
    record_name = f"{dist_info}/RECORD"
    try:
        rows = list(csv.reader(io.StringIO(files[record_name].decode("utf-8"))))
    except (KeyError, UnicodeDecodeError, csv.Error) as exc:
        raise CoreReleaseError("core wheel RECORD is missing or invalid") from exc
    records: dict[str, tuple[str, str]] = {}
    for row in rows:
        if len(row) != 3 or row[0] in records:
            raise CoreReleaseError("core wheel RECORD has malformed or duplicate rows")
        _safe(row[0])
        records[row[0]] = (row[1], row[2])
    if set(records) != set(files):
        raise CoreReleaseError("core wheel RECORD inventory is invalid")
    for name, value in files.items():
        digest, size = records[name]
        if name == record_name:
            if digest or size:
                raise CoreReleaseError("core wheel RECORD self-row is invalid")
            continue
        encoded = base64.urlsafe_b64encode(hashlib.sha256(value).digest()).rstrip(b"=").decode("ascii")
        if digest != f"sha256={encoded}" or size != str(len(value)):
            raise CoreReleaseError(f"core wheel RECORD mismatch: {name}")


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
        raise CoreReleaseError("core wheel local member header is invalid")
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
        raise CoreReleaseError("core wheel local member header is inconsistent")
    stream.seek(name_size + extra_size, 1)
    compressed = stream.read(info.compress_size)
    if len(compressed) != info.compress_size:
        raise CoreReleaseError("core wheel compressed member is truncated")
    return compressed


def verify_wheel(path: Path, version: str, project_root: Path) -> dict[str, object]:
    expected_name = f"sasori_core-{version}-py3-none-any.whl"
    if path.is_symlink() or not path.is_file():
        raise CoreReleaseError("core wheel must be one regular file")
    if path.name != expected_name or path.stat().st_size >= MAX_WHEEL_BYTES:
        raise CoreReleaseError("core wheel name or size violates the contract")
    files: dict[str, bytes] = {}
    seen: set[str] = set()
    compression_counts = {"deflate": 0, "bzip2": 0}
    with path.open("rb") as raw, zipfile.ZipFile(path) as archive:
        if archive.comment:
            raise CoreReleaseError("core wheel archive comment is not allowed")
        infos = archive.infolist()
        if not infos or len(infos) > MAX_MEMBERS:
            raise CoreReleaseError("core wheel member count is invalid")
        total = 0
        dist_info_started = False
        for info in infos:
            _safe(info.filename)
            folded = info.filename.rstrip("/").casefold()
            mode = (info.external_attr >> 16) & 0o170000
            if folded in seen:
                raise CoreReleaseError("core wheel has duplicate/colliding members")
            if (
                info.is_dir()
                or (mode and mode != stat.S_IFREG)
                or info.flag_bits & 0x9
                or info.comment
                or info.extra
                or info.file_size > MAX_MEMBER_BYTES
                or info.compress_type not in (zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2)
            ):
                raise CoreReleaseError("core wheel contains an invalid member")
            seen.add(folded)
            is_dist_info = ".dist-info/" in info.filename
            if dist_info_started and not is_dist_info:
                raise CoreReleaseError("core wheel dist-info members must be physically last")
            dist_info_started = dist_info_started or is_dist_info
            total += info.file_size
            if total > MAX_UNCOMPRESSED_BYTES:
                raise CoreReleaseError("core wheel expands beyond the size budget")
            value = archive.read(info)
            expected_method, expected_compressed = _canonical_wheel_compression(value)
            if (
                info.compress_type != expected_method
                or _compressed_wheel_member(raw, info) != expected_compressed
            ):
                raise CoreReleaseError("core wheel member is not canonically repacked")
            compression_counts[
                "bzip2" if info.compress_type == zipfile.ZIP_BZIP2 else "deflate"
            ] += 1
            files[info.filename] = value
    if not all(compression_counts.values()):
        raise CoreReleaseError("core wheel must use canonical Deflate and BZIP2 members")

    dist = f"sasori_core-{version}.dist-info"
    expected_modules = {f"sasori_core/{name}" for name in CORE_MODULES}
    expected_dist_info = {
        f"{dist}/METADATA",
        f"{dist}/WHEEL",
        f"{dist}/top_level.txt",
        f"{dist}/RECORD",
        f"{dist}/licenses/LICENSE",
    }
    if set(files) != expected_modules | expected_dist_info:
        raise CoreReleaseError("core wheel file inventory is invalid")
    metadata_sha256 = _metadata(
        files[f"{dist}/METADATA"], version, (project_root / "README.md").read_bytes()
    )
    wheel_headers = BytesParser(policy=policy.default).parsebytes(files[f"{dist}/WHEEL"])
    if (
        set(wheel_headers.keys()) != {"Wheel-Version", "Generator", "Root-Is-Purelib", "Tag"}
        or any(len(wheel_headers.get_all(field, [])) != 1 for field in wheel_headers.keys())
        or str(wheel_headers.get("Wheel-Version", "")) != "1.0"
        or str(wheel_headers.get("Root-Is-Purelib", "")).casefold() != "true"
        or wheel_headers.get_all("Tag", []) != ["py3-none-any"]
    ):
        raise CoreReleaseError("core wheel headers do not match the contract")
    if files[f"{dist}/top_level.txt"].decode("utf-8").splitlines() != ["sasori_core"]:
        raise CoreReleaseError("core wheel top_level.txt is invalid")
    if files[f"{dist}/licenses/LICENSE"] != (project_root / "LICENSE").read_bytes():
        raise CoreReleaseError("core wheel license does not match canonical source")
    source_payload = _source_payload(project_root)
    wheel_payload = {name: files[name] for name in expected_modules}
    if wheel_payload != source_payload:
        raise CoreReleaseError("core wheel payload does not match canonical source")
    _verify_record(files, dist)
    return {
        "kind": "wheel",
        "filename": path.name,
        "bytes": path.stat().st_size,
        "sha256": _sha256(path.read_bytes()),
        "member_inventory_sha256": _inventory(files),
        "source_payload_sha256": _inventory(source_payload),
        "metadata_sha256": metadata_sha256,
        "regular_file_count": len(files),
        "archive_member_count": len(infos),
        "compression": {
            "algorithm": "per-member-min-deflate9-bzip2-9-v1",
            "methods": compression_counts,
        },
        "modules": sorted(CORE_MODULES),
    }


def _expected_sdist_inventory(root: str, source_payload: dict[str, bytes]) -> tuple[set[str], set[str]]:
    files = {
        f"{root}/LICENSE",
        f"{root}/README.md",
        f"{root}/pyproject.toml",
        f"{root}/PKG-INFO",
        f"{root}/setup.cfg",
        *(f"{root}/src/{name}" for name in source_payload),
        *(f"{root}/src/sasori_core.egg-info/{name}" for name in SDIST_EGG_INFO_FILES),
    }
    directories = {root}
    for name in files:
        parts = PurePosixPath(name).parts
        directories.update(
            PurePosixPath(*parts[:index]).as_posix()
            for index in range(1, len(parts))
        )
    return files, directories


def verify_sdist(path: Path, version: str, project_root: Path) -> dict[str, object]:
    root = f"sasori_core-{version}"
    if path.is_symlink() or not path.is_file():
        raise CoreReleaseError("core sdist must be one regular file")
    if path.name != f"{root}.tar.gz":
        raise CoreReleaseError("core sdist name violates the contract")
    files: dict[str, bytes] = {}
    directories: set[str] = set()
    seen: set[str] = set()
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        if not members or len(members) > MAX_MEMBERS:
            raise CoreReleaseError("core sdist member count is invalid")
        total = 0
        for member in members:
            parts = _safe(member.name)
            if parts[0] != root:
                raise CoreReleaseError("core sdist has more than one top-level directory")
            folded = member.name.rstrip("/").casefold()
            if folded in seen:
                raise CoreReleaseError("core sdist has duplicate/colliding members")
            seen.add(folded)
            if not (member.isfile() or member.isdir()):
                raise CoreReleaseError("core sdist links/devices are forbidden")
            mode = stat.S_IMODE(member.mode)
            if member.isdir():
                if mode & 0o222 == 0 or mode & 0o111 == 0:
                    raise CoreReleaseError("core sdist directory mode is invalid")
                directories.add(member.name.rstrip("/"))
                continue
            if mode & 0o111 or member.size > MAX_MEMBER_BYTES:
                raise CoreReleaseError("core sdist contains invalid file mode or size")
            stream = archive.extractfile(member)
            if stream is None:
                raise CoreReleaseError("core sdist member is unreadable")
            value = stream.read()
            total += len(value)
            if total > MAX_UNCOMPRESSED_BYTES:
                raise CoreReleaseError("core sdist expands beyond the size budget")
            files[member.name] = value

    source_payload = _source_payload(project_root)
    expected_files, expected_directories = _expected_sdist_inventory(root, source_payload)
    if set(files) != expected_files or directories != expected_directories:
        raise CoreReleaseError("core sdist file/directory inventory is invalid")
    for relative in ("LICENSE", "README.md", "pyproject.toml"):
        if files[f"{root}/{relative}"] != (project_root / relative).read_bytes():
            raise CoreReleaseError(f"core sdist {relative} does not match canonical source")
    metadata_sha256 = _metadata(
        files[f"{root}/PKG-INFO"], version, (project_root / "README.md").read_bytes()
    )
    if files[f"{root}/src/sasori_core.egg-info/PKG-INFO"] != files[f"{root}/PKG-INFO"]:
        raise CoreReleaseError("core sdist PKG-INFO copies do not match")
    if files[f"{root}/src/sasori_core.egg-info/top_level.txt"].decode("utf-8").splitlines() != ["sasori_core"]:
        raise CoreReleaseError("core sdist top_level.txt is invalid")
    if files[f"{root}/src/sasori_core.egg-info/dependency_links.txt"].strip():
        raise CoreReleaseError("core sdist dependency_links.txt is invalid")
    expected_sources = {
        "LICENSE",
        "README.md",
        "pyproject.toml",
        *(f"src/{name}" for name in source_payload),
        *(f"src/sasori_core.egg-info/{name}" for name in SDIST_EGG_INFO_FILES),
    }
    actual_sources = set(
        files[f"{root}/src/sasori_core.egg-info/SOURCES.txt"]
        .decode("utf-8")
        .splitlines()
    )
    if actual_sources != expected_sources or len(actual_sources) != len(expected_sources):
        raise CoreReleaseError("core sdist SOURCES.txt is invalid")
    if files[f"{root}/setup.cfg"].replace(b"\r\n", b"\n") != b"[egg_info]\ntag_build = \ntag_date = 0\n\n":
        raise CoreReleaseError("core sdist generated setup.cfg is invalid")
    sdist_payload = {
        name.removeprefix(f"{root}/src/"): value
        for name, value in files.items()
        if name.startswith(f"{root}/src/sasori_core/")
    }
    if sdist_payload != source_payload:
        raise CoreReleaseError("core sdist payload does not match canonical source")
    return {
        "kind": "sdist",
        "filename": path.name,
        "bytes": path.stat().st_size,
        "sha256": _sha256(path.read_bytes()),
        "member_inventory_sha256": _inventory(files),
        "source_payload_sha256": _inventory(source_payload),
        "metadata_sha256": metadata_sha256,
        "regular_file_count": len(files),
        "archive_member_count": len(members),
        "sources": sorted(CORE_MODULES),
    }


def verify(wheel: Path, sdist: Path, project_root: Path) -> dict[str, object]:
    version = _project(project_root)
    wheel_result = verify_wheel(wheel, version, project_root)
    sdist_result = verify_sdist(sdist, version, project_root)
    if wheel_result["source_payload_sha256"] != sdist_result["source_payload_sha256"]:
        raise CoreReleaseError("core wheel and sdist source payloads do not match")
    if wheel_result["metadata_sha256"] != sdist_result["metadata_sha256"]:
        raise CoreReleaseError("core wheel and sdist package metadata do not match")
    return {
        "schema_version": 2,
        "kind": "sasori.core-artifact-verification",
        "verifier_version": VERIFIER_VERSION,
        "project": {
            "name": "sasori-core",
            "version": version,
            "runtime_dependencies": [],
            "wheel_size_ceiling": MAX_WHEEL_BYTES,
            "source_payload_algorithm": SOURCE_PAYLOAD_ALGORITHM,
            "source_payload_sha256": wheel_result["source_payload_sha256"],
        },
        "artifacts": [wheel_result, sdist_result],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--sdist", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).parents[1])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = verify(args.wheel, args.sdist, args.project_root)
    except (CoreReleaseError, OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        print(f"core release verification failed: {exc}")
        return 2
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n"
    if args.output is None:
        print(encoded, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
