import base64
import csv
import hashlib
import importlib.util
import io
import json
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


def metadata(*, dependency=False, extra=""):
    requires = "Requires-Dist: unwanted>=1\n" if dependency else ""
    return (
        "Metadata-Version: 2.4\n"
        "Name: sasori\n"
        f"Version: {PROJECT_VERSION}\n"
        "Requires-Python: >=3.11,<3.14\n"
        "License-Expression: MIT\n"
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


class ReleaseVerificationTests(unittest.TestCase):
    def test_release_contract_version_tracks_context_and_asset_inventory(self):
        self.assertEqual(release_verify.VERIFIER_VERSION, "5")
        self.assertEqual(
            release_verify.SOURCE_TREE_ALGORITHM, "sasori-source-tree-v3"
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

    def wheel(
        self,
        *,
        dependency=False,
        corrupt_record=False,
        metadata_extra="",
        wheel_extra="",
        extra_dist_info=False,
    ):
        path = self.root / f"sasori-{PROJECT_VERSION}-py3-none-any.whl"
        dist_info = f"sasori-{PROJECT_VERSION}.dist-info"
        files = release_verify._source_payload(self.source)
        files.update(
            {
                f"{dist_info}/METADATA": metadata(
                    dependency=dependency, extra=metadata_extra
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
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, value in files.items():
                archive.writestr(name, value)
        return path

    def sdist(
        self, *, traversal=False, extra_directory=False, valid_directories=False
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
        files[f"{root}/PKG-INFO"] = metadata()
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
        self.assertEqual(manifest["project"]["runtime_dependencies"], [])
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
            "--no-index",
            "--find-links /out/build-wheelhouse",
            "name: sasori-build-wheelhouse-${{ github.sha }}",
            "path: dist/build-wheelhouse/*.whl",
            "retention-days: 1",
            "compression-level: 0",
        ):
            with self.subTest(package_contract=required):
                self.assertIn(required, package)
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
        smoke = workflow.split(marker, 1)[1].split("\n  release-bundle:\n", 1)[0]
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
