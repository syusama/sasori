from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import re
import sys
import urllib.parse
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


SCANNER_NAME = "syft"
SCANNER_VERSION = "1.50.0"
CATALOG_SCHEMA_VERSION = "16.1.10"
CATALOG_SCHEMA_URL = (
    "https://raw.githubusercontent.com/anchore/syft/main/schema/"
    "json/schema-16.1.10.json"
)
BINDING_CLAIM = (
    "unsigned CI image inventory binding; not trusted provenance or a signature"
)
MAX_JSON_BYTES = 32 * 1024 * 1024
MAX_EMBEDDED_BYTES = 2 * 1024 * 1024
SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
REVISION = re.compile(r"[0-9a-f]{40}")
IMAGE_REFERENCE = re.compile(
    r"(?P<name>[a-z0-9]+(?:[._-][a-z0-9]+)*):"
    r"(?P<tag>[A-Za-z0-9][A-Za-z0-9._-]*)"
)


class ImageSBOMError(RuntimeError):
    pass


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant: {value}")


def _regular(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise ImageSBOMError(f"SBOM input is not a regular file: {path}")


def _load_json(path: Path) -> dict[str, object]:
    _regular(path)
    if path.stat().st_size <= 0 or path.stat().st_size > MAX_JSON_BYTES:
        raise ImageSBOMError(f"SBOM JSON size is invalid: {path.name}")
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ImageSBOMError(f"SBOM JSON is invalid: {path.name}") from exc
    if not isinstance(value, dict):
        raise ImageSBOMError(f"SBOM JSON must be an object: {path.name}")
    return value


def _keys(value: dict[str, object], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ImageSBOMError(f"{label} fields do not match the locked schema")


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise ImageSBOMError(f"{label} is not a SHA-256 identity")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _embedded_json(value: object, label: str) -> tuple[bytes, dict[str, object]]:
    if not isinstance(value, str) or len(value) > MAX_EMBEDDED_BYTES * 2:
        raise ImageSBOMError(f"{label} payload is invalid")
    try:
        raw = base64.b64decode(value, validate=True)
        decoded = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (binascii.Error, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ImageSBOMError(f"{label} payload is invalid") from exc
    if len(raw) > MAX_EMBEDDED_BYTES or not isinstance(decoded, dict):
        raise ImageSBOMError(f"{label} payload is invalid")
    return raw, decoded


def _created(value: object) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ImageSBOMError("SPDX creation time is invalid")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise ImageSBOMError("SPDX creation time is invalid") from exc
    if parsed.isoformat().replace("+00:00", "Z") != value:
        raise ImageSBOMError("SPDX creation time is not canonical")
    return value


def _validate_daemon_inspect(
    inspect: dict[str, object], image_reference: str, daemon_image_id: str
) -> dict[str, object]:
    daemon_image_id = _digest(daemon_image_id, "daemon image ID")
    repo_tags = inspect.get("RepoTags")
    repo_digests = inspect.get("RepoDigests")
    rootfs = inspect.get("RootFS")
    architecture = inspect.get("Architecture")
    operating_system = inspect.get("Os")
    descriptor = inspect.get("Descriptor")
    if (
        inspect.get("Id") != daemon_image_id
        or not isinstance(repo_tags, list)
        or image_reference not in repo_tags
        or not isinstance(repo_digests, list)
        or any(not isinstance(item, str) for item in repo_digests)
        or not isinstance(rootfs, dict)
        or rootfs.get("Type") != "layers"
        or not isinstance(rootfs.get("Layers"), list)
        or not rootfs["Layers"]
        or any(
            not isinstance(item, str) or SHA256.fullmatch(item) is None
            for item in rootfs["Layers"]
        )
        or not isinstance(architecture, str)
        or not architecture
        or not isinstance(operating_system, str)
        or not operating_system
    ):
        raise ImageSBOMError("Docker daemon image inspection is invalid")
    descriptor_identity: dict[str, object] | None = None
    if descriptor is not None:
        if not isinstance(descriptor, dict):
            raise ImageSBOMError("Docker daemon image descriptor is invalid")
        descriptor_digest = _digest(
            descriptor.get("digest"), "daemon descriptor digest"
        )
        descriptor_media_type = descriptor.get("mediaType")
        descriptor_size = descriptor.get("size")
        if (
            descriptor_digest != daemon_image_id
            or not isinstance(descriptor_media_type, str)
            or not descriptor_media_type
            or not isinstance(descriptor_size, int)
            or isinstance(descriptor_size, bool)
            or descriptor_size <= 0
        ):
            raise ImageSBOMError("Docker daemon image descriptor is invalid")
        descriptor_identity = {
            "digest": descriptor_digest,
            "media_type": descriptor_media_type,
            "size": descriptor_size,
        }
    return {
        "image_id": daemon_image_id,
        "repo_digests": repo_digests,
        "rootfs_layer_digests": rootfs["Layers"],
        "architecture": architecture,
        "os": operating_system,
        "descriptor": descriptor_identity,
    }


def _validate_catalog(
    catalog: dict[str, object],
    image_reference: str,
    daemon_identity: dict[str, object],
) -> dict[str, object]:
    _keys(
        catalog,
        {
            "artifacts",
            "artifactRelationships",
            "files",
            "source",
            "distro",
            "descriptor",
            "schema",
        },
        "Syft catalog",
    )
    schema = catalog.get("schema")
    descriptor = catalog.get("descriptor")
    source = catalog.get("source")
    if not all(isinstance(item, dict) for item in (schema, descriptor, source)):
        raise ImageSBOMError("Syft catalog identity is invalid")
    _keys(schema, {"version", "url"}, "Syft catalog schema")
    _keys(descriptor, {"name", "version", "configuration"}, "Syft descriptor")
    _keys(source, {"id", "name", "version", "type", "metadata"}, "Syft source")
    if (
        schema
        != {"version": CATALOG_SCHEMA_VERSION, "url": CATALOG_SCHEMA_URL}
        or descriptor.get("name") != SCANNER_NAME
        or descriptor.get("version") != SCANNER_VERSION
        or not isinstance(descriptor.get("configuration"), dict)
    ):
        raise ImageSBOMError("Syft scanner or catalog schema is not locked")

    reference = IMAGE_REFERENCE.fullmatch(image_reference)
    metadata = source.get("metadata")
    if reference is None or not isinstance(metadata, dict):
        raise ImageSBOMError("Syft image source is invalid")
    _keys(
        metadata,
        {
            "userInput",
            "imageID",
            "manifestDigest",
            "mediaType",
            "tags",
            "imageSize",
            "layers",
            "manifest",
            "config",
            "repoDigests",
            "architecture",
            "os",
            "labels",
        },
        "Syft image metadata",
    )
    manifest_digest = _digest(metadata.get("manifestDigest"), "manifest digest")
    config_digest = _digest(metadata.get("imageID"), "image config digest")
    repo_digests = metadata.get("repoDigests")
    tags = metadata.get("tags")
    layers = metadata.get("layers")
    architecture = metadata.get("architecture")
    operating_system = metadata.get("os")
    if (
        source.get("id") != manifest_digest.removeprefix("sha256:")
        or source.get("name") != reference.group("name")
        or source.get("version") != reference.group("tag")
        or source.get("type") != "image"
        or metadata.get("userInput") != image_reference
        or not isinstance(tags, list)
        or image_reference not in tags
        or repo_digests != daemon_identity["repo_digests"]
        or not isinstance(layers, list)
        or not layers
        or not isinstance(architecture, str)
        or not architecture
        or not isinstance(operating_system, str)
        or not operating_system
        or not isinstance(metadata.get("labels"), dict)
        or not isinstance(metadata.get("imageSize"), int)
        or isinstance(metadata.get("imageSize"), bool)
        or metadata["imageSize"] <= 0
    ):
        raise ImageSBOMError("Syft image source identity is invalid")
    manifest_raw, manifest = _embedded_json(metadata.get("manifest"), "image manifest")
    config_raw, config = _embedded_json(metadata.get("config"), "image config")
    if (
        "sha256:" + hashlib.sha256(manifest_raw).hexdigest() != manifest_digest
        or "sha256:" + hashlib.sha256(config_raw).hexdigest() != config_digest
    ):
        raise ImageSBOMError("embedded image manifest or config digest is invalid")
    _keys(manifest, {"schemaVersion", "mediaType", "config", "layers"}, "image manifest")
    manifest_config = manifest.get("config")
    manifest_layers = manifest.get("layers")
    config_rootfs = config.get("rootfs")
    if (
        not isinstance(manifest_config, dict)
        or not isinstance(manifest_layers, list)
        or not isinstance(config_rootfs, dict)
    ):
        raise ImageSBOMError("embedded image manifest is invalid")
    _keys(manifest_config, {"mediaType", "size", "digest"}, "image config subject")
    for layer in layers:
        if not isinstance(layer, dict):
            raise ImageSBOMError("Syft image layer is invalid")
        _keys(layer, {"mediaType", "digest", "size"}, "Syft image layer")
        _digest(layer.get("digest"), "image layer digest")
    for layer in manifest_layers:
        if not isinstance(layer, dict):
            raise ImageSBOMError("embedded image manifest layer is invalid")
        _keys(layer, {"mediaType", "digest", "size"}, "image manifest layer")
        _digest(layer.get("digest"), "image manifest layer digest")
    catalog_layer_subjects = [
        (layer.get("mediaType"), layer.get("digest")) for layer in layers
    ]
    manifest_layer_subjects = [
        (layer.get("mediaType"), layer.get("digest")) for layer in manifest_layers
    ]
    if (
        manifest.get("schemaVersion") != 2
        or metadata.get("mediaType") != manifest.get("mediaType")
        or manifest_config.get("digest") != config_digest
        or manifest_config.get("size") != len(config_raw)
        or manifest_layer_subjects != catalog_layer_subjects
        or config.get("architecture") != architecture
        or config.get("os") != operating_system
        or architecture != daemon_identity["architecture"]
        or operating_system != daemon_identity["os"]
        or config_rootfs.get("type") != "layers"
        or config_rootfs.get("diff_ids")
        != daemon_identity["rootfs_layer_digests"]
        or [layer.get("digest") for layer in layers]
        != daemon_identity["rootfs_layer_digests"]
    ):
        raise ImageSBOMError("embedded image subjects do not match the catalog")
    daemon_image_id = str(daemon_identity["image_id"])
    if daemon_image_id != config_digest:
        descriptor = daemon_identity["descriptor"]
        expected_repo_digest = f"{reference.group('name')}@{daemon_image_id}"
        if (
            not isinstance(descriptor, dict)
            or descriptor.get("digest") != daemon_image_id
            or expected_repo_digest not in repo_digests
        ):
            raise ImageSBOMError("catalog is not bound to the daemon descriptor")

    artifacts = catalog.get("artifacts")
    relationships = catalog.get("artifactRelationships")
    files = catalog.get("files")
    if (
        not isinstance(artifacts, list)
        or not artifacts
        or not isinstance(relationships, list)
        or not isinstance(files, list)
        or not files
    ):
        raise ImageSBOMError("Syft package or file inventory is empty")
    package_subjects: Counter[tuple[str, str, str]] = Counter()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise ImageSBOMError("Syft package inventory is invalid")
        name = artifact.get("name")
        version = artifact.get("version")
        purl = artifact.get("purl")
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(version, str)
            or not isinstance(purl, str)
        ):
            raise ImageSBOMError("Syft package subject is invalid")
        package_subjects[(name, version, purl)] += 1
    file_subjects: Counter[tuple[str, str | None]] = Counter()
    for file in files:
        if not isinstance(file, dict) or not isinstance(file.get("location"), dict):
            raise ImageSBOMError("Syft file inventory is invalid")
        path = file["location"].get("path")
        digests = file.get("digests", [])
        if not isinstance(path, str) or not path.lstrip("/"):
            raise ImageSBOMError("Syft file subject is invalid")
        if not isinstance(digests, list):
            raise ImageSBOMError("Syft file digest inventory is invalid")
        sha256_values = [
            item.get("value")
            for item in digests
            if isinstance(item, dict) and item.get("algorithm") == "sha256"
        ]
        if len(sha256_values) > 1 or any(
            not isinstance(value, str)
            or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in sha256_values
        ):
            raise ImageSBOMError("Syft file SHA-256 inventory is invalid")
        metadata = file.get("metadata")
        if (
            isinstance(metadata, dict)
            and metadata.get("type") == "RegularFile"
            and len(sha256_values) != 1
        ):
            raise ImageSBOMError("Syft regular file has no SHA-256 digest")
        file_subjects[(path.lstrip("/"), sha256_values[0] if sha256_values else None)] += 1
    return {
        "manifest_digest": manifest_digest,
        "config_digest": config_digest,
        "architecture": architecture,
        "os": operating_system,
        "artifact_count": len(artifacts),
        "file_count": len(files),
        "relationship_count": len(relationships),
        "package_subjects": package_subjects,
        "file_subjects": file_subjects,
    }


def _validate_spdx(
    spdx: dict[str, object], image_reference: str, catalog_identity: dict[str, object]
) -> dict[str, object]:
    required = {
        "spdxVersion",
        "dataLicense",
        "SPDXID",
        "name",
        "documentNamespace",
        "creationInfo",
        "packages",
        "files",
        "relationships",
    }
    if set(spdx) not in (required, required | {"hasExtractedLicensingInfos"}):
        raise ImageSBOMError("SPDX fields do not match the locked exporter schema")
    creation = spdx.get("creationInfo")
    packages = spdx.get("packages")
    files = spdx.get("files")
    relationships = spdx.get("relationships")
    if not isinstance(creation, dict):
        raise ImageSBOMError("SPDX creation information is invalid")
    _keys(creation, {"licenseListVersion", "creators", "created"}, "SPDX creation")
    if (
        spdx.get("spdxVersion") != "SPDX-2.3"
        or spdx.get("dataLicense") != "CC0-1.0"
        or spdx.get("SPDXID") != "SPDXRef-DOCUMENT"
        or spdx.get("name") != IMAGE_REFERENCE.fullmatch(image_reference).group("name")
        or not isinstance(spdx.get("documentNamespace"), str)
        or not str(spdx["documentNamespace"]).startswith(
            "https://anchore.com/syft/image/"
        )
        or creation.get("creators")
        != ["Organization: Anchore, Inc", f"Tool: {SCANNER_NAME}-{SCANNER_VERSION}"]
        or not isinstance(packages, list)
        or not isinstance(files, list)
        or not isinstance(relationships, list)
    ):
        raise ImageSBOMError("SPDX document identity is invalid")
    created_at = _created(creation.get("created"))

    package_ids: set[str] = set()
    roots: list[dict[str, object]] = []
    package_subjects: Counter[tuple[str, str, str]] = Counter()
    for package in packages:
        if not isinstance(package, dict) or not isinstance(package.get("SPDXID"), str):
            raise ImageSBOMError("SPDX package is invalid")
        package_id = package["SPDXID"]
        if package_id in package_ids:
            raise ImageSBOMError("SPDX package identity is duplicated")
        package_ids.add(package_id)
        if package.get("primaryPackagePurpose") == "CONTAINER":
            roots.append(package)
        else:
            name = package.get("name")
            version = package.get("versionInfo")
            external_refs = package.get("externalRefs", [])
            if (
                not isinstance(name, str)
                or not name
                or not isinstance(version, str)
                or not isinstance(external_refs, list)
            ):
                raise ImageSBOMError("SPDX package subject is invalid")
            purls = [
                item.get("referenceLocator")
                for item in external_refs
                if isinstance(item, dict) and item.get("referenceType") == "purl"
            ]
            if len(purls) > 1 or any(not isinstance(item, str) for item in purls):
                raise ImageSBOMError("SPDX package purl inventory is invalid")
            package_subjects[(name, version, purls[0] if purls else "")] += 1
    if len(roots) != 1 or len(packages) != catalog_identity["artifact_count"] + 1:
        raise ImageSBOMError("SPDX container or package inventory is invalid")
    if len(files) != catalog_identity["file_count"]:
        raise ImageSBOMError("SPDX file inventory does not match the catalog")
    if package_subjects != catalog_identity["package_subjects"]:
        raise ImageSBOMError("SPDX package subjects do not match the catalog")
    file_subjects: Counter[tuple[str, str | None]] = Counter()
    for file in files:
        if not isinstance(file, dict):
            raise ImageSBOMError("SPDX file inventory is invalid")
        path = file.get("fileName")
        checksums = file.get("checksums", [])
        if (
            not isinstance(path, str)
            or not path.lstrip("/")
            or not isinstance(checksums, list)
        ):
            raise ImageSBOMError("SPDX file subject is invalid")
        sha256_values = [
            item.get("checksumValue")
            for item in checksums
            if isinstance(item, dict) and item.get("algorithm") == "SHA256"
        ]
        if len(sha256_values) > 1 or any(
            not isinstance(value, str)
            or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in sha256_values
        ):
            raise ImageSBOMError("SPDX file SHA-256 inventory is invalid")
        file_subjects[(path.lstrip("/"), sha256_values[0] if sha256_values else None)] += 1
    if file_subjects != catalog_identity["file_subjects"]:
        raise ImageSBOMError("SPDX file subjects do not match the catalog")

    reference = IMAGE_REFERENCE.fullmatch(image_reference)
    root = roots[0]
    _keys(
        root,
        {
            "name",
            "SPDXID",
            "versionInfo",
            "supplier",
            "downloadLocation",
            "filesAnalyzed",
            "checksums",
            "licenseConcluded",
            "licenseDeclared",
            "copyrightText",
            "externalRefs",
            "primaryPackagePurpose",
        },
        "SPDX container package",
    )
    manifest_digest = str(catalog_identity["manifest_digest"])
    purl = (
        f"pkg:oci/{reference.group('name')}@"
        f"{urllib.parse.quote(manifest_digest, safe='')}"
        f"?arch={urllib.parse.quote(str(catalog_identity['architecture']), safe='')}"
        f"&tag={urllib.parse.quote(reference.group('tag'), safe='')}"
    )
    if (
        root.get("name") != reference.group("name")
        or root.get("versionInfo") != reference.group("tag")
        or root.get("filesAnalyzed") is not False
        or root.get("checksums")
        != [
            {
                "algorithm": "SHA256",
                "checksumValue": manifest_digest.removeprefix("sha256:"),
            }
        ]
        or root.get("externalRefs")
        != [
            {
                "referenceCategory": "PACKAGE-MANAGER",
                "referenceType": "purl",
                "referenceLocator": purl,
            }
        ]
    ):
        raise ImageSBOMError("SPDX container subject does not match the catalog")
    root_id = root["SPDXID"]
    contained = {
        item.get("relatedSpdxElement")
        for item in relationships
        if isinstance(item, dict)
        and item.get("spdxElementId") == root_id
        and item.get("relationshipType") == "CONTAINS"
    }
    if package_ids.difference({root_id}) != contained:
        raise ImageSBOMError("SPDX container does not contain every package")
    descriptions = [
        item
        for item in relationships
        if isinstance(item, dict)
        and item.get("spdxElementId") == "SPDXRef-DOCUMENT"
        and item.get("relationshipType") == "DESCRIBES"
    ]
    if len(descriptions) != 1 or descriptions[0].get("relatedSpdxElement") != root_id:
        raise ImageSBOMError("SPDX document does not describe the container root")
    return {
        "created_at_utc": created_at,
        "package_count": len(packages),
        "file_count": len(files),
        "relationship_count": len(relationships),
    }


def image_sbom_binding(
    *,
    spdx_path: Path,
    catalog_path: Path,
    daemon_inspect_path: Path,
    image_reference: str,
    daemon_image_id: str,
    runtime_image_id: str,
    source_revision: str,
) -> dict[str, object]:
    if REVISION.fullmatch(source_revision) is None:
        raise ImageSBOMError("source revision is not a full Git SHA")
    daemon_inspect = _load_json(daemon_inspect_path)
    daemon_identity = _validate_daemon_inspect(
        daemon_inspect, image_reference, daemon_image_id
    )
    catalog = _load_json(catalog_path)
    spdx = _load_json(spdx_path)
    catalog_identity = _validate_catalog(catalog, image_reference, daemon_identity)
    runtime_image_id = _digest(runtime_image_id, "runtime container image ID")
    if runtime_image_id != daemon_identity["image_id"]:
        raise ImageSBOMError("runtime container did not use the daemon image")
    spdx_identity = _validate_spdx(spdx, image_reference, catalog_identity)
    return {
        "schema_version": 1,
        "kind": "sasori.image-sbom-binding",
        "signed": False,
        "claim": BINDING_CLAIM,
        "source_revision": source_revision,
        "image": {
            "reference": image_reference,
            "daemon_image_id": daemon_image_id,
            "runtime_container_image_id": runtime_image_id,
            "daemon_descriptor": daemon_identity["descriptor"],
            "daemon_repo_digests": daemon_identity["repo_digests"],
            "rootfs_layer_digests": daemon_identity["rootfs_layer_digests"],
            "scanner_manifest_digest": catalog_identity["manifest_digest"],
            "config_digest": catalog_identity["config_digest"],
            "architecture": catalog_identity["architecture"],
            "os": catalog_identity["os"],
        },
        "scanner": {
            "name": SCANNER_NAME,
            "version": SCANNER_VERSION,
            "catalog_schema_version": CATALOG_SCHEMA_VERSION,
        },
        "sbom": {
            "filename": spdx_path.name,
            "sha256": _sha256(spdx_path),
            "format": "SPDX-2.3",
            "package_count": spdx_identity["package_count"],
            "file_count": spdx_identity["file_count"],
            "relationship_count": spdx_identity["relationship_count"],
        },
        "catalog": {
            "filename": catalog_path.name,
            "sha256": _sha256(catalog_path),
            "artifact_count": catalog_identity["artifact_count"],
            "file_count": catalog_identity["file_count"],
            "relationship_count": catalog_identity["relationship_count"],
        },
        "created_at_utc": spdx_identity["created_at_utc"],
    }


def _write_json(path: Path, value: dict[str, object]) -> None:
    if path.exists():
        raise ImageSBOMError("image SBOM binding output already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bind a locked Syft SPDX image SBOM to one Docker image ID."
    )
    parser.add_argument("command", choices=("create", "verify"))
    parser.add_argument("--spdx", required=True, type=Path)
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--daemon-inspect", required=True, type=Path)
    parser.add_argument("--image-reference", required=True)
    parser.add_argument("--daemon-image-id", required=True)
    parser.add_argument("--runtime-image-id", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--binding", required=True, type=Path)
    options = parser.parse_args(arguments)
    try:
        expected = image_sbom_binding(
            spdx_path=options.spdx,
            catalog_path=options.catalog,
            daemon_inspect_path=options.daemon_inspect,
            image_reference=options.image_reference,
            daemon_image_id=options.daemon_image_id,
            runtime_image_id=options.runtime_image_id,
            source_revision=options.source_revision,
        )
        if options.command == "create":
            _write_json(options.binding, expected)
        elif _load_json(options.binding) != expected:
            raise ImageSBOMError("image SBOM binding does not match its subjects")
        print(json.dumps(expected, separators=(",", ":"), sort_keys=True))
        return 0
    except (OSError, ImageSBOMError) as exc:
        print(f"image SBOM verification failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
