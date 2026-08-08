from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import release_bundle  # noqa: E402


with (ROOT / "pyproject.toml").open("rb") as stream:
    PROJECT_VERSION = tomllib.load(stream)["project"]["version"]

EXPECTED_TAG = f"v{PROJECT_VERSION}"
WRONG_TAG = f"v{PROJECT_VERSION}.wrong"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ReleaseBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.metadata = self.root / "metadata"
        self.source.mkdir()
        self.metadata.mkdir()
        shutil.copyfile(ROOT / "pyproject.toml", self.source / "pyproject.toml")
        for relative in release_bundle.NOTICE_FILES:
            destination = self.source / Path(relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / Path(relative), destination)
        self.wheel = self.root / f"sasori-{PROJECT_VERSION}-py3-none-any.whl"
        self.sdist = self.root / f"sasori-{PROJECT_VERSION}.tar.gz"
        self.wheel.write_bytes(b"wheel candidate\n")
        self.sdist.write_bytes(b"source candidate\n")
        self.head = "a" * 40
        self.source_tree_sha256 = "c" * 64
        self.source_file_count = 42
        self.created_at = "2026-08-08T00:00:00Z"
        self.build_inputs = {
            "pyproject_toml_sha256": "d" * 64,
            "requirements_build_sha256": "e" * 64,
            "build_requirements": [
                {
                    "name": "setuptools",
                    "version": "80.9.0",
                    "sha256": "f" * 64,
                }
            ],
            "python_index_url": "https://pypi.tuna.tsinghua.edu.cn/simple",
            "container_base_image": (
                "docker.m.daocloud.io/library/python:3.12-slim@sha256:"
                + "1" * 64
            ),
        }
        self.current_source = {
            "state": "clean_release_tag_candidate",
            "git_clean": True,
            "head_baseline": self.head,
            "head_is_artifact_source_identity": True,
            "exact_tags": [EXPECTED_TAG],
            "expected_release_tag": EXPECTED_TAG,
            "matching_release_tag": EXPECTED_TAG,
            "tracked_changed_count": 0,
            "untracked_count": 0,
            "git_status_porcelain_sha256": hashlib.sha256(b"").hexdigest(),
        }
        source_patch = mock.patch.object(
            release_bundle.release_verify,
            "_source_state",
            return_value=self.current_source,
        )
        source_patch.start()
        self.addCleanup(source_patch.stop)
        tree_patch = mock.patch.object(
            release_bundle.release_verify,
            "_source_tree",
            return_value=(self.source_tree_sha256, self.source_file_count),
        )
        tree_patch.start()
        self.addCleanup(tree_patch.stop)
        build_patch = mock.patch.object(
            release_bundle.release_verify,
            "_build_inputs",
            return_value=self.build_inputs,
        )
        build_patch.start()
        self.addCleanup(build_patch.stop)
        self._write_metadata()

    def _write_json(self, path: Path, value: dict[str, object]) -> None:
        path.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _artifacts(self) -> list[dict[str, object]]:
        return [
            {
                "kind": "wheel",
                "filename": self.wheel.name,
                "size": self.wheel.stat().st_size,
                "sha256": sha256(self.wheel),
                "member_inventory_sha256": "2" * 64,
                "regular_file_count": 10,
                "archive_member_count": 10,
            },
            {
                "kind": "sdist",
                "filename": self.sdist.name,
                "size": self.sdist.stat().st_size,
                "sha256": sha256(self.sdist),
                "member_inventory_sha256": "3" * 64,
                "regular_file_count": 12,
                "archive_member_count": 13,
            },
        ]

    def _write_metadata(self) -> None:
        artifacts = self._artifacts()
        project = {
            "name": "sasori",
            "version": PROJECT_VERSION,
            "requires_python": ">=3.11,<3.14",
            "license_expression": "MIT",
        }
        manifest = {
            "schema_version": 1,
            "kind": "sasori.artifact-manifest",
            "project": {**project, "runtime_dependencies": []},
            "artifacts": artifacts,
            "build_inputs": self.build_inputs,
            "verification": {
                "release_eligible": True,
                "verified_at_utc": self.created_at,
                "verifier": {
                    "name": "sasori-release-verify",
                    "version": release_bundle.release_verify.VERIFIER_VERSION,
                },
            },
        }
        spdx = release_bundle.release_verify._spdx(
            project, artifacts, self.build_inputs, self.created_at
        )
        provenance = {
            "schema_version": 1,
            "kind": "sasori.local-provenance",
            "release_eligible": True,
            "reason": None,
            "signed": False,
            "claim": release_bundle.LOCAL_PROVENANCE_CLAIM,
            "source": {
                "state": "clean_release_tag_candidate",
                "git_clean": True,
                "head_baseline": self.head,
                "head_is_artifact_source_identity": True,
                "exact_tags": [EXPECTED_TAG],
                "expected_release_tag": EXPECTED_TAG,
                "matching_release_tag": EXPECTED_TAG,
                "tracked_changed_count": 0,
                "untracked_count": 0,
                "git_status_porcelain_sha256": hashlib.sha256(b"").hexdigest(),
                "artifact_source_binding": "verified_to_current_working_tree",
                "workflow_trigger_tag": EXPECTED_TAG,
                "workflow_trigger_tag_matches_expected": True,
                "included_source_tree_algorithm": "sasori-source-tree-v1",
                "included_source_tree_sha256": self.source_tree_sha256,
                "included_source_file_count": self.source_file_count,
            },
            "builder": {
                "python_implementation": "CPython",
                "python_version": "3.12.0",
                "platform": "test-platform",
                "declared_release_configuration": self.build_inputs,
            },
            "artifacts": [
                {
                    "filename": artifact["filename"],
                    "sha256": artifact["sha256"],
                }
                for artifact in artifacts
            ],
            "created_at_utc": self.created_at,
        }
        self._write_json(self.metadata / "artifact-manifest.json", manifest)
        self._write_json(
            self.metadata / f"sasori-{PROJECT_VERSION}.spdx.json", spdx
        )
        self._write_json(self.metadata / "provenance.local.json", provenance)

    def _assemble(self, name: str) -> Path:
        output = self.root / name
        result = release_bundle.assemble_bundle(
            wheel=self.wheel,
            sdist=self.sdist,
            metadata_root=self.metadata,
            source_root=self.source,
            output_root=output,
            trigger_tag=EXPECTED_TAG,
        )
        self.assertEqual(
            result,
            {
                "files": 8,
                "trigger_tag": EXPECTED_TAG,
                "version": PROJECT_VERSION,
            },
        )
        return output

    def _read_json(self, path: Path) -> dict[str, object]:
        return json.loads(path.read_text(encoding="utf-8"))

    def test_assembles_and_verifies_the_exact_eight_file_candidate(self) -> None:
        output = self._assemble("candidate")
        self.assertEqual(
            sorted(
                path.relative_to(output).as_posix()
                for path in output.rglob("*")
                if path.is_file()
            ),
            sorted(release_bundle.expected_inventory(PROJECT_VERSION)),
        )
        self.assertEqual(
            release_bundle.verify_bundle(output, self.source, EXPECTED_TAG)["files"],
            8,
        )

    def test_wrong_trigger_and_nonempty_output_fail_before_bundle_creation(self) -> None:
        wrong_output = self.root / "wrong-tag"
        with self.assertRaises(release_bundle.ReleaseBundleError):
            release_bundle.assemble_bundle(
                wheel=self.wheel,
                sdist=self.sdist,
                metadata_root=self.metadata,
                source_root=self.source,
                output_root=wrong_output,
                trigger_tag=WRONG_TAG,
            )
        self.assertFalse(wrong_output.exists())

        candidate = self._assemble("wrong-verify-tag")
        with self.assertRaises(release_bundle.ReleaseBundleError):
            release_bundle.verify_bundle(candidate, self.source, WRONG_TAG)

        with mock.patch.object(
            release_bundle.release_verify,
            "_source_state",
            return_value={
                "state": "clean_untagged_local_candidate",
                "git_clean": True,
                "head_baseline": self.head,
                "matching_release_tag": None,
            },
        ), self.assertRaises(release_bundle.ReleaseBundleError):
            release_bundle.verify_bundle(candidate, self.source, EXPECTED_TAG)

        nonempty = self.root / "nonempty"
        nonempty.mkdir()
        (nonempty / "stale").write_text("stale", encoding="utf-8")
        with self.assertRaises(release_bundle.ReleaseBundleError):
            release_bundle.assemble_bundle(
                wheel=self.wheel,
                sdist=self.sdist,
                metadata_root=self.metadata,
                source_root=self.source,
                output_root=nonempty,
                trigger_tag=EXPECTED_TAG,
            )

    def test_missing_extra_symlink_and_tampered_artifact_fail_closed(self) -> None:
        for index, relative in enumerate(
            release_bundle.expected_inventory(PROJECT_VERSION)
        ):
            with self.subTest(boundary="missing", relative=relative):
                output = self._assemble(f"missing-{index}")
                (output / Path(relative)).unlink()
                with self.assertRaises(release_bundle.ReleaseBundleError):
                    release_bundle.verify_bundle(output, self.source, EXPECTED_TAG)

        extra = self._assemble("extra")
        (extra / "unexpected.txt").write_text("extra", encoding="utf-8")
        with self.assertRaises(release_bundle.ReleaseBundleError):
            release_bundle.verify_bundle(extra, self.source, EXPECTED_TAG)

        extra_directory = self._assemble("extra-directory")
        (extra_directory / "unexpected").mkdir()
        with self.assertRaises(release_bundle.ReleaseBundleError):
            release_bundle.verify_bundle(
                extra_directory, self.source, EXPECTED_TAG
            )

        tampered = self._assemble("tampered-wheel")
        (tampered / self.wheel.name).write_bytes(b"tampered")
        with self.assertRaises(release_bundle.ReleaseBundleError):
            release_bundle.verify_bundle(tampered, self.source, EXPECTED_TAG)

        symlink = self._assemble("symlink")
        member = symlink / self.wheel.name
        original_is_symlink = Path.is_symlink

        def pretend_member_is_symlink(path: Path) -> bool:
            return path == member or original_is_symlink(path)

        with mock.patch.object(Path, "is_symlink", pretend_member_is_symlink):
            with self.assertRaises(release_bundle.ReleaseBundleError):
                release_bundle.verify_bundle(symlink, self.source, EXPECTED_TAG)

    def test_spdx_provenance_and_notices_are_bound_to_verified_subjects(self) -> None:
        spdx_bundle = self._assemble("spdx")
        spdx_path = spdx_bundle / f"sasori-{PROJECT_VERSION}.spdx.json"
        spdx = self._read_json(spdx_path)
        wheel_package = next(
            package
            for package in spdx["packages"]
            if package["SPDXID"] == "SPDXRef-Package-sasori-wheel"
        )
        wheel_package["checksums"][0]["checksumValue"] = "0" * 64
        self._write_json(spdx_path, spdx)
        with self.assertRaises(release_bundle.ReleaseBundleError):
            release_bundle.verify_bundle(spdx_bundle, self.source, EXPECTED_TAG)

        for index, field in enumerate(
            (
                "signed",
                "claim",
                "artifacts",
                "head_baseline",
                "source_tree",
                "reason",
            )
        ):
            with self.subTest(boundary="provenance", field=field):
                bundle = self._assemble(f"provenance-{index}")
                path = bundle / "provenance.local.json"
                provenance = self._read_json(path)
                if field == "signed":
                    provenance[field] = True
                elif field == "claim":
                    provenance[field] = "trusted"
                elif field == "artifacts":
                    provenance[field][0]["sha256"] = "f" * 64
                elif field == "head_baseline":
                    provenance["source"][field] = "b" * 40
                elif field == "source_tree":
                    provenance["source"]["included_source_tree_sha256"] = "d" * 64
                else:
                    provenance[field] = "clean_release_tag_candidate"
                self._write_json(path, provenance)
                with self.assertRaises(release_bundle.ReleaseBundleError):
                    release_bundle.verify_bundle(bundle, self.source, EXPECTED_TAG)

        notice_bundle = self._assemble("notice")
        (notice_bundle / "LICENSE").write_text("changed", encoding="utf-8")
        with self.assertRaises(release_bundle.ReleaseBundleError):
            release_bundle.verify_bundle(notice_bundle, self.source, EXPECTED_TAG)

    def test_schema_v1_rejects_unknown_trust_claims_and_extra_spdx_subjects(
        self,
    ) -> None:
        provenance_bundle = self._assemble("unknown-provenance-trust")
        provenance_path = provenance_bundle / "provenance.local.json"
        provenance = self._read_json(provenance_path)
        provenance["trusted"] = True
        provenance["attestation"] = {"kind": "slsa", "trusted": True}
        self._write_json(provenance_path, provenance)
        with self.assertRaises(release_bundle.ReleaseBundleError):
            release_bundle.verify_bundle(
                provenance_bundle, self.source, EXPECTED_TAG
            )

        manifest_bundle = self._assemble("unknown-manifest-trust")
        manifest_path = manifest_bundle / "artifact-manifest.json"
        manifest = self._read_json(manifest_path)
        manifest["verification"]["trusted"] = True
        self._write_json(manifest_path, manifest)
        with self.assertRaises(release_bundle.ReleaseBundleError):
            release_bundle.verify_bundle(manifest_bundle, self.source, EXPECTED_TAG)

        spdx_bundle = self._assemble("extra-spdx-subject")
        spdx_path = spdx_bundle / f"sasori-{PROJECT_VERSION}.spdx.json"
        spdx = self._read_json(spdx_path)
        spdx["packages"].append(
            {
                "name": "unexpected.tar.gz",
                "SPDXID": "SPDXRef-Package-unexpected-archive",
                "versionInfo": PROJECT_VERSION,
                "downloadLocation": "NOASSERTION",
                "filesAnalyzed": False,
                "licenseConcluded": "MIT",
                "licenseDeclared": "MIT",
                "copyrightText": "NOASSERTION",
                "primaryPackagePurpose": "ARCHIVE",
                "checksums": [
                    {"algorithm": "SHA256", "checksumValue": "9" * 64}
                ],
            }
        )
        self._write_json(spdx_path, spdx)
        with self.assertRaises(release_bundle.ReleaseBundleError):
            release_bundle.verify_bundle(spdx_bundle, self.source, EXPECTED_TAG)

    def test_schema_v1_rejects_nested_extensions_and_metadata_drift(self) -> None:
        mutations = (
            (
                "source-extension",
                "provenance.local.json",
                lambda value: value["source"].update({"trusted": True}),
            ),
            (
                "builder-extension",
                "provenance.local.json",
                lambda value: value["builder"].update({"attested": True}),
            ),
            (
                "artifact-extension",
                "artifact-manifest.json",
                lambda value: value["artifacts"][0].update(
                    {"release_eligible": True}
                ),
            ),
            (
                "build-input-drift",
                "artifact-manifest.json",
                lambda value: value["build_inputs"].update(
                    {"python_index_url": "https://example.invalid/simple"}
                ),
            ),
            (
                "artifact-size-drift",
                "artifact-manifest.json",
                lambda value: value["artifacts"][0].update(
                    {"size": value["artifacts"][0]["size"] + 1}
                ),
            ),
            (
                "timestamp-drift",
                "provenance.local.json",
                lambda value: value.update(
                    {"created_at_utc": "2026-08-08T00:00:01Z"}
                ),
            ),
            (
                "invalid-timestamp",
                "artifact-manifest.json",
                lambda value: value["verification"].update(
                    {"verified_at_utc": "2026-08-08T00:00:00+00:00"}
                ),
            ),
        )
        for name, relative, mutate in mutations:
            with self.subTest(boundary=name):
                bundle = self._assemble(name)
                path = bundle / relative
                value = self._read_json(path)
                mutate(value)
                self._write_json(path, value)
                with self.assertRaises(release_bundle.ReleaseBundleError):
                    release_bundle.verify_bundle(
                        bundle, self.source, EXPECTED_TAG
                    )

    def test_bundle_metadata_json_is_strict_and_bounded(self) -> None:
        invalid_payloads = (
            '{"kind":"first","kind":"second"}',
            '{"value":NaN}',
            " " * (release_bundle.MAX_JSON_BYTES + 1),
        )
        for index, payload in enumerate(invalid_payloads):
            with self.subTest(index=index):
                bundle = self._assemble(f"invalid-json-{index}")
                (bundle / "artifact-manifest.json").write_text(
                    payload, encoding="utf-8"
                )
                with self.assertRaises(release_bundle.ReleaseBundleError):
                    release_bundle.verify_bundle(bundle, self.source, EXPECTED_TAG)

    def test_workflow_binds_tag_and_waits_for_every_release_gate(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("startsWith(github.ref, 'refs/tags/v')", workflow)
        self.assertIn("--trigger-tag \"$SASORI_TRIGGER_TAG\"", workflow)
        self.assertIn("SASORI_TRIGGER_TAG: ${{ github.ref_name }}", workflow)
        self.assertIn("name: sasori-release-candidate-${{ github.sha }}", workflow)
        marker = "\n  release-bundle:\n"
        self.assertEqual(workflow.count(marker), 1)
        bundle = workflow.split(marker, 1)[1]
        for job in ("test", "package", "container", "wheel-smoke", "sdist-smoke"):
            with self.subTest(required_job=job):
                self.assertIn(f"      - {job}\n", bundle)
        for required in (
            "if: ${{ github.ref_type == 'tag' }}",
            "name: sasori-release-candidate-${{ github.sha }}",
            "python scripts/release_bundle.py verify",
            "--trigger-tag \"$SASORI_TRIGGER_TAG\"",
            "name: sasori-${{ needs.package.outputs.version }}-release-bundle",
        ):
            with self.subTest(required=required):
                self.assertIn(required, bundle)


if __name__ == "__main__":
    unittest.main()
