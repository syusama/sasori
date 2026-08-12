import base64
import csv
import hashlib
import importlib.util
import io
import json
import re
import shutil
import sys
import tarfile
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path, PurePosixPath
from unittest import mock


ROOT = Path(__file__).parents[1]
with (ROOT / "pyproject.toml").open("rb") as stream:
    PROJECT_VERSION = tomllib.load(stream)["project"]["version"]
EXPECTED_TAG = f"v{PROJECT_VERSION}"
WRONG_TAG = f"v{PROJECT_VERSION}.wrong"
SPEC = importlib.util.spec_from_file_location(
    "sasori_release_verify", ROOT / "scripts" / "release_verify.py"
)
release_verify = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = release_verify
SPEC.loader.exec_module(release_verify)
REPACK_SPEC = importlib.util.spec_from_file_location(
    "sasori_repack_wheel_for_release_tests", ROOT / "scripts" / "repack_wheel.py"
)
repack_wheel = importlib.util.module_from_spec(REPACK_SPEC)
sys.modules[REPACK_SPEC.name] = repack_wheel
REPACK_SPEC.loader.exec_module(repack_wheel)


def metadata(*, dependency=False, extra="", body: bytes | None = None):
    requires = f"Requires-Dist: sasori-core=={PROJECT_VERSION}\n"
    if dependency:
        requires += "Requires-Dist: unwanted>=1\n"
    header = (
        "Metadata-Version: 2.4\n"
        "Name: sasori\n"
        f"Version: {PROJECT_VERSION}\n"
        "Requires-Python: >=3.11,<3.14\n"
        "License-Expression: MIT\n"
        "Description-Content-Type: text/markdown\n"
        "License-File: LICENSE\n"
        "License-File: THIRD_PARTY_NOTICES.md\n"
        "License-File: licenses/CPYTHON-3.12-LICENSE.txt\n"
        "Project-URL: Documentation, https://github.com/syusama/sasori/blob/main/docs/FOUNDATION.md\n"
        "Project-URL: Homepage, https://github.com/syusama/sasori\n"
        "Project-URL: Issues, https://github.com/syusama/sasori/issues\n"
        "Project-URL: Repository, https://github.com/syusama/sasori\n"
        f"{extra}"
        f"{requires}\n"
    ).encode()
    return header + ((ROOT / "README.md").read_bytes() if body is None else body)


def jpeg_dimensions(payload: bytes) -> tuple[int, int]:
    if not payload.startswith(b"\xff\xd8"):
        raise ValueError("not a JPEG stream")
    standalone = {0x01, 0xD8, 0xD9, *range(0xD0, 0xD8)}
    start_of_frame = {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
    offset = 2
    while offset < len(payload):
        if payload[offset] != 0xFF:
            raise ValueError("invalid JPEG marker prefix")
        while offset < len(payload) and payload[offset] == 0xFF:
            offset += 1
        if offset >= len(payload):
            break
        marker = payload[offset]
        offset += 1
        if marker in standalone:
            continue
        if offset + 2 > len(payload):
            break
        length = int.from_bytes(payload[offset : offset + 2], "big")
        if length < 2 or offset + length > len(payload):
            raise ValueError("invalid JPEG segment length")
        if marker in start_of_frame:
            if length < 7:
                raise ValueError("invalid JPEG start-of-frame segment")
            height = int.from_bytes(payload[offset + 3 : offset + 5], "big")
            width = int.from_bytes(payload[offset + 5 : offset + 7], "big")
            if width < 1 or height < 1:
                raise ValueError("invalid JPEG dimensions")
            return width, height
        if marker == 0xDA:
            break
        offset += length
    raise ValueError("JPEG dimensions not found")


def png_dimensions(payload: bytes) -> tuple[int, int]:
    expected_header = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    if len(payload) < 24 or payload[:16] != expected_header:
        raise ValueError("not a PNG stream with an IHDR header")
    width = int.from_bytes(payload[16:20], "big")
    height = int.from_bytes(payload[20:24], "big")
    if width < 1 or height < 1:
        raise ValueError("invalid PNG dimensions")
    return width, height


class ReleaseVerificationTests(unittest.TestCase):
    def test_release_contract_version_tracks_every_decision_record(self):
        self.assertEqual(release_verify.VERIFIER_VERSION, "15")
        self.assertEqual(
            release_verify.SOURCE_TREE_ALGORITHM, "sasori-source-tree-v12"
        )
        expected = {
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / "docs").glob("ADR-*.md")
        }
        declared = {
            path for path in release_verify.RELEASE_DOCS if path.startswith("docs/ADR-")
        }
        self.assertEqual(
            declared,
            expected,
            "every accepted ADR must be an exact release source-tree input",
        )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        for name in (
            "pyproject.toml",
            "MANIFEST.in",
            "README.md",
            "README_zh.md",
            "README_ja.md",
            "README_ko.md",
            "LICENSE",
            "SECURITY.md",
            "THIRD_PARTY_NOTICES.md",
            "requirements-build.txt",
            "Dockerfile",
            ".dockerignore",
        ):
            shutil.copy2(ROOT / name, self.source / name)
        shutil.copytree(ROOT / "licenses", self.source / "licenses")
        shutil.copytree(ROOT / "docs", self.source / "docs")
        shutil.copytree(ROOT / "src", self.source / "src", ignore=shutil.ignore_patterns("*.egg-info", "__pycache__"))

    def test_saved_workflow_adr_is_bound_into_the_release_source_tree(self):
        before_digest, before_count = release_verify._source_tree(self.source)
        decision = (
            self.source / "docs" / "ADR-0017-DURABLE-SAVED-WORKFLOW-CATALOG.md"
        )
        decision.write_bytes(decision.read_bytes() + b"\n")
        after_digest, after_count = release_verify._source_tree(self.source)
        self.assertEqual(after_count, before_count)
        self.assertNotEqual(after_digest, before_digest)

    def test_multilingual_readmes_and_real_screenshot_inventory_are_bound(self):
        readmes = {
            "README.md": (
                "A small Python agent runtime. A complete framework when you need one."
            ),
            "README_zh.md": "从轻量 Python Agent 核心开始，按需要扩展成完整框架。",
            "README_ja.md": (
                "小さな Python Agent ランタイムから始め、"
                "必要なときだけ完全なフレームワークへ。"
            ),
            "README_ko.md": (
                "작은 Python Agent 런타임으로 시작해, "
                "필요할 때 완전한 프레임워크로 확장합니다."
            ),
        }
        screenshot_references = {}
        for name, marker in readmes.items():
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn(marker, text)
            for peer in readmes:
                if peer != name:
                    self.assertIn(peer, text)
            self.assertIn("docs/assets/sasori-banner.png", text)
            self.assertNotIn("docs/assets/sasori-logo.jpg", text)
            screenshot_references[name] = set(
                re.findall(r"docs/assets/screenshots/([^\"') ]+\.jpg)", text)
            )
        self.assertTrue(screenshot_references["README.md"])
        self.assertTrue(
            all(
                references == screenshot_references["README.md"]
                for references in screenshot_references.values()
            )
        )

        with (ROOT / "pyproject.toml").open("rb") as stream:
            self.assertEqual(tomllib.load(stream)["project"]["readme"], "README.md")
        manifest_lines = {
            line.strip()
            for line in (ROOT / "MANIFEST.in").read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        self.assertTrue(
            {f"include {name}" for name in readmes} <= manifest_lines
        )
        self.assertIn("recursive-include docs *.md *.jpg *.json", manifest_lines)

        banner_path = ROOT / "docs" / "assets" / "sasori-banner.png"
        banner_payload = banner_path.read_bytes()
        self.assertEqual(banner_payload[:16], b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
        self.assertEqual(
            (int.from_bytes(banner_payload[16:20], "big"), int.from_bytes(banner_payload[20:24], "big")),
            (1024, 1536),
        )
        self.assertEqual(
            hashlib.sha256(banner_payload).hexdigest(),
            "9b35d3b6049b2372137a9f28ffe216f885d674935a39bd81f9b42000e15e3bc3",
        )
        for relative, expected_count in (
            ("Dockerfile", 1),
            (".github/workflows/ci.yml", 2),
            (".github/workflows/testpypi.yml", 1),
            ("docs/RELEASE.md", 1),
        ):
            surface = (ROOT / relative).read_text(encoding="utf-8")
            for translated in ("README_zh.md", "README_ja.md", "README_ko.md"):
                self.assertEqual(
                    surface.count(translated),
                    expected_count,
                    f"{relative} must copy {translated} on every release source path",
                )
        self.assertEqual(release_verify.MAX_WHEEL_BYTES, 250 * 1024)

        manifest_path = ROOT / "docs" / "assets" / "screenshots-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], 1)
        self.assertTrue(manifest["real_server_journey"])
        self.assertEqual(manifest["browser"]["console_entries_after_journey"], 0)

        logo = manifest["brand_logo"]
        logo_path = ROOT / logo["path"]
        logo_payload = logo_path.read_bytes()
        self.assertEqual(logo["path"], "docs/assets/sasori-logo.jpg")
        self.assertEqual(logo["placement"], "Workbench brand mark and repository asset")
        self.assertEqual(logo["media_type"], "image/jpeg")
        self.assertEqual(logo["bytes"], len(logo_payload))
        self.assertEqual(
            logo["sha256"], hashlib.sha256(logo_payload).hexdigest()
        )
        self.assertEqual(
            logo["actual_pixels"],
            dict(zip(("width", "height"), jpeg_dimensions(logo_payload))),
        )
        workbench_logo = ROOT / "src" / "sasori_web" / "mark.0.2.0.jpg"
        workbench_payload = workbench_logo.read_bytes()
        self.assertEqual(jpeg_dimensions(workbench_payload), (96, 96))
        self.assertLess(len(workbench_payload), 8 * 1024)

        commit = manifest["runtime_source_commit"]
        self.assertRegex(commit, r"\A[0-9a-f]{40}\Z")
        self.assertEqual(manifest["run"]["durable_event_count"], 17)
        self.assertEqual(manifest["workflow"]["preflight_model_calls"], 0)
        self.assertEqual(manifest["workflow"]["preflight_tool_dispatches"], 0)

        expected_paths = {
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / "docs" / "assets" / "screenshots").glob("*.jpg")
        }
        entries = manifest["captures"]
        declared_paths = {entry["path"] for entry in entries}
        self.assertEqual(declared_paths, expected_paths)
        self.assertEqual(len(entries), len(declared_paths))
        release_assets = set(release_verify.RELEASE_ASSETS)
        self.assertNotIn("docs/assets/sasori-banner.png", release_assets)
        self.assertEqual(
            release_assets,
            {
                "README_zh.md",
                "README_ja.md",
                "README_ko.md",
                "docs/assets/sasori-logo.jpg",
                "docs/assets/screenshots-manifest.json",
                *expected_paths,
            },
        )

        for entry in entries:
            path = ROOT / entry["path"]
            payload = path.read_bytes()
            self.assertEqual(entry["media_type"], "image/jpeg")
            self.assertEqual(entry["bytes"], len(payload))
            self.assertEqual(entry["sha256"], hashlib.sha256(payload).hexdigest())
            self.assertEqual(
                entry["actual_pixels"],
                dict(zip(("width", "height"), jpeg_dimensions(payload))),
            )
            self.assertTrue(path.name.endswith(f"-{commit[:7]}.jpg"))
            self.assertGreater(entry["requested_viewport"]["width"], 0)
            self.assertGreater(entry["requested_viewport"]["height"], 0)
            self.assertIsInstance(entry["reduced_motion"], bool)
            self.assertTrue(entry["scenario"].strip())

    def test_locked_container_build_normalizes_sdist_file_modes(self):
        surfaces = {
            "docs/RELEASE.md": "/tmp/sasori",
            ".github/workflows/ci.yml": "/tmp/sasori /tmp/sasori-core",
            ".github/workflows/testpypi.yml": "/tmp/sasori /tmp/sasori-core",
        }
        for relative, roots in surfaces.items():
            with self.subTest(surface=relative):
                surface = (ROOT / relative).read_text(encoding="utf-8")
                directory_mode = f"find {roots} -type d -exec chmod 0755 {{}} +"
                file_mode = f"find {roots} -type f -exec chmod 0644 {{}} +"
                self.assertEqual(surface.count(directory_mode), 1)
                self.assertEqual(surface.count(file_mode), 1)
                self.assertLess(
                    surface.index("-prune -exec rm -rf -- {} +"),
                    surface.index(directory_mode),
                )
                if relative != "docs/RELEASE.md":
                    host_directories = (
                        'mkdir -p "$GITHUB_WORKSPACE/dist/build-wheelhouse" '
                        '"$GITHUB_WORKSPACE/dist/sasori-core"'
                    )
                    self.assertEqual(surface.count(host_directories), 1)
                    self.assertLess(
                        surface.index(host_directories), surface.index("docker run")
                    )

    def test_project_metadata_requires_the_file_backed_readme(self):
        pyproject = self.source / "pyproject.toml"
        original = pyproject.read_text(encoding="utf-8")
        pyproject.write_text(
            original.replace(
                'readme = "README.md"',
                'readme = {text = "stale", content-type = "text/markdown"}',
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            release_verify.ReleaseVerificationError,
            "project metadata violates the release contract",
        ):
            release_verify._project(self.source)

    def test_docker_context_must_exclude_secrets(self):
        dockerignore = self.source / ".dockerignore"
        original = dockerignore.read_text(encoding="utf-8")
        for unsafe in (
            original.replace(".secrets\n", ""),
            original + "\n!.secrets/container-test-token\n",
        ):
            with self.subTest(unsafe=unsafe):
                dockerignore.write_text(unsafe, encoding="utf-8")
                with self.assertRaisesRegex(
                    release_verify.ReleaseVerificationError,
                    "Docker context must exclude .secrets without negation",
                ):
                    release_verify._build_inputs(self.source)
        dockerignore.write_text(original, encoding="utf-8")

    def test_docker_context_must_exclude_private_root_downloads(self):
        dockerignore = self.source / ".dockerignore"
        original = dockerignore.read_text(encoding="utf-8")
        for unsafe in (
            original.replace("/downloads/\n", ""),
            original + "\n!/downloads/private\n",
        ):
            with self.subTest(unsafe=unsafe):
                dockerignore.write_text(unsafe, encoding="utf-8")
                with self.assertRaisesRegex(
                    release_verify.ReleaseVerificationError,
                    "exclude root downloads without negation",
                ):
                    release_verify._build_inputs(self.source)
        dockerignore.write_text(original, encoding="utf-8")

    def test_docker_context_must_recursively_exclude_python_build_caches(self):
        dockerignore = self.source / ".dockerignore"
        original = dockerignore.read_text(encoding="utf-8")
        for required in (
            "**/__pycache__",
            "**/__pycache__/**",
            "**/*.py[cod]",
            "**/*.egg-info",
            "**/*.egg-info/**",
        ):
            with self.subTest(required=required):
                unsafe = original.replace(required + "\n", "")
                dockerignore.write_text(unsafe, encoding="utf-8")
                with self.assertRaisesRegex(
                    release_verify.ReleaseVerificationError,
                    "recursively exclude Python build caches",
                ):
                    release_verify._build_inputs(self.source)
        dockerignore.write_text(original, encoding="utf-8")

    def test_docker_builder_must_prune_generated_python_build_state(self):
        dockerfile = self.source / "Dockerfile"
        original = dockerfile.read_text(encoding="utf-8")
        for marker in (
            "COPY pyproject.toml MANIFEST.in ",
            'find src packages -type d \\( -name "*.egg-info" -o -name "__pycache__" \\)',
            'find src packages -type f \\( -name "*.pyc" -o -name "*.pyo" \\) -delete',
        ):
            with self.subTest(marker=marker):
                dockerfile.write_text(original.replace(marker, "removed", 1), encoding="utf-8")
                with self.assertRaisesRegex(
                    release_verify.ReleaseVerificationError,
                    "Docker builder must prune generated Python build state",
                ):
                    release_verify._build_inputs(self.source)
        dockerfile.write_text(original, encoding="utf-8")

    def wheel(
        self,
        *,
        dependency=False,
        corrupt_record=False,
        metadata_extra="",
        wheel_extra="",
        extra_dist_info=False,
        metadata_body=None,
        compression=zipfile.ZIP_DEFLATED,
        archive_comment=b"",
        dist_info_first=False,
        repack=True,
    ):
        path = self.root / f"sasori-{PROJECT_VERSION}-py3-none-any.whl"
        dist_info = f"sasori-{PROJECT_VERSION}.dist-info"
        files = release_verify._source_payload(self.source)
        files.update(
            {
                f"{dist_info}/METADATA": metadata(
                    dependency=dependency,
                    extra=metadata_extra,
                    body=metadata_body,
                ),
                f"{dist_info}/WHEEL": (
                    "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\n"
                    f"Tag: py3-none-any\n{wheel_extra}\n"
                ).encode(),
                f"{dist_info}/entry_points.txt": (
                    "[console_scripts]\n"
                    "sasori = sasori.cli:main\n"
                    "sasori-server = sasori.server:main\n"
                    "sasori-catalog = sasori_market.catalog:main\n\n"
                    "[sasori.plugins]\n"
                    "com.sasori.workspace = sasori_plugins.workspace:register\n"
                    "com.sasori.web-fetch = sasori_plugins.web_fetch:register\n"
                    "com.sasori.rag-sqlite = sasori_plugins.rag_sqlite:register\n"
                    "com.sasori.git = sasori_plugins.git:register\n"
                ).encode(),
                f"{dist_info}/top_level.txt": (
                    "\n".join(release_verify.TOP_LEVEL_PACKAGES) + "\n"
                ).encode(),
            }
        )
        files.update(
            {
                f"{dist_info}/licenses/{name}": (self.source / name).read_bytes()
                for name in release_verify.LICENSE_FILES
            }
        )
        if extra_dist_info:
            files[f"{dist_info}/private.txt"] = b"must not ship"
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\n")
        for name, value in files.items():
            digest = base64.urlsafe_b64encode(hashlib.sha256(value).digest()).rstrip(b"=").decode()
            writer.writerow((name, f"sha256={digest}", len(value)))
        record_name = f"{dist_info}/RECORD"
        writer.writerow((record_name, "", ""))
        files[record_name] = output.getvalue().encode()
        if corrupt_record:
            files["sasori/__init__.py"] += b"\n# tampered after RECORD\n"
        ordered = list(files.items())
        if dist_info_first:
            ordered.sort(key=lambda item: not item[0].startswith(f"{dist_info}/"))
        with zipfile.ZipFile(path, "w", compression=compression) as archive:
            for name, value in ordered:
                archive.writestr(name, value, compress_type=compression)
            archive.comment = archive_comment
        if repack:
            repack_wheel.repack(path)
        return path

    def sdist(
        self,
        *,
        traversal=False,
        extra_directory=False,
        valid_directories=False,
        metadata_body=None,
    ):
        path = self.root / f"sasori-{PROJECT_VERSION}.tar.gz"
        root = f"sasori-{PROJECT_VERSION}"
        files = {
            f"{root}/{name}": (self.source / name).read_bytes()
            for name in (
                "pyproject.toml",
                "MANIFEST.in",
                "README.md",
                *release_verify.LICENSE_FILES,
                *release_verify.RELEASE_DOCS,
                *release_verify.RELEASE_ASSETS,
            )
        }
        files[f"{root}/PKG-INFO"] = metadata(body=metadata_body)
        files.update(
            {
                f"{root}/src/{name}": value
                for name, value in release_verify._source_payload(self.source).items()
            }
        )
        if traversal:
            files[f"{root}/../escape"] = b"no"
        with tarfile.open(path, "w:gz") as archive:
            if valid_directories:
                directories = {
                    PurePosixPath(*PurePosixPath(name).parts[:index]).as_posix()
                    for name in files
                    for index in range(1, len(PurePosixPath(name).parts))
                }
                for name in sorted(directories):
                    info = tarfile.TarInfo(name)
                    info.type = tarfile.DIRTYPE
                    archive.addfile(info)
            for name, value in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(value)
                archive.addfile(info, io.BytesIO(value))
            if extra_directory:
                info = tarfile.TarInfo(f"{root}/unexpected-empty")
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
        return path

    def test_valid_artifacts_write_honest_dirty_local_records(self):
        release_verify._reject_forbidden(
            (f"sasori-{PROJECT_VERSION}", "src", "sasori.egg-info"), "sdist"
        )
        output = self.root / "records"
        manifest, spdx, provenance = release_verify.verify_release(
            self.wheel(), self.sdist(), self.source, output
        )
        self.assertEqual([item["kind"] for item in manifest["artifacts"]], ["wheel", "sdist"])
        self.assertEqual(
            manifest["project"]["runtime_dependencies"],
            [f"sasori-core=={PROJECT_VERSION}"],
        )
        self.assertEqual(spdx["spdxVersion"], "SPDX-2.3")
        package_by_id = {item["SPDXID"]: item for item in spdx["packages"]}
        for artifact in manifest["artifacts"]:
            self.assertEqual(artifact["regular_file_count"], artifact["archive_member_count"])
        for artifact in manifest["artifacts"]:
            package = package_by_id[f"SPDXRef-Package-sasori-{artifact['kind']}"]
            self.assertEqual(package["name"], artifact["filename"])
            self.assertEqual(
                package["checksums"],
                [{"algorithm": "SHA256", "checksumValue": artifact["sha256"]}],
            )
        self.assertTrue(
            all(
                item["primaryPackagePurpose"]
                in release_verify.SPDX_PACKAGE_PURPOSES
                for item in spdx["packages"]
            )
        )
        setuptools = package_by_id["SPDXRef-Package-setuptools-build"]
        self.assertEqual(setuptools["licenseDeclared"], "MIT")
        self.assertEqual(setuptools["licenseConcluded"], "MIT")
        self.assertFalse(provenance["release_eligible"])
        self.assertEqual(provenance["source"]["state"], "unversioned_local_candidate")
        self.assertFalse(provenance["source"]["head_is_artifact_source_identity"])
        self.assertEqual(
            json.loads((output / "artifact-manifest.json").read_text(encoding="utf-8"))["kind"],
            "sasori.artifact-manifest",
        )

    def test_rejects_runtime_dependency_and_record_tampering(self):
        for options in ({"dependency": True}, {"corrupt_record": True}):
            with self.subTest(options=options), self.assertRaises(
                release_verify.ReleaseVerificationError
            ):
                release_verify.verify_wheel(
                    self.wheel(**options),
                    self.source,
                    release_verify._project(self.source),
                )

    def test_wheel_and_sdist_metadata_bind_the_exact_readme_body(self):
        project = release_verify._project(self.source)
        for body in (b"", b"# stale or substituted project page\n"):
            with self.subTest(kind="wheel", body=body), self.assertRaisesRegex(
                release_verify.ReleaseVerificationError,
                "description does not match README.md",
            ):
                release_verify.verify_wheel(
                    self.wheel(metadata_body=body), self.source, project
                )
            with self.subTest(kind="sdist", body=body), self.assertRaisesRegex(
                release_verify.ReleaseVerificationError,
                "description does not match README.md",
            ):
                release_verify.verify_sdist(
                    self.sdist(metadata_body=body), self.source, project
                )

    def test_wheel_compression_contract_requires_canonical_mixed_streams(self):
        project = release_verify._project(self.source)
        accepted = release_verify.verify_wheel(
            self.wheel(), self.source, project
        )
        self.assertEqual(
            accepted["compression"]["algorithm"],
            "per-member-min-deflate9-bzip2-9-v1",
        )
        self.assertGreater(accepted["compression"]["methods"]["deflate"], 0)
        self.assertGreater(accepted["compression"]["methods"]["bzip2"], 0)
        for options in (
            {"compression": zipfile.ZIP_DEFLATED, "repack": False},
            {"compression": zipfile.ZIP_BZIP2, "repack": False},
            {"compression": zipfile.ZIP_STORED, "repack": False},
            {"archive_comment": b"comment", "repack": False},
            {"dist_info_first": True, "repack": False},
        ):
            with self.subTest(options=options), self.assertRaises(
                release_verify.ReleaseVerificationError
            ):
                release_verify.verify_wheel(
                    self.wheel(**options), self.source, project
                )

    def test_rejects_archive_path_traversal(self):
        for options in ({"traversal": True}, {"extra_directory": True}):
            with self.subTest(options=options), self.assertRaises(
                release_verify.ReleaseVerificationError
            ):
                release_verify.verify_sdist(
                    self.sdist(**options),
                    self.source,
                    release_verify._project(self.source),
                )

    def test_sdist_counts_valid_directory_members_separately(self):
        result = release_verify.verify_sdist(
            self.sdist(valid_directories=True),
            self.source,
            release_verify._project(self.source),
        )
        self.assertGreater(
            result["archive_member_count"], result["regular_file_count"]
        )

    def test_rejects_ambiguous_wheel_metadata_and_extra_dist_info(self):
        cases = (
            {"metadata_extra": "Name: other\n"},
            {"metadata_extra": "License-Expression: Apache-2.0\n"},
            {"metadata_extra": "Description-Content-Type: text/plain\n"},
            {"metadata_extra": "License-File: EXTRA\n"},
            {"wheel_extra": "Tag: py3-none-any\n"},
            {"wheel_extra": "Unknown: value\n"},
            {"extra_dist_info": True},
        )
        for options in cases:
            with self.subTest(options=options), self.assertRaises(
                release_verify.ReleaseVerificationError
            ):
                release_verify.verify_wheel(
                    self.wheel(**options),
                    self.source,
                    release_verify._project(self.source),
                )

    def test_release_eligibility_requires_the_exact_version_tag(self):
        def git_result(stdout=b""):
            return mock.Mock(returncode=0, stdout=stdout)

        cases = (
            (b"", b"test\n", False, "clean_wrong_tag_local_candidate"),
            (
                b"",
                f"{WRONG_TAG}\n".encode(),
                False,
                "clean_wrong_tag_local_candidate",
            ),
            (
                b"",
                f"{EXPECTED_TAG}\n".encode(),
                True,
                "clean_release_tag_candidate",
            ),
            (
                b"",
                f"{WRONG_TAG}\n{EXPECTED_TAG}\n".encode(),
                True,
                "clean_release_tag_candidate",
            ),
            (
                b"?? local\n",
                f"{EXPECTED_TAG}\n".encode(),
                False,
                "dirty_or_untracked_local_candidate",
            ),
        )
        for status, tags, eligible, state_name in cases:
            with self.subTest(tags=tags, status=status), mock.patch.object(
                release_verify,
                "_git",
                side_effect=(git_result(b"abc123\n"), git_result(status), git_result(tags)),
            ):
                state = release_verify._source_state(
                    self.source, EXPECTED_TAG
                )
                self.assertEqual(state["state"], state_name)
                self.assertEqual(
                    bool(
                        state["git_clean"]
                        and state["matching_release_tag"] == EXPECTED_TAG
                    ),
                    eligible,
                )

    def test_workflow_trigger_tag_must_match_the_project_version(self):
        project = release_verify._project(self.source)
        self.assertIsNone(release_verify._validate_trigger_tag(project, None))
        self.assertEqual(
            release_verify._validate_trigger_tag(project, EXPECTED_TAG),
            EXPECTED_TAG,
        )
        for trigger_tag in (WRONG_TAG, f"{EXPECTED_TAG}-other", ""):
            with self.subTest(trigger_tag=trigger_tag), self.assertRaises(
                release_verify.ReleaseVerificationError
            ) as raised:
                release_verify._validate_trigger_tag(project, trigger_tag)
            self.assertEqual(raised.exception.code, 4)

        output = self.root / "triggered-records"
        _, _, provenance = release_verify.verify_release(
            self.wheel(),
            self.sdist(),
            self.source,
            output,
            trigger_tag=EXPECTED_TAG,
        )
        self.assertEqual(
            provenance["source"]["workflow_trigger_tag"], EXPECTED_TAG
        )
        self.assertTrue(
            provenance["source"]["workflow_trigger_tag_matches_expected"]
        )

        wrong_output = self.root / "wrong-trigger-records"
        with self.assertRaises(release_verify.ReleaseVerificationError) as raised:
            release_verify.verify_release(
                self.wheel(),
                self.sdist(),
                self.source,
                wrong_output,
                trigger_tag=WRONG_TAG,
            )
        self.assertEqual(raised.exception.code, 4)
        self.assertFalse(wrong_output.exists())

    def test_release_fixtures_follow_project_version(self):
        self.assertIn(
            f"Version: {PROJECT_VERSION}\n".encode(),
            metadata(),
        )
        self.assertEqual(
            self.wheel().name,
            f"sasori-{PROJECT_VERSION}-py3-none-any.whl",
        )
        self.assertEqual(
            self.sdist().name,
            f"sasori-{PROJECT_VERSION}.tar.gz",
        )

    def test_ci_rebuilds_sdist_with_locked_inputs_before_consumer_smoke(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("name: sasori-sdist\n          path: dist/*.tar.gz", workflow)
        package = workflow.split("\n  package:\n", 1)[1].split(
            "\n  wheel-smoke:\n", 1
        )[0]
        self.assertEqual(package.count("python -m pip download"), 1)
        for required in (
            "export PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple",
            "python -m pip download",
            "--require-hashes",
            "--only-binary=:all:",
            "--no-deps",
            "--dest /out/build-wheelhouse",
            'test "${1%-py3-none-any.whl}" != "$1"',
            "python -m pip --isolated --no-cache-dir install",
            "python scripts/repack_wheel.py --wheel",
            "--no-index",
            "--find-links /out/build-wheelhouse",
            "name: sasori-build-wheelhouse-${{ github.sha }}",
            "path: dist/build-wheelhouse/*.whl",
            "retention-days: 1",
            "compression-level: 0",
        ):
            with self.subTest(package_contract=required):
                self.assertIn(required, package)
        lines = package.splitlines()
        heredoc_start = next(
            index for index, line in enumerate(lines) if "python - <<PY" in line
        )
        heredoc_end = next(
            index
            for index in range(heredoc_start + 1, len(lines))
            if lines[index].strip() == "PY"
        )
        yaml_indent = len(lines[heredoc_end]) - len(lines[heredoc_end].lstrip())
        body_lines = lines[heredoc_start + 1 : heredoc_end]
        self.assertTrue(body_lines)
        self.assertTrue(all(line.startswith(" " * yaml_indent) for line in body_lines))
        compile(
            "\n".join(line[yaml_indent:] for line in body_lines),
            "<ci-package-sdist-heredoc>",
            "exec",
        )
        self.assertLess(
            package.index("PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple"),
            package.index("python -m pip download"),
        )
        self.assertLess(
            package.index("python -m pip download"),
            package.index("python -m pip --isolated --no-cache-dir install"),
        )
        marker = "\n  sdist-smoke:\n"
        self.assertEqual(workflow.count(marker), 1)
        smoke = workflow.split(marker, 1)[1].split("\n  core-package-smoke:\n", 1)[0]
        for required in (
            'name: "Rebuilt sdist / ${{ matrix.os }} / Python ${{ matrix.python-version }}"',
            "name: sasori-sdist",
            'path: ${{ runner.temp }}/sasori-sdist',
            "name: sasori-build-wheelhouse-${{ github.sha }}",
            'path: ${{ runner.temp }}/sasori-build-wheelhouse',
            '$sdistRoot = Join-Path $env:RUNNER_TEMP "sasori-sdist"',
            '$wheelhouseRoot = Join-Path $env:RUNNER_TEMP "sasori-build-wheelhouse"',
            "python scripts/sdist_consumer_smoke.py",
            "--sdist $sdists[0].FullName",
            "--build-lock requirements-build.txt",
            "--build-wheelhouse $wheelhouseRoot",
            "--consumer-check scripts/installed_wheel_smoke.py",
            "--wheel-repacker scripts/repack_wheel.py",
            "--release-verifier scripts/release_verify.py",
            "--source-root .",
        ):
            with self.subTest(required=required):
                self.assertIn(required, smoke)
        for value in ("ubuntu-24.04", "windows-2025", '"3.11"', '"3.12"', '"3.13"'):
            with self.subTest(matrix_value=value):
                self.assertEqual(smoke.count(value), 1)
        self.assertNotIn("pypi", smoke.lower())
        self.assertNotIn("tuna", smoke.lower())

    def test_ci_rebuilds_and_smokes_sasori_core_as_a_standalone_distribution(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8")
        marker = "\n  core-package-smoke:\n"
        self.assertEqual(workflow.count(marker), 1)
        smoke = workflow.split(marker, 1)[1].split("\n  release-bundle:\n", 1)[0]
        for required in (
            'name: "sasori-core wheel + rebuilt sdist / ${{ matrix.os }} / Python ${{ matrix.python-version }}"',
            "pattern: sasori-core-*",
            "packages/sasori-core/scripts/release_verify.py",
            "packages/sasori-core/scripts/installed_smoke.py",
            "--no-index --no-deps $wheels[0].FullName",
            "--no-index --no-build-isolation --no-deps",
            "--no-index --no-deps $rebuilt[0].FullName",
        ):
            with self.subTest(required=required):
                self.assertIn(required, smoke)
        for value in ("ubuntu-24.04", "windows-2025", '"3.11"', '"3.12"', '"3.13"'):
            with self.subTest(matrix_value=value):
                self.assertEqual(smoke.count(value), 1)

    def test_spdx_rejects_duplicate_checksum_algorithms(self):
        manifest, spdx, _ = release_verify.verify_release(
            self.wheel(), self.sdist(), self.source, self.root / "records"
        )
        package = next(
            item
            for item in spdx["packages"]
            if item["SPDXID"] == "SPDXRef-Package-sasori-wheel"
        )
        package["checksums"].append(dict(package["checksums"][0]))
        with self.assertRaises(release_verify.ReleaseVerificationError):
            release_verify._validate_spdx(spdx, manifest["artifacts"])


if __name__ == "__main__":
    unittest.main()
