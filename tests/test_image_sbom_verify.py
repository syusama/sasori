from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import image_sbom_verify  # noqa: E402


REVISION = "a" * 40
CONFIG_DAEMON_ID = "sha256:" + "b" * 64
REPO_DAEMON_ID = "sha256:" + "c" * 64
LAYER_DIGEST = "sha256:" + "d" * 64
FILE_DIGEST = "e" * 64
IMAGE_REFERENCE = "sasori:local"


class ImageSBOMVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def _write_json(self, path: Path, value: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _fixture(
        self, name: str, *, daemon_image_id: str = CONFIG_DAEMON_ID
    ) -> tuple[Path, Path, str]:
        root = self.root / name
        root.mkdir()
        config = {
            "architecture": "amd64",
            "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": [LAYER_DIGEST]},
        }
        config_raw = json.dumps(config, separators=(",", ":"), sort_keys=True).encode()
        config_digest = "sha256:" + hashlib.sha256(config_raw).hexdigest()
        layer = {
            "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
            "digest": LAYER_DIGEST,
            "size": 200,
        }
        manifest = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "size": len(config_raw),
                "digest": config_digest,
            },
            "layers": [layer],
        }
        manifest_raw = json.dumps(
            manifest, separators=(",", ":"), sort_keys=True
        ).encode()
        manifest_digest = "sha256:" + hashlib.sha256(manifest_raw).hexdigest()
        repo_digests = []
        if daemon_image_id == REPO_DAEMON_ID:
            repo_digests = [f"sasori@{REPO_DAEMON_ID}"]
        catalog = {
            "artifacts": [
                {
                    "id": "package-1",
                    "name": "sasori",
                    "version": "0.1",
                    "purl": "",
                }
            ],
            "artifactRelationships": [
                {"parent": "package-1", "child": "file-1"}
            ],
            "files": [
                {
                    "id": "file-1",
                    "location": {"path": "/one"},
                    "metadata": {"type": "RegularFile"},
                    "digests": [
                        {"algorithm": "sha256", "value": FILE_DIGEST}
                    ],
                }
            ],
            "source": {
                "id": manifest_digest.removeprefix("sha256:"),
                "name": "sasori",
                "version": "local",
                "type": "image",
                "metadata": {
                    "userInput": IMAGE_REFERENCE,
                    "imageID": config_digest,
                    "manifestDigest": manifest_digest,
                    "mediaType": manifest["mediaType"],
                    "tags": [IMAGE_REFERENCE],
                    "imageSize": 300,
                    "layers": [layer],
                    "manifest": base64.b64encode(manifest_raw).decode("ascii"),
                    "config": base64.b64encode(config_raw).decode("ascii"),
                    "repoDigests": repo_digests,
                    "architecture": "amd64",
                    "os": "linux",
                    "labels": {},
                },
            },
            "distro": {"name": "debian", "version": "13"},
            "descriptor": {
                "name": image_sbom_verify.SCANNER_NAME,
                "version": image_sbom_verify.SCANNER_VERSION,
                "configuration": {},
            },
            "schema": {
                "version": image_sbom_verify.CATALOG_SCHEMA_VERSION,
                "url": image_sbom_verify.CATALOG_SCHEMA_URL,
            },
        }
        purl_digest = manifest_digest.replace(":", "%3A")
        root_package = {
            "name": "sasori",
            "SPDXID": "SPDXRef-DocumentRoot-Image-sasori",
            "versionInfo": "local",
            "supplier": "NOASSERTION",
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False,
            "checksums": [
                {
                    "algorithm": "SHA256",
                    "checksumValue": manifest_digest.removeprefix("sha256:"),
                }
            ],
            "licenseConcluded": "NOASSERTION",
            "licenseDeclared": "NOASSERTION",
            "copyrightText": "NOASSERTION",
            "externalRefs": [
                {
                    "referenceCategory": "PACKAGE-MANAGER",
                    "referenceType": "purl",
                    "referenceLocator": (
                        f"pkg:oci/sasori@{purl_digest}?arch=amd64&tag=local"
                    ),
                }
            ],
            "primaryPackagePurpose": "CONTAINER",
        }
        spdx = {
            "spdxVersion": "SPDX-2.3",
            "dataLicense": "CC0-1.0",
            "SPDXID": "SPDXRef-DOCUMENT",
            "name": "sasori",
            "documentNamespace": "https://anchore.com/syft/image/test",
            "creationInfo": {
                "licenseListVersion": "3.28",
                "creators": [
                    "Organization: Anchore, Inc",
                    f"Tool: syft-{image_sbom_verify.SCANNER_VERSION}",
                ],
                "created": "2026-08-08T00:00:00Z",
            },
            "packages": [
                root_package,
                {
                    "name": "sasori",
                    "versionInfo": "0.1",
                    "SPDXID": "SPDXRef-Package-sasori",
                    "externalRefs": [],
                },
            ],
            "files": [
                {
                    "fileName": "/one",
                    "SPDXID": "SPDXRef-File-one",
                    "checksums": [
                        {"algorithm": "SHA256", "checksumValue": FILE_DIGEST}
                    ],
                }
            ],
            "relationships": [
                {
                    "spdxElementId": "SPDXRef-DOCUMENT",
                    "relatedSpdxElement": root_package["SPDXID"],
                    "relationshipType": "DESCRIBES",
                },
                {
                    "spdxElementId": root_package["SPDXID"],
                    "relatedSpdxElement": "SPDXRef-Package-sasori",
                    "relationshipType": "CONTAINS",
                }
            ],
        }
        catalog_path = root / "image.syft.json"
        spdx_path = root / "image.spdx.json"
        self._write_json(catalog_path, catalog)
        self._write_json(spdx_path, spdx)
        expected_daemon_id = (
            config_digest if daemon_image_id == CONFIG_DAEMON_ID else daemon_image_id
        )
        daemon_inspect = {
            "Id": expected_daemon_id,
            "RepoTags": [IMAGE_REFERENCE],
            "RepoDigests": repo_digests,
            "Architecture": "amd64",
            "Os": "linux",
            "RootFS": {"Type": "layers", "Layers": [LAYER_DIGEST]},
            "Descriptor": (
                {
                    "digest": expected_daemon_id,
                    "mediaType": "application/vnd.oci.image.index.v1+json",
                    "size": 512,
                }
                if daemon_image_id == REPO_DAEMON_ID
                else None
            ),
        }
        self._write_json(root / "daemon.json", daemon_inspect)
        return spdx_path, catalog_path, expected_daemon_id

    def _binding(
        self,
        spdx: Path,
        catalog: Path,
        daemon_image_id: str,
        runtime_image_id: str | None = None,
    ) -> dict[str, object]:
        runtime_image_id = runtime_image_id or daemon_image_id
        return image_sbom_verify.image_sbom_binding(
            spdx_path=spdx,
            catalog_path=catalog,
            daemon_inspect_path=catalog.parent / "daemon.json",
            image_reference=IMAGE_REFERENCE,
            daemon_image_id=daemon_image_id,
            runtime_image_id=runtime_image_id,
            source_revision=REVISION,
        )

    def test_real_syft_contract_accepts_config_or_repo_daemon_identity(self) -> None:
        for label, supplied in (
            ("config", CONFIG_DAEMON_ID),
            ("repo", REPO_DAEMON_ID),
        ):
            with self.subTest(daemon_identity=label):
                spdx, catalog, expected_daemon_id = self._fixture(
                    label, daemon_image_id=supplied
                )
                binding = self._binding(spdx, catalog, expected_daemon_id)
                self.assertEqual(binding["schema_version"], 1)
                self.assertEqual(binding["kind"], "sasori.image-sbom-binding")
                self.assertFalse(binding["signed"])
                self.assertEqual(binding["claim"], image_sbom_verify.BINDING_CLAIM)
                self.assertEqual(binding["source_revision"], REVISION)
                self.assertEqual(
                    binding["image"]["daemon_image_id"], expected_daemon_id
                )
                self.assertEqual(
                    set(binding["image"]),
                    {
                        "reference",
                        "daemon_image_id",
                        "runtime_container_image_id",
                        "daemon_descriptor",
                        "daemon_repo_digests",
                        "rootfs_layer_digests",
                        "scanner_manifest_digest",
                        "config_digest",
                        "architecture",
                        "os",
                    },
                )
                self.assertEqual(binding["scanner"]["version"], "1.50.0")
                self.assertEqual(binding["sbom"]["package_count"], 2)
                self.assertEqual(binding["catalog"]["artifact_count"], 1)

    def test_hosted_syft_may_omit_empty_image_labels(self) -> None:
        spdx, catalog, daemon_image_id = self._fixture("hosted-no-labels")
        value = json.loads(catalog.read_text(encoding="utf-8"))
        value["source"]["metadata"].pop("labels")
        self._write_json(catalog, value)

        binding = self._binding(spdx, catalog, daemon_image_id)

        self.assertEqual(binding["image"]["daemon_image_id"], daemon_image_id)

    def test_cli_creates_then_reverifies_an_exact_binding(self) -> None:
        spdx, catalog, daemon_image_id = self._fixture("cli")
        binding = self.root / "binding.json"
        arguments = [
            "--spdx",
            str(spdx),
            "--catalog",
            str(catalog),
            "--daemon-inspect",
            str(catalog.parent / "daemon.json"),
            "--image-reference",
            IMAGE_REFERENCE,
            "--daemon-image-id",
            daemon_image_id,
            "--runtime-image-id",
            daemon_image_id,
            "--source-revision",
            REVISION,
            "--binding",
            str(binding),
        ]
        for command in ("create", "verify"):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = image_sbom_verify.main([command, *arguments])
            self.assertEqual((code, stderr.getvalue()), (0, ""))
            self.assertEqual(json.loads(stdout.getvalue())["signed"], False)
        stored = json.loads(binding.read_text(encoding="utf-8"))
        stored["trusted"] = True
        self._write_json(binding, stored)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ):
            self.assertEqual(image_sbom_verify.main(["verify", *arguments]), 2)

    def test_catalog_spdx_and_subject_tampering_fail_closed(self) -> None:
        mutations = (
            ("catalog-extra", "catalog", lambda value: value.update({"trusted": True})),
            (
                "scanner-version",
                "catalog",
                lambda value: value["descriptor"].update({"version": "latest"}),
            ),
            (
                "source-id",
                "catalog",
                lambda value: value["source"].update({"id": "0" * 64}),
            ),
            (
                "manifest-payload",
                "catalog",
                lambda value: value["source"]["metadata"].update(
                    {
                        "manifest": base64.b64encode(b'{"changed":true}').decode(
                            "ascii"
                        )
                    }
                ),
            ),
            (
                "layer-digest",
                "catalog",
                lambda value: value["source"]["metadata"]["layers"][0].update(
                    {"digest": "sha256:" + "0" * 64}
                ),
            ),
            (
                "media-type",
                "catalog",
                lambda value: value["source"]["metadata"].update(
                    {"mediaType": "application/x-fake"}
                ),
            ),
            (
                "tag",
                "catalog",
                lambda value: value["source"]["metadata"].update({"tags": []}),
            ),
            (
                "unknown-image-metadata",
                "catalog",
                lambda value: value["source"]["metadata"].update(
                    {"trusted": True}
                ),
            ),
            (
                "invalid-image-labels",
                "catalog",
                lambda value: value["source"]["metadata"].update(
                    {"labels": ["not-a-map"]}
                ),
            ),
            (
                "invalid-image-label-value",
                "catalog",
                lambda value: value["source"]["metadata"].update(
                    {"labels": {"not-a-string": True}}
                ),
            ),
            (
                "package-subject",
                "spdx",
                lambda value: value["packages"][1].update({"name": "other"}),
            ),
            (
                "file-subject",
                "spdx",
                lambda value: value["files"][0]["checksums"][0].update(
                    {"checksumValue": "f" * 64}
                ),
            ),
            ("spdx-extra", "spdx", lambda value: value.update({"trusted": True})),
            (
                "root-checksum",
                "spdx",
                lambda value: value["packages"][0]["checksums"][0].update(
                    {"checksumValue": "0" * 64}
                ),
            ),
            (
                "root-extension",
                "spdx",
                lambda value: value["packages"][0].update({"signed": True}),
            ),
            (
                "second-container",
                "spdx",
                lambda value: value["packages"].append(
                    {
                        "SPDXID": "SPDXRef-Other-Container",
                        "primaryPackagePurpose": "CONTAINER",
                    }
                ),
            ),
            (
                "missing-containment",
                "spdx",
                lambda value: value.update(
                    {
                        "relationships": [
                            item
                            for item in value["relationships"]
                            if item["relationshipType"] != "CONTAINS"
                        ]
                    }
                ),
            ),
            (
                "missing-description",
                "spdx",
                lambda value: value.update(
                    {
                        "relationships": [
                            item
                            for item in value["relationships"]
                            if item["relationshipType"] != "DESCRIBES"
                        ]
                    }
                ),
            ),
            (
                "invalid-created",
                "spdx",
                lambda value: value["creationInfo"].update(
                    {"created": "2026-08-08T00:00:00+00:00"}
                ),
            ),
        )
        for name, target, mutate in mutations:
            with self.subTest(boundary=name):
                spdx, catalog, daemon_image_id = self._fixture(name)
                path = catalog if target == "catalog" else spdx
                value = json.loads(path.read_text(encoding="utf-8"))
                mutate(value)
                self._write_json(path, value)
                with self.assertRaises(image_sbom_verify.ImageSBOMError):
                    self._binding(spdx, catalog, daemon_image_id)

    def test_invalid_daemon_revision_json_and_symlink_fail_closed(self) -> None:
        spdx, catalog, daemon_image_id = self._fixture("boundaries")
        with self.assertRaises(image_sbom_verify.ImageSBOMError):
            self._binding(spdx, catalog, "sha256:" + "9" * 64)
        with self.assertRaises(image_sbom_verify.ImageSBOMError):
            self._binding(
                spdx,
                catalog,
                daemon_image_id,
                runtime_image_id="sha256:" + "8" * 64,
            )
        repo_spdx, repo_catalog, repo_daemon_id = self._fixture(
            "wrong-repo-name", daemon_image_id=REPO_DAEMON_ID
        )
        repo_value = json.loads(repo_catalog.read_text(encoding="utf-8"))
        repo_value["source"]["metadata"]["repoDigests"] = [
            f"other@{REPO_DAEMON_ID}"
        ]
        self._write_json(repo_catalog, repo_value)
        with self.assertRaises(image_sbom_verify.ImageSBOMError):
            self._binding(repo_spdx, repo_catalog, repo_daemon_id)
        spdx, catalog, daemon_image_id = self._fixture("inspect-rootfs")
        runtime_image_id = json.loads(catalog.read_text(encoding="utf-8"))["source"][
            "metadata"
        ]["imageID"]
        inspect_path = catalog.parent / "daemon.json"
        inspect_value = json.loads(inspect_path.read_text(encoding="utf-8"))
        inspect_value["RootFS"]["Layers"] = ["sha256:" + "9" * 64]
        self._write_json(inspect_path, inspect_value)
        with self.assertRaises(image_sbom_verify.ImageSBOMError):
            self._binding(spdx, catalog, daemon_image_id)
        with self.assertRaises(image_sbom_verify.ImageSBOMError):
            image_sbom_verify.image_sbom_binding(
                spdx_path=spdx,
                catalog_path=catalog,
                daemon_inspect_path=catalog.parent / "daemon.json",
                image_reference=IMAGE_REFERENCE,
                daemon_image_id=daemon_image_id,
                runtime_image_id=daemon_image_id,
                source_revision="short",
            )

        catalog.write_text('{"kind":"one","kind":"two"}', encoding="utf-8")
        with self.assertRaises(image_sbom_verify.ImageSBOMError):
            self._binding(
                spdx,
                catalog,
                daemon_image_id,
                runtime_image_id=runtime_image_id,
            )

        spdx, catalog, daemon_image_id = self._fixture("symlink")
        original = Path.is_symlink

        def pretend_symlink(path: Path) -> bool:
            return path == spdx or original(path)

        with mock.patch.object(Path, "is_symlink", pretend_symlink), self.assertRaises(
            image_sbom_verify.ImageSBOMError
        ):
            self._binding(spdx, catalog, daemon_image_id)

    def test_workflow_locks_scans_binds_audits_and_uploads_the_same_image(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("anchore/sbom-action@", workflow)
        for required in (
            "syft_version=1.50.0",
            "syft_sha256=bf7b29ff57f06da30918266a0e1c2885a8f99784798d1bdb1628886aa015d788",
            '"https://github.com/anchore/syft/releases/download/v${syft_version}/syft_${syft_version}_linux_amd64.tar.gz"',
            "sha256sum --check --strict",
            'tar -xzf "$archive" -C "$syft_root" syft',
            'docker image inspect sasori:local --format \'{{.Id}}\'',
            'test "$before" = "$SASORI_IMAGE_ID"',
            'test "$after" = "$before"',
            "SASORI_RUNTIME_IMAGE_ID",
            'test "$runtime_image_id" = "$SASORI_RUNTIME_IMAGE_ID"',
            'cmp -s "$IMAGE_INSPECT_FILE" "$syft_root/image-inspect-after.json"',
            '--output "spdx-json=$IMAGE_SBOM_FILE"',
            '--output "syft-json=$IMAGE_CATALOG_FILE"',
            "python scripts/image_sbom_verify.py create",
            "python scripts/image_sbom_verify.py verify",
            '--daemon-inspect "$IMAGE_INSPECT_FILE"',
            '--runtime-image-id "$runtime_image_id"',
            '--daemon-image-id "$after"',
            '--source-revision "$GITHUB_SHA"',
            '"image SBOM:$image_sbom"',
            '"image catalog:$image_catalog"',
            '"image SBOM binding:$image_binding"',
            '"image inspection:$image_inspect"',
            "name: sasori-image-sbom-${{ github.sha }}",
            "${{ runner.temp }}/sasori-image-${{ github.run_id }}-${{ github.run_attempt }}.spdx.json",
            "${{ runner.temp }}/sasori-image-${{ github.run_id }}-${{ github.run_attempt }}.syft.json",
            "${{ runner.temp }}/sasori-image-${{ github.run_id }}-${{ github.run_attempt }}.binding.json",
            "retention-days: 30",
        ):
            with self.subTest(required=required):
                self.assertIn(required, workflow)
        self.assertLess(
            workflow.index("Verify workflow, restart persistence, and exclusive ownership"),
            workflow.index("Generate and bind the final image SBOM"),
        )
        self.assertLess(
            workflow.index("Generate and bind the final image SBOM"),
            workflow.index("Audit logs and generated reports"),
        )
        self.assertLess(
            workflow.index("Audit logs and generated reports"),
            workflow.index("Stop containers and remove sensitive local files"),
        )
        self.assertLess(
            workflow.index("Stop containers and remove sensitive local files"),
            workflow.index("Upload the verified image SBOM and binding"),
        )
        upload = workflow[
            workflow.index("Upload the verified image SBOM and binding") :
            workflow.index("  package:")
        ]
        self.assertNotIn(".inspect.json", upload)
        self.assertEqual(
            workflow.count(
                'docker container inspect "$container_id" --format \'{{.Image}}\''
            ),
            2,
        )


if __name__ == "__main__":
    unittest.main()
