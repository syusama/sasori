from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import release_verify


LOCAL_PROVENANCE_CLAIM = (
    "local verification record; not a trusted-build attestation"
)
NOTICE_FILES = (
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "licenses/CPYTHON-3.12-LICENSE.txt",
)
MAX_JSON_BYTES = 1_000_000


class ReleaseBundleError(RuntimeError):
    pass


def _require_keys(
    value: dict[str, object], expected: set[str], label: str
) -> None:
    if set(value) != expected:
        raise ReleaseBundleError(f"{label} fields do not match schema version 1")


def _require_sha256(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ReleaseBundleError(f"{label} is not a lowercase SHA-256 digest")
    return value


def _require_created_at(value: object) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ReleaseBundleError("bundle metadata timestamp is invalid")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise ReleaseBundleError("bundle metadata timestamp is invalid") from exc
    if parsed.isoformat().replace("+00:00", "Z") != value:
        raise ReleaseBundleError("bundle metadata timestamp is not canonical")
    return value


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant: {value}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(path: Path) -> dict[str, object]:
    _require_regular_file(path)
    if path.stat().st_size > MAX_JSON_BYTES:
        raise ReleaseBundleError(f"bundle metadata is too large: {path.name}")
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ReleaseBundleError(f"bundle metadata is invalid: {path.name}") from exc
    if not isinstance(value, dict):
        raise ReleaseBundleError(f"bundle metadata must be an object: {path.name}")
    return value


def _require_regular_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise ReleaseBundleError(f"bundle input is not a regular file: {path}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def expected_inventory(version: str) -> tuple[str, ...]:
    return (
        f"sasori-{version}-py3-none-any.whl",
        f"sasori-{version}.tar.gz",
        "artifact-manifest.json",
        f"sasori-{version}.spdx.json",
        "provenance.local.json",
        *NOTICE_FILES,
    )


def _project(source_root: Path) -> dict[str, str]:
    try:
        return release_verify._project(source_root)
    except release_verify.ReleaseVerificationError as exc:
        raise ReleaseBundleError(str(exc)) from exc


def _validate_trigger_tag(project: dict[str, str], trigger_tag: str) -> str:
    try:
        validated = release_verify._validate_trigger_tag(project, trigger_tag)
    except release_verify.ReleaseVerificationError as exc:
        raise ReleaseBundleError(str(exc)) from exc
    if validated is None:  # pragma: no cover - required CLI/function contract
        raise ReleaseBundleError("release bundles require a workflow trigger tag")
    return validated


def _inventory(bundle_root: Path, version: str) -> tuple[str, ...]:
    if bundle_root.is_symlink() or not bundle_root.is_dir():
        raise ReleaseBundleError("bundle root is not a regular directory")
    files: list[str] = []
    directories: list[str] = []
    for path in bundle_root.rglob("*"):
        relative = path.relative_to(bundle_root).as_posix()
        if path.is_symlink():
            raise ReleaseBundleError(f"bundle member must not be a symlink: {relative}")
        if path.is_dir():
            directories.append(relative)
        elif path.is_file():
            files.append(relative)
        else:
            raise ReleaseBundleError(f"bundle member is not regular: {relative}")
    if sorted(directories) != ["licenses"]:
        raise ReleaseBundleError("bundle directory inventory is not exact")
    expected = sorted(expected_inventory(version))
    if sorted(files) != expected:
        raise ReleaseBundleError("bundle file inventory is not exact")
    return tuple(sorted(files))


def _artifact_map(
    manifest: dict[str, object], version: str
) -> dict[str, dict[str, object]]:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != 2:
        raise ReleaseBundleError("manifest must describe exactly wheel and sdist")
    result: dict[str, dict[str, object]] = {}
    expected_names = {
        "wheel": f"sasori-{version}-py3-none-any.whl",
        "sdist": f"sasori-{version}.tar.gz",
    }
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise ReleaseBundleError("manifest artifact is structurally invalid")
        _require_keys(
            artifact,
            {
                "kind",
                "filename",
                "size",
                "sha256",
                "member_inventory_sha256",
                "regular_file_count",
                "archive_member_count",
            },
            "manifest artifact",
        )
        kind = artifact.get("kind")
        filename = artifact.get("filename")
        size = artifact.get("size")
        regular_file_count = artifact.get("regular_file_count")
        archive_member_count = artifact.get("archive_member_count")
        if (
            not isinstance(kind, str)
            or kind not in expected_names
            or kind in result
            or filename != expected_names[kind]
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(regular_file_count, int)
            or isinstance(regular_file_count, bool)
            or regular_file_count < 0
            or not isinstance(archive_member_count, int)
            or isinstance(archive_member_count, bool)
            or archive_member_count < regular_file_count
        ):
            raise ReleaseBundleError("manifest artifact identity is invalid")
        _require_sha256(artifact.get("sha256"), "manifest artifact digest")
        _require_sha256(
            artifact.get("member_inventory_sha256"),
            "manifest artifact inventory digest",
        )
        result[kind] = artifact
    if set(result) != set(expected_names):
        raise ReleaseBundleError("manifest artifact kinds are incomplete")
    return result


def _provenance_subjects(provenance: dict[str, object]) -> dict[str, str]:
    artifacts = provenance.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != 2:
        raise ReleaseBundleError("provenance artifact subjects are invalid")
    result: dict[str, str] = {}
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise ReleaseBundleError("provenance artifact subject is invalid")
        _require_keys(
            artifact,
            {"filename", "sha256"},
            "provenance artifact subject",
        )
        filename = artifact.get("filename")
        checksum = _require_sha256(
            artifact.get("sha256"), "provenance artifact digest"
        )
        if (
            not isinstance(filename, str)
            or filename in result
        ):
            raise ReleaseBundleError("provenance artifact subject is invalid")
        result[filename] = checksum
    return result


def verify_bundle(
    bundle_root: Path, source_root: Path, trigger_tag: str
) -> dict[str, object]:
    source_root = source_root.resolve(strict=True)
    project = _project(source_root)
    trigger_tag = _validate_trigger_tag(project, trigger_tag)
    version = project["version"]
    inventory = _inventory(bundle_root, version)

    manifest = _load_json(bundle_root / "artifact-manifest.json")
    spdx = _load_json(bundle_root / f"sasori-{version}.spdx.json")
    provenance = _load_json(bundle_root / "provenance.local.json")

    _require_keys(
        manifest,
        {
            "schema_version",
            "kind",
            "project",
            "artifacts",
            "build_inputs",
            "verification",
        },
        "manifest",
    )
    manifest_project = manifest.get("project")
    verification = manifest.get("verification")
    if isinstance(manifest_project, dict):
        _require_keys(
            manifest_project,
            {
                "name",
                "version",
                "requires_python",
                "license_expression",
                "runtime_dependencies",
            },
            "manifest project",
        )
    if isinstance(verification, dict):
        _require_keys(
            verification,
            {"release_eligible", "verified_at_utc", "verifier"},
            "manifest verification",
        )
    verifier = verification.get("verifier") if isinstance(verification, dict) else None
    if isinstance(verifier, dict):
        _require_keys(verifier, {"name", "version"}, "manifest verifier")
    try:
        build_inputs = release_verify._build_inputs(source_root)
    except release_verify.ReleaseVerificationError as exc:
        raise ReleaseBundleError(str(exc)) from exc
    if (
        manifest.get("schema_version") != 1
        or manifest.get("kind") != "sasori.artifact-manifest"
        or not isinstance(manifest_project, dict)
        or manifest_project != {**project, "runtime_dependencies": []}
        or manifest.get("build_inputs") != build_inputs
        or not isinstance(verification, dict)
        or verification.get("release_eligible") is not True
        or verifier
        != {
            "name": "sasori-release-verify",
            "version": release_verify.VERIFIER_VERSION,
        }
    ):
        raise ReleaseBundleError("manifest is not an eligible project record")
    created_at = _require_created_at(verification.get("verified_at_utc"))

    artifacts = _artifact_map(manifest, version)
    artifact_subjects: dict[str, str] = {}
    for artifact in artifacts.values():
        filename = str(artifact["filename"])
        artifact_path = bundle_root / filename
        checksum = _sha256(artifact_path)
        if checksum != artifact["sha256"]:
            raise ReleaseBundleError(f"artifact digest mismatch: {filename}")
        if artifact_path.stat().st_size != artifact["size"]:
            raise ReleaseBundleError(f"artifact size mismatch: {filename}")
        artifact_subjects[filename] = checksum

    creation_info = spdx.get("creationInfo")
    if not isinstance(creation_info, dict):
        raise ReleaseBundleError("SPDX creation information is invalid")
    spdx_created_at = creation_info.get("created")
    expected_spdx = release_verify._spdx(
        project, list(artifacts.values()), build_inputs, created_at
    )
    if spdx_created_at != created_at or spdx != expected_spdx:
        raise ReleaseBundleError("SPDX document does not match schema version 1")
    try:
        release_verify._validate_spdx(spdx, list(artifacts.values()))
    except release_verify.ReleaseVerificationError as exc:
        raise ReleaseBundleError(str(exc)) from exc

    source = provenance.get("source")
    builder = provenance.get("builder")
    _require_keys(
        provenance,
        {
            "schema_version",
            "kind",
            "release_eligible",
            "reason",
            "signed",
            "claim",
            "source",
            "builder",
            "artifacts",
            "created_at_utc",
        },
        "provenance",
    )
    if isinstance(source, dict):
        _require_keys(
            source,
            {
                "state",
                "git_clean",
                "head_baseline",
                "head_is_artifact_source_identity",
                "exact_tags",
                "expected_release_tag",
                "matching_release_tag",
                "tracked_changed_count",
                "untracked_count",
                "git_status_porcelain_sha256",
                "artifact_source_binding",
                "included_source_tree_algorithm",
                "included_source_tree_sha256",
                "included_source_file_count",
                "workflow_trigger_tag",
                "workflow_trigger_tag_matches_expected",
            },
            "provenance source",
        )
    if isinstance(builder, dict):
        _require_keys(
            builder,
            {
                "python_implementation",
                "python_version",
                "platform",
                "declared_release_configuration",
            },
            "provenance builder",
        )
    current_source = release_verify._source_state(source_root, trigger_tag)
    try:
        source_tree_sha256, source_file_count = release_verify._source_tree(
            source_root
        )
    except release_verify.ReleaseVerificationError as exc:
        raise ReleaseBundleError(str(exc)) from exc
    if (
        provenance.get("schema_version") != 1
        or provenance.get("kind") != "sasori.local-provenance"
        or provenance.get("release_eligible") is not True
        or provenance.get("reason") is not None
        or provenance.get("signed") is not False
        or provenance.get("claim") != LOCAL_PROVENANCE_CLAIM
        or provenance.get("created_at_utc") != created_at
        or not isinstance(source, dict)
        or not isinstance(builder, dict)
        or any(
            not isinstance(builder.get(field), str) or not builder[field]
            for field in ("python_implementation", "python_version", "platform")
        )
        or builder.get("declared_release_configuration") != build_inputs
        or source.get("state") != "clean_release_tag_candidate"
        or source.get("git_clean") is not True
        or source.get("head_is_artifact_source_identity") is not True
        or not isinstance(source.get("exact_tags"), list)
        or trigger_tag not in source["exact_tags"]
        or source.get("expected_release_tag") != trigger_tag
        or source.get("matching_release_tag") != trigger_tag
        or source.get("tracked_changed_count") != 0
        or source.get("untracked_count") != 0
        or source.get("git_status_porcelain_sha256")
        != hashlib.sha256(b"").hexdigest()
        or source.get("artifact_source_binding")
        != "verified_to_current_working_tree"
        or source.get("workflow_trigger_tag") != trigger_tag
        or source.get("workflow_trigger_tag_matches_expected") is not True
        or current_source.get("state") != "clean_release_tag_candidate"
        or current_source.get("git_clean") is not True
        or current_source.get("matching_release_tag") != trigger_tag
        or any(source.get(key) != value for key, value in current_source.items())
        or source.get("included_source_tree_algorithm")
        != "sasori-source-tree-v1"
        or source.get("included_source_tree_sha256") != source_tree_sha256
        or source.get("included_source_file_count") != source_file_count
    ):
        raise ReleaseBundleError("local provenance release boundary is invalid")
    if _provenance_subjects(provenance) != artifact_subjects:
        raise ReleaseBundleError("provenance subjects do not match the artifacts")

    for relative in NOTICE_FILES:
        bundled = bundle_root / Path(relative)
        source = source_root / Path(relative)
        _require_regular_file(source)
        if bundled.read_bytes() != source.read_bytes():
            raise ReleaseBundleError(f"bundle notice differs from source: {relative}")

    return {
        "files": len(inventory),
        "trigger_tag": trigger_tag,
        "version": version,
    }


def assemble_bundle(
    *,
    wheel: Path,
    sdist: Path,
    metadata_root: Path,
    source_root: Path,
    output_root: Path,
    trigger_tag: str,
) -> dict[str, object]:
    source_root = source_root.resolve(strict=True)
    project = _project(source_root)
    trigger_tag = _validate_trigger_tag(project, trigger_tag)
    version = project["version"]
    inputs = {
        f"sasori-{version}-py3-none-any.whl": wheel,
        f"sasori-{version}.tar.gz": sdist,
        "artifact-manifest.json": metadata_root / "artifact-manifest.json",
        f"sasori-{version}.spdx.json": metadata_root
        / f"sasori-{version}.spdx.json",
        "provenance.local.json": metadata_root / "provenance.local.json",
        **{relative: source_root / Path(relative) for relative in NOTICE_FILES},
    }
    if set(inputs) != set(expected_inventory(version)):
        raise ReleaseBundleError("bundle assembly inventory is invalid")
    for source in inputs.values():
        _require_regular_file(source)
    if output_root.exists():
        if output_root.is_symlink() or not output_root.is_dir():
            raise ReleaseBundleError("bundle output is not a regular directory")
        if any(output_root.iterdir()):
            raise ReleaseBundleError("bundle output must be empty")
    else:
        output_root.mkdir(parents=True)
    for relative, source in inputs.items():
        destination = output_root / Path(relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".tmp")
        shutil.copyfile(source, temporary)
        temporary.replace(destination)
    return verify_bundle(output_root, source_root, trigger_tag)


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Assemble or verify an exact-tag Sasori release candidate bundle."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    assemble = subparsers.add_parser("assemble")
    assemble.add_argument("--wheel", required=True, type=Path)
    assemble.add_argument("--sdist", required=True, type=Path)
    assemble.add_argument("--metadata-root", required=True, type=Path)
    assemble.add_argument("--source-root", required=True, type=Path)
    assemble.add_argument("--output", required=True, type=Path)
    assemble.add_argument("--trigger-tag", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--bundle-root", required=True, type=Path)
    verify.add_argument("--source-root", required=True, type=Path)
    verify.add_argument("--trigger-tag", required=True)
    options = parser.parse_args(arguments)
    try:
        if options.command == "assemble":
            result = assemble_bundle(
                wheel=options.wheel,
                sdist=options.sdist,
                metadata_root=options.metadata_root,
                source_root=options.source_root,
                output_root=options.output,
                trigger_tag=options.trigger_tag,
            )
        else:
            result = verify_bundle(
                options.bundle_root, options.source_root, options.trigger_tag
            )
        print(json.dumps(result, separators=(",", ":"), sort_keys=True))
        return 0
    except (OSError, ReleaseBundleError) as exc:
        print(f"release bundle verification failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
