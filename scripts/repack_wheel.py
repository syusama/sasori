from __future__ import annotations

import argparse
import bz2
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
import zipfile
import zlib


ALLOWED_METHODS = {zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2}
MAX_MEMBERS = 512
MAX_MEMBER_BYTES = 8 * 1024 * 1024
MAX_EXPANDED_BYTES = 32 * 1024 * 1024


class WheelRepackError(Exception):
    pass


def _regular_wheel(path: Path) -> Path:
    if path.is_symlink():
        raise WheelRepackError("wheel must not be a symlink")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise WheelRepackError("wheel is missing or unreadable") from exc
    if not resolved.is_file() or resolved.suffix != ".whl":
        raise WheelRepackError("wheel must be one regular .whl file")
    return resolved


def _safe_name(name: str) -> None:
    parts = PurePosixPath(name).parts
    if (
        not name
        or "\\" in name
        or name.startswith("/")
        or not parts
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise WheelRepackError("wheel contains an unsafe member path")


def _method(data: bytes) -> int:
    compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
    deflated = compressor.compress(data) + compressor.flush()
    compressed = bz2.compress(data, compresslevel=9)
    return zipfile.ZIP_BZIP2 if len(compressed) < len(deflated) else zipfile.ZIP_DEFLATED


def _clone(info: zipfile.ZipInfo) -> zipfile.ZipInfo:
    cloned = zipfile.ZipInfo(info.filename, info.date_time)
    cloned.comment = info.comment
    cloned.extra = info.extra
    cloned.create_system = info.create_system
    cloned.create_version = info.create_version
    cloned.extract_version = info.extract_version
    cloned.internal_attr = info.internal_attr
    cloned.external_attr = info.external_attr
    cloned.volume = info.volume
    return cloned


def _read(path: Path) -> tuple[list[zipfile.ZipInfo], dict[str, bytes]]:
    try:
        with zipfile.ZipFile(path) as archive:
            if archive.comment:
                raise WheelRepackError("wheel archive comment is not allowed")
            infos = archive.infolist()
            if not infos or len(infos) > MAX_MEMBERS:
                raise WheelRepackError("wheel member count is invalid")
            names: set[str] = set()
            files: dict[str, bytes] = {}
            expanded = 0
            for info in infos:
                _safe_name(info.filename)
                folded = info.filename.casefold()
                mode = info.external_attr >> 16
                kind = stat.S_IFMT(mode)
                if (
                    folded in names
                    or info.is_dir()
                    or (kind and kind != stat.S_IFREG)
                    or info.flag_bits & 0x9
                    or info.comment
                    or info.file_size > MAX_MEMBER_BYTES
                ):
                    raise WheelRepackError("wheel member contract is invalid")
                names.add(folded)
                expanded += info.file_size
                if expanded > MAX_EXPANDED_BYTES:
                    raise WheelRepackError("wheel expands beyond the repack limit")
                files[info.filename] = archive.read(info)
    except WheelRepackError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise WheelRepackError("wheel is unreadable") from exc
    return infos, files


def repack(path: Path) -> dict[str, object]:
    wheel = _regular_wheel(path)
    infos, files = _read(wheel)
    infos = [info for info in infos if ".dist-info/" not in info.filename] + [
        info for info in infos if ".dist-info/" in info.filename
    ]
    methods = {info.filename: _method(files[info.filename]) for info in infos}
    before_bytes = wheel.stat().st_size
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{wheel.name}.", suffix=".tmp", dir=wheel.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w", allowZip64=False) as archive:
            for info in infos:
                method = methods[info.filename]
                archive.writestr(
                    _clone(info),
                    files[info.filename],
                    compress_type=method,
                    compresslevel=9,
                )
        checked_infos, checked_files = _read(temporary)
        if [info.filename for info in checked_infos] != [info.filename for info in infos]:
            raise WheelRepackError("repacked wheel member order changed")
        if checked_files != files:
            raise WheelRepackError("repacked wheel payload changed")
        if any(
            info.compress_type != methods[info.filename]
            or info.compress_type not in ALLOWED_METHODS
            for info in checked_infos
        ):
            raise WheelRepackError("repacked wheel compression contract changed")
        os.chmod(temporary, stat.S_IMODE(wheel.stat().st_mode))
        os.replace(temporary, wheel)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    payload = wheel.read_bytes()
    counts = {
        "bzip2": sum(method == zipfile.ZIP_BZIP2 for method in methods.values()),
        "deflate": sum(method == zipfile.ZIP_DEFLATED for method in methods.values()),
    }
    return {
        "wheel": wheel.name,
        "before_bytes": before_bytes,
        "after_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "members": len(infos),
        "methods": counts,
    }


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Repack one wheel with the smaller of Deflate 9 and BZIP2 9 per member."
    )
    parser.add_argument("--wheel", required=True, type=Path)
    options = parser.parse_args(arguments)
    try:
        evidence = repack(options.wheel)
    except WheelRepackError as exc:
        parser.error(str(exc))
    print(json.dumps(evidence, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
