import copy
import importlib.util
import json
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "sasori_testpypi_roundtrip", ROOT / "scripts" / "testpypi_roundtrip.py"
)
testpypi = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = testpypi
SPEC.loader.exec_module(testpypi)

VERSION = "0.1.0.dev0"
COMMIT = "a" * 40


class TestPyPIReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _write_project(self, *, name="sasori", version=VERSION):
        source = self.root / "source"
        source.mkdir(exist_ok=True)
        (source / "pyproject.toml").write_text(
            f'[project]\nname = "{name}"\nversion = "{version}"\n',
            encoding="utf-8",
        )
        return source

    def _write_distributions(self, directory=None, *, version=VERSION):
        directory = directory or self.root / "dist"
        directory.mkdir(parents=True, exist_ok=True)
        filenames = testpypi.expected_filenames(version)
        (directory / filenames["bdist_wheel"]).write_bytes(b"exact wheel bytes")
        (directory / filenames["sdist"]).write_bytes(b"exact sdist bytes")
        return directory

    def _inventory(self, directory=None):
        directory = directory or self._write_distributions()
        return testpypi.artifact_inventory(directory, VERSION)

    def _release_payload(self, inventory=None):
        inventory = inventory or self._inventory()
        files = []
        for kind, item in inventory.items():
            files.append(
                {
                    "packagetype": kind,
                    "filename": item["filename"],
                    "size": item["size"],
                    "yanked": False,
                    "digests": {"sha256": item["sha256"]},
                }
            )
        return {
            "info": {"name": "sasori"},
            "releases": {VERSION: files},
        }

    def _ci_payload(self):
        return {
            "workflow_runs": [
                {
                    "id": 123,
                    "run_number": 38,
                    "html_url": "https://github.com/syusama/sasori/actions/runs/123",
                    "head_sha": COMMIT,
                    "head_branch": "main",
                    "event": "push",
                    "status": "completed",
                    "conclusion": "success",
                    "path": ".github/workflows/ci.yml",
                }
            ]
        }

    def _write_metadata(self, directory, inventory, *, artifacts=None, provenance=None):
        directory.mkdir(parents=True, exist_ok=True)
        manifest = {
            "project": {"name": "sasori", "version": VERSION},
            "artifacts": artifacts
            or [
                inventory[kind]
                | {"kind": testpypi.MANIFEST_KIND_BY_PACKAGE_TYPE[kind]}
                for kind in sorted(inventory)
            ],
        }
        provenance = provenance or {
            "release_eligible": False,
            "reason": testpypi.UNTAGGED_SOURCE_STATE,
            "source": {
                "state": testpypi.UNTAGGED_SOURCE_STATE,
                "head_baseline": COMMIT,
                "head_is_artifact_source_identity": True,
                "git_clean": True,
                "exact_tags": [],
            },
        }
        (directory / "artifact-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        (directory / "provenance.local.json").write_text(
            json.dumps(provenance), encoding="utf-8"
        )

    def test_project_name_and_version_are_exact(self):
        source = self._write_project()
        self.assertEqual(testpypi.project_version(source), VERSION)
        for name, version in (("other", VERSION), ("sasori", "bad/version")):
            with self.subTest(name=name, version=version):
                (source / "pyproject.toml").write_text(
                    f'[project]\nname = "{name}"\nversion = "{version}"\n',
                    encoding="utf-8",
                )
                with self.assertRaises(testpypi.TestPyPIGateError):
                    testpypi.project_version(source)

    def test_artifact_inventory_accepts_exact_wheel_and_sdist(self):
        inventory = self._inventory()
        self.assertEqual(set(inventory), {"bdist_wheel", "sdist"})
        self.assertEqual(inventory["bdist_wheel"]["size"], 17)
        self.assertRegex(inventory["sdist"]["sha256"], r"^[0-9a-f]{64}$")

    def test_artifact_inventory_rejects_missing_extra_and_wrong_names(self):
        for mutation in ("missing", "extra", "wrong"):
            with self.subTest(mutation=mutation):
                directory = self.root / mutation
                self._write_distributions(directory)
                wheel = directory / testpypi.expected_filenames(VERSION)["bdist_wheel"]
                if mutation == "missing":
                    wheel.unlink()
                elif mutation == "extra":
                    (directory / "unexpected.txt").write_text("x", encoding="utf-8")
                else:
                    wheel.rename(directory / "sasori-wrong.whl")
                with self.assertRaisesRegex(
                    testpypi.TestPyPIGateError, "inventory is not exact"
                ):
                    testpypi.artifact_inventory(directory, VERSION)

    def test_artifact_inventory_rejects_a_symlinked_distribution(self):
        directory = self._write_distributions()
        wheel_name = testpypi.expected_filenames(VERSION)["bdist_wheel"]
        original = Path.is_symlink

        def marked_symlink(path):
            return path.name == wheel_name or original(path)

        with mock.patch.object(Path, "is_symlink", marked_symlink):
            with self.assertRaisesRegex(
                testpypi.TestPyPIGateError, "must be one regular file"
            ):
                testpypi.artifact_inventory(directory, VERSION)

    def test_source_identity_requires_exact_clean_untagged_commit(self):
        values = {
            ("rev-parse", "HEAD"): COMMIT,
            ("status", "--porcelain=v1", "--untracked-files=all"): "",
            ("tag", "--points-at", "HEAD"): "",
        }
        with mock.patch.object(
            testpypi, "_git", side_effect=lambda _, *args: values[args]
        ):
            self.assertEqual(
                testpypi.source_identity(self.root, COMMIT),
                {"commit_sha": COMMIT, "git_clean": True, "tags_at_head": []},
            )

        for field, changed, message in (
            (("rev-parse", "HEAD"), "b" * 40, "does not match"),
            (
                ("status", "--porcelain=v1", "--untracked-files=all"),
                "?? x",
                "not clean",
            ),
            (("tag", "--points-at", "HEAD"), "v0.1.0.dev0", "before any tag"),
        ):
            with self.subTest(field=field):
                bad = values | {field: changed}
                with mock.patch.object(
                    testpypi, "_git", side_effect=lambda _, *args: bad[args]
                ):
                    with self.assertRaisesRegex(testpypi.TestPyPIGateError, message):
                        testpypi.source_identity(self.root, COMMIT)

        with self.assertRaisesRegex(testpypi.TestPyPIGateError, "40 lowercase"):
            testpypi.source_identity(self.root, "A" * 40)

    def test_successful_ci_run_binds_one_exact_main_push(self):
        self.assertEqual(
            testpypi.successful_ci_run(self._ci_payload(), COMMIT),
            {
                "run_id": 123,
                "run_number": 38,
                "url": "https://github.com/syusama/sasori/actions/runs/123",
            },
        )
        duplicate = self._ci_payload()
        duplicate["workflow_runs"].append(copy.deepcopy(duplicate["workflow_runs"][0]))
        with self.assertRaisesRegex(testpypi.TestPyPIGateError, "one successful"):
            testpypi.successful_ci_run(duplicate, COMMIT)

        for field, value in (
            ("head_sha", "b" * 40),
            ("head_branch", "feature"),
            ("event", "workflow_dispatch"),
            ("status", "in_progress"),
            ("conclusion", "failure"),
            ("path", ".github/workflows/testpypi.yml"),
        ):
            with self.subTest(field=field):
                payload = self._ci_payload()
                payload["workflow_runs"][0][field] = value
                with self.assertRaises(testpypi.TestPyPIGateError):
                    testpypi.successful_ci_run(payload, COMMIT)

    def test_successful_ci_run_rejects_boolean_or_malformed_identity(self):
        for field, value in (
            ("id", True),
            ("run_number", False),
            ("html_url", "http://example.invalid/run"),
        ):
            with self.subTest(field=field):
                payload = self._ci_payload()
                payload["workflow_runs"][0][field] = value
                with self.assertRaisesRegex(testpypi.TestPyPIGateError, "identity"):
                    testpypi.successful_ci_run(payload, COMMIT)

    def test_require_ci_success_uses_the_exact_workflow_query(self):
        with mock.patch.object(
            testpypi, "_read_json_url", return_value=self._ci_payload()
        ) as read:
            testpypi.require_ci_success("syusama/sasori", COMMIT, "ephemeral")
        url = read.call_args.args[0]
        self.assertIn("/actions/workflows/ci.yml/runs?", url)
        self.assertIn(f"head_sha={COMMIT}", url)
        self.assertIn("branch=main", url)
        self.assertIn("event=push", url)
        self.assertEqual(read.call_args.kwargs["token"], "ephemeral")

    def test_version_absence_rejects_even_an_empty_existing_release(self):
        testpypi.require_version_absent(None, VERSION)
        testpypi.require_version_absent(
            {"info": {"name": "sasori"}, "releases": {}}, VERSION
        )
        for existing in ([], [{"filename": "deleted-or-existing"}]):
            with self.subTest(existing=existing):
                with self.assertRaisesRegex(
                    testpypi.TestPyPIGateError, "already exists"
                ):
                    testpypi.require_version_absent(
                        {
                            "info": {"name": "sasori"},
                            "releases": {VERSION: existing},
                        },
                        VERSION,
                    )

    def test_testpypi_release_accepts_exact_two_file_json(self):
        inventory = self._inventory()
        files = testpypi.testpypi_release(
            self._release_payload(inventory), VERSION, inventory
        )
        self.assertEqual([item["kind"] for item in files], ["bdist_wheel", "sdist"])
        self.assertTrue(all(item["yanked"] is False for item in files))

    def test_testpypi_release_rejects_every_file_identity_mismatch(self):
        inventory = self._inventory()
        base = self._release_payload(inventory)
        cases = (
            ("filename", "wrong.whl"),
            ("size", 999),
            ("yanked", True),
            ("packagetype", "other"),
        )
        for field, value in cases:
            with self.subTest(field=field):
                payload = copy.deepcopy(base)
                payload["releases"][VERSION][0][field] = value
                with self.assertRaises(testpypi.TestPyPIGateError):
                    testpypi.testpypi_release(payload, VERSION, inventory)
        payload = copy.deepcopy(base)
        payload["releases"][VERSION][0]["digests"]["sha256"] = "0" * 64
        with self.assertRaises(testpypi.TestPyPIGateError):
            testpypi.testpypi_release(payload, VERSION, inventory)
        payload = copy.deepcopy(base)
        payload["releases"][VERSION].append(
            copy.deepcopy(payload["releases"][VERSION][0])
        )
        with self.assertRaisesRegex(
            testpypi.TestPyPIGateError, "inventory is not exact"
        ):
            testpypi.testpypi_release(payload, VERSION, inventory)

    def test_polling_retries_only_within_the_declared_bounds(self):
        inventory = self._inventory()
        responses = iter((None, self._release_payload(inventory)))
        sleeps = []
        result = testpypi.wait_for_testpypi(
            VERSION,
            inventory,
            attempts=2,
            delay_seconds=0.25,
            fetch=lambda: next(responses),
            sleep=sleeps.append,
        )
        self.assertEqual(len(result), 2)
        self.assertEqual(sleeps, [0.25])

        with self.assertRaisesRegex(testpypi.TestPyPIGateError, "did not converge"):
            testpypi.wait_for_testpypi(
                VERSION,
                inventory,
                attempts=2,
                delay_seconds=0,
                fetch=lambda: None,
                sleep=lambda _: None,
            )
        for attempts, delay in ((0, 0), (61, 0), (1, -1), (1, 61)):
            with self.subTest(attempts=attempts, delay=delay):
                with self.assertRaisesRegex(testpypi.TestPyPIGateError, "bounds"):
                    testpypi.wait_for_testpypi(
                        VERSION,
                        inventory,
                        attempts=attempts,
                        delay_seconds=delay,
                    )

    def test_download_inventory_and_smoke_markers_are_exact(self):
        wheel = self.root / "wheel-download"
        sdist = self.root / "sdist-download"
        self._write_distributions(wheel)
        self._write_distributions(sdist)
        (wheel / testpypi.expected_filenames(VERSION)["sdist"]).unlink()
        (sdist / testpypi.expected_filenames(VERSION)["bdist_wheel"]).unlink()
        inventory = testpypi._download_inventory(wheel, sdist, VERSION)
        self.assertEqual(set(inventory), {"bdist_wheel", "sdist"})

        marker = self.root / "marker"
        marker.write_bytes(b"PASS\n")
        testpypi._require_pass_marker(marker, "smoke")
        marker.write_bytes(b"PASS\r\n")
        with self.assertRaisesRegex(testpypi.TestPyPIGateError, "invalid"):
            testpypi._require_pass_marker(marker, "smoke")

    def test_metadata_binds_release_manifest_kinds_to_testpypi_inventory(self):
        inventory = self._inventory()
        metadata = self.root / "metadata"
        self._write_metadata(metadata, inventory)
        result = testpypi._metadata_inventory(metadata, VERSION, COMMIT, inventory)
        self.assertEqual(result["source_state"], "clean_untagged_local_candidate")
        self.assertFalse(result["release_eligible"])

        duplicated = [
            inventory["bdist_wheel"] | {"kind": "wheel"},
            inventory["bdist_wheel"] | {"kind": "wheel"},
            inventory["sdist"] | {"kind": "sdist"},
        ]
        self._write_metadata(metadata, inventory, artifacts=duplicated)
        with self.assertRaisesRegex(testpypi.TestPyPIGateError, "kinds are not exact"):
            testpypi._metadata_inventory(metadata, VERSION, COMMIT, inventory)

        package_types = [
            inventory[kind] | {"kind": kind} for kind in sorted(inventory)
        ]
        self._write_metadata(metadata, inventory, artifacts=package_types)
        with self.assertRaisesRegex(testpypi.TestPyPIGateError, "kinds are not exact"):
            testpypi._metadata_inventory(metadata, VERSION, COMMIT, inventory)

    def test_index_evidence_requires_its_exact_schema_and_regular_items(self):
        inventory = self._inventory()
        files = testpypi.testpypi_release(
            self._release_payload(inventory), VERSION, inventory
        )
        evidence = {
            "schema_version": 1,
            "kind": "sasori.testpypi-index",
            "project": "sasori",
            "version": VERSION,
            "index": testpypi.TESTPYPI_JSON_URL,
            "files": files,
        }
        payload = testpypi._release_payload_from_index_evidence(evidence, VERSION)
        testpypi.testpypi_release(payload, VERSION, inventory)
        for field, value in (
            ("schema_version", 2),
            ("kind", "wrong"),
            ("project", "other"),
            ("version", "0"),
            ("index", "https://example.invalid/"),
            ("files", [None]),
        ):
            with self.subTest(field=field):
                changed = copy.deepcopy(evidence)
                changed[field] = value
                with self.assertRaisesRegex(testpypi.TestPyPIGateError, "evidence"):
                    testpypi._release_payload_from_index_evidence(changed, VERSION)

    def test_finalize_writes_evidence_only_after_all_exact_checks(self):
        source = self._write_project()
        uploaded_dir = self._write_distributions(self.root / "uploaded")
        inventory = self._inventory(uploaded_dir)
        wheel = self.root / "wheel-download"
        sdist = self.root / "sdist-download"
        self._write_distributions(wheel)
        self._write_distributions(sdist)
        (wheel / testpypi.expected_filenames(VERSION)["sdist"]).unlink()
        (sdist / testpypi.expected_filenames(VERSION)["bdist_wheel"]).unlink()
        files = testpypi.testpypi_release(
            self._release_payload(inventory), VERSION, inventory
        )
        index = self.root / "index.json"
        index.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "sasori.testpypi-index",
                    "project": "sasori",
                    "version": VERSION,
                    "index": testpypi.TESTPYPI_JSON_URL,
                    "files": files,
                }
            ),
            encoding="utf-8",
        )
        wheel_marker = self.root / "wheel-pass"
        sdist_marker = self.root / "sdist-pass"
        wheel_marker.write_bytes(b"PASS\n")
        sdist_marker.write_bytes(b"PASS\n")
        output = self.root / "roundtrip.json"
        options = Namespace(
            source_root=source,
            upload_dir=uploaded_dir,
            wheel_download_dir=wheel,
            sdist_download_dir=sdist,
            expected_commit=COMMIT,
            expected_version=VERSION,
            index_evidence=index,
            wheel_smoke_marker=wheel_marker,
            sdist_smoke_marker=sdist_marker,
            output=output,
        )
        with mock.patch.object(
            testpypi,
            "source_identity",
            return_value={"commit_sha": COMMIT, "git_clean": True, "tags_at_head": []},
        ):
            testpypi.command_finalize(options)
        result = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(result["kind"], "sasori.testpypi-roundtrip")
        self.assertTrue(all(result["checks"].values()))
        self.assertEqual(result["source"]["commit_sha"], COMMIT)

    def test_manual_workflow_is_oidc_only_and_fail_closed(self):
        workflow = (ROOT / ".github" / "workflows" / "testpypi.yml").read_text(
            encoding="utf-8"
        )
        trigger = workflow.split("\npermissions:", 1)[0]
        self.assertIn("\n  workflow_dispatch:\n", trigger)
        self.assertNotIn("\n  push:", trigger)
        self.assertNotIn("\n  pull_request:", trigger)
        self.assertRegex(
            trigger,
            r"(?s)inputs:\s+commit:.*?required: true.*?version:.*?required: true",
        )
        self.assertIn("if: ${{ github.ref == 'refs/heads/main' }}", workflow)
        self.assertIn('test "$REQUESTED_COMMIT" = "$GITHUB_SHA"', workflow)
        self.assertIn('test -z "$(git tag --points-at HEAD)"', workflow)
        self.assertEqual(workflow.count("id-token: write"), 1)
        self.assertNotIn("password:", workflow)
        self.assertNotIn("TWINE_PASSWORD", workflow)
        self.assertNotIn("TESTPYPI_TOKEN", workflow)
        self.assertNotIn("secrets.", workflow)
        self.assertRegex(workflow, r"environment:\s+name: testpypi")
        self.assertIn("group: testpypi-sasori", workflow)
        self.assertIn("cancel-in-progress: false", workflow)

        uses = [
            line.strip().split("@", 1)[1].split()[0]
            for line in workflow.splitlines()
            if line.strip().startswith("uses:")
        ]
        self.assertTrue(uses)
        self.assertTrue(all(len(revision) == 40 for revision in uses))
        self.assertIn(
            "pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33",
            workflow,
        )
        self.assertIn("repository-url: https://test.pypi.org/legacy/", workflow)
        self.assertIn('skip-existing: "false"', workflow)
        self.assertIn('attestations: "true"', workflow)
        self.assertIn(r"docker\.m\.daocloud\.io/library/python:", workflow)
        self.assertIn("https://pypi.tuna.tsinghua.edu.cn/simple", workflow)
        self.assertEqual(
            workflow.count("python scripts/testpypi_roundtrip.py preflight"), 2
        )
        self.assertEqual(
            workflow.count("python scripts/core_testpypi_roundtrip.py preflight"),
            2,
        )
        self.assertIn("python scripts/testpypi_roundtrip.py wait", workflow)
        self.assertIn("python scripts/core_testpypi_roundtrip.py wait", workflow)
        self.assertIn("python scripts/testpypi_roundtrip.py finalize", workflow)
        self.assertIn("python scripts/core_testpypi_roundtrip.py finalize", workflow)
        self.assertIn("scripts/installed_wheel_smoke.py", workflow)
        self.assertIn("scripts/sdist_consumer_smoke.py", workflow)
        self.assertIn('test "$code" -eq 5', workflow)
        self.assertEqual(
            testpypi.UNTAGGED_SOURCE_STATE, "clean_untagged_local_candidate"
        )
        self.assertNotRegex(workflow, r"git\s+tag\s+(?!-{1,2}points-at)")
        self.assertNotIn("git push --tags", workflow)
        self.assertIn(
            'test -z "$(git status --porcelain=v1 --untracked-files=all)"',
            workflow,
        )
        for path in (
            "candidate/testpypi-upload",
            "candidate/testpypi-core-upload",
            "candidate/testpypi-bundle-upload",
            "candidate/release-metadata",
            "candidate/testpypi-evidence",
            "candidate/build-wheelhouse",
        ):
            self.assertIn(path, workflow)


if __name__ == "__main__":
    unittest.main()
