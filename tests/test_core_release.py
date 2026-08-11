from __future__ import annotations

import base64
import csv
import hashlib
import importlib.util
import io
import stat
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "packages" / "sasori-core"
VERSION = "0.1.0.dev1"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


release_verify = _load(
    "sasori_core_release_verify", CORE / "scripts" / "release_verify.py"
)
wheel_repack = _load("sasori_wheel_repack_for_core_tests", ROOT / "scripts" / "repack_wheel.py")


def _metadata() -> bytes:
    headers = [
        "Metadata-Version: 2.4",
        "Name: sasori-core",
        f"Version: {VERSION}",
        "Summary: Sasori core release test fixture",
        "License-Expression: MIT",
        "Requires-Python: <3.14,>=3.11",
        "Description-Content-Type: text/markdown",
        "License-File: LICENSE",
        "Dynamic: license-file",
        *(
            f"Project-URL: {name}, {url}"
            for name, url in release_verify.PROJECT_URLS.items()
        ),
    ]
    return "\n".join(headers).encode("utf-8") + b"\n\n" + (CORE / "README.md").read_bytes()


def _record(files: dict[str, bytes], record_name: str) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    for name in sorted({*files, record_name}):
        if name == record_name:
            writer.writerow((name, "", ""))
            continue
        digest = base64.urlsafe_b64encode(hashlib.sha256(files[name]).digest()).rstrip(b"=").decode("ascii")
        writer.writerow((name, f"sha256={digest}", str(len(files[name]))))
    return stream.getvalue().encode("utf-8")


def _wheel_files() -> dict[str, bytes]:
    files = release_verify._source_payload(CORE)
    dist = f"sasori_core-{VERSION}.dist-info"
    files.update(
        {
            f"{dist}/licenses/LICENSE": (CORE / "LICENSE").read_bytes(),
            f"{dist}/METADATA": _metadata(),
            f"{dist}/WHEEL": (
                b"Wheel-Version: 1.0\n"
                b"Generator: sasori-test\n"
                b"Root-Is-Purelib: true\n"
                b"Tag: py3-none-any\n\n"
            ),
            f"{dist}/top_level.txt": b"sasori_core\n",
        }
    )
    record_name = f"{dist}/RECORD"
    files[record_name] = _record(files, record_name)
    return files


def _write_wheel(path: Path, files: dict[str, bytes], *, comment: bytes = b"") -> None:
    dist_prefix = f"sasori_core-{VERSION}.dist-info/"
    names = sorted(name for name in files if not name.startswith(dist_prefix)) + sorted(
        name for name in files if name.startswith(dist_prefix)
    )
    with zipfile.ZipFile(path, "w", allowZip64=False) as archive:
        for name in names:
            info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, files[name], compress_type=zipfile.ZIP_DEFLATED)
        archive.comment = comment
    if not comment:
        wheel_repack.repack(path)


def _sdist_files() -> dict[str, bytes]:
    root = f"sasori_core-{VERSION}"
    source = release_verify._source_payload(CORE)
    source_names = {
        "LICENSE",
        "README.md",
        "pyproject.toml",
        *(f"src/{name}" for name in source),
        *(
            f"src/sasori_core.egg-info/{name}"
            for name in release_verify.SDIST_EGG_INFO_FILES
        ),
    }
    files = {
        f"{root}/LICENSE": (CORE / "LICENSE").read_bytes(),
        f"{root}/README.md": (CORE / "README.md").read_bytes(),
        f"{root}/pyproject.toml": (CORE / "pyproject.toml").read_bytes(),
        f"{root}/PKG-INFO": _metadata(),
        f"{root}/setup.cfg": b"[egg_info]\ntag_build = \ntag_date = 0\n\n",
        f"{root}/src/sasori_core.egg-info/PKG-INFO": _metadata(),
        f"{root}/src/sasori_core.egg-info/SOURCES.txt": "\n".join(
            sorted(source_names)
        ).encode("utf-8"),
        f"{root}/src/sasori_core.egg-info/dependency_links.txt": b"\n",
        f"{root}/src/sasori_core.egg-info/top_level.txt": b"sasori_core\n",
    }
    files.update({f"{root}/src/{name}": value for name, value in source.items()})
    return files


def _write_sdist(path: Path, files: dict[str, bytes]) -> None:
    directories: set[str] = set()
    for name in files:
        parts = PurePosixPath(name).parts
        directories.update(
            PurePosixPath(*parts[:index]).as_posix()
            for index in range(1, len(parts))
        )
    with tarfile.open(path, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        for name in sorted(directories, key=lambda item: (item.count("/"), item)):
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            info.mtime = 0
            archive.addfile(info)
        for name in sorted(files):
            value = files[name]
            info = tarfile.TarInfo(name)
            info.mode = 0o644
            info.mtime = 0
            info.size = len(value)
            archive.addfile(info, io.BytesIO(value))


class CoreReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.wheel = self.directory / f"sasori_core-{VERSION}-py3-none-any.whl"
        self.sdist = self.directory / f"sasori_core-{VERSION}.tar.gz"
        _write_wheel(self.wheel, _wheel_files())
        _write_sdist(self.sdist, _sdist_files())

    def test_checked_in_core_project_is_zero_dependency_and_version_aligned(self):
        version = release_verify._project(CORE)
        self.assertEqual(version, VERSION)
        self.assertEqual(release_verify.VERIFIER_VERSION, "3")
        self.assertEqual(release_verify.MAX_WHEEL_BYTES, 128 * 1024)

    def test_canonical_wheel_sdist_and_source_are_cryptographically_bound(self):
        value = release_verify.verify(self.wheel, self.sdist, CORE)
        wheel, sdist = value["artifacts"]
        self.assertEqual(value["schema_version"], 2)
        self.assertEqual(value["project"]["runtime_dependencies"], [])
        self.assertEqual(
            wheel["source_payload_sha256"],
            sdist["source_payload_sha256"],
        )
        self.assertEqual(wheel["metadata_sha256"], sdist["metadata_sha256"])
        self.assertGreater(wheel["compression"]["methods"]["bzip2"], 0)
        self.assertGreater(wheel["compression"]["methods"]["deflate"], 0)

    def test_metadata_binds_readme_and_accepts_only_crlf_equivalence(self):
        files = _wheel_files()
        metadata_name = f"sasori_core-{VERSION}.dist-info/METADATA"
        record_name = f"sasori_core-{VERSION}.dist-info/RECORD"
        files[metadata_name] = files[metadata_name].replace(b"\n", b"\r\n")
        files[record_name] = _record(
            {name: value for name, value in files.items() if name != record_name},
            record_name,
        )
        _write_wheel(self.wheel, files)
        value = release_verify.verify(self.wheel, self.sdist, CORE)
        wheel, sdist = value["artifacts"]
        self.assertEqual(wheel["metadata_sha256"], sdist["metadata_sha256"])

        files = _wheel_files()
        files[metadata_name] += b"stale\n"
        files[record_name] = _record(
            {name: value for name, value in files.items() if name != record_name},
            record_name,
        )
        _write_wheel(self.wheel, files)
        with self.assertRaisesRegex(release_verify.CoreReleaseError, "README.md"):
            release_verify.verify(self.wheel, self.sdist, CORE)

    def test_wheel_only_payload_tamper_with_valid_record_is_rejected(self):
        files = _wheel_files()
        files["sasori_core/runtime.py"] += b"\n# wheel-only tamper\n"
        record_name = f"sasori_core-{VERSION}.dist-info/RECORD"
        files[record_name] = _record(
            {name: value for name, value in files.items() if name != record_name},
            record_name,
        )
        _write_wheel(self.wheel, files)
        with self.assertRaisesRegex(release_verify.CoreReleaseError, "canonical source"):
            release_verify.verify(self.wheel, self.sdist, CORE)

    def test_sdist_only_extra_and_missing_source_are_rejected(self):
        files = _sdist_files()
        root = f"sasori_core-{VERSION}"
        files[f"{root}/src/sasori_core/unexpected.py"] = b"raise SystemExit\n"
        _write_sdist(self.sdist, files)
        with self.assertRaisesRegex(release_verify.CoreReleaseError, "inventory"):
            release_verify.verify_sdist(self.sdist, VERSION, CORE)

        files = _sdist_files()
        del files[f"{root}/src/sasori_core/runtime.py"]
        _write_sdist(self.sdist, files)
        with self.assertRaisesRegex(release_verify.CoreReleaseError, "inventory"):
            release_verify.verify_sdist(self.sdist, VERSION, CORE)

    def test_wheel_comment_extra_dist_info_and_symlink_mode_are_rejected(self):
        _write_wheel(self.wheel, _wheel_files(), comment=b"comment")
        with self.assertRaisesRegex(release_verify.CoreReleaseError, "comment"):
            release_verify.verify_wheel(self.wheel, VERSION, CORE)

        files = _wheel_files()
        files[f"sasori_core-{VERSION}.dist-info/unexpected.txt"] = b"unexpected"
        record_name = f"sasori_core-{VERSION}.dist-info/RECORD"
        files[record_name] = _record(
            {name: value for name, value in files.items() if name != record_name},
            record_name,
        )
        _write_wheel(self.wheel, files)
        with self.assertRaisesRegex(release_verify.CoreReleaseError, "inventory"):
            release_verify.verify_wheel(self.wheel, VERSION, CORE)

        files = _wheel_files()
        with zipfile.ZipFile(self.wheel, "w") as archive:
            for name, value in files.items():
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = (
                    (stat.S_IFLNK | 0o777) if name == "sasori_core/runtime.py" else (stat.S_IFREG | 0o644)
                ) << 16
                archive.writestr(info, value, compress_type=zipfile.ZIP_DEFLATED)
        with self.assertRaises(release_verify.CoreReleaseError):
            release_verify.verify_wheel(self.wheel, VERSION, CORE)

    def test_case_collision_and_traversal_fail_closed(self):
        files = _sdist_files()
        root = f"sasori_core-{VERSION}"
        files[f"{root}/src/sasori_core/Runtime.py"] = b"pass\n"
        _write_sdist(self.sdist, files)
        with self.assertRaisesRegex(release_verify.CoreReleaseError, "colliding"):
            release_verify.verify_sdist(self.sdist, VERSION, CORE)

        bad_wheel = self.directory / "sasori_core-bad-py3-none-any.whl"
        with zipfile.ZipFile(bad_wheel, "w") as archive:
            archive.writestr("../escape", b"x")
        with self.assertRaisesRegex(release_verify.CoreReleaseError, "unsafe"):
            release_verify.verify_wheel(bad_wheel, "bad", CORE)


if __name__ == "__main__":
    unittest.main()
