from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping

from sasori import PluginManifest, PluginLoadError
from sasori._provider_common import json_values_equal, strict_json_loads
from sasori.plugins import discover_plugins, manifest_upgrade_diff, parse_manifest


_MAX_INDEX_BYTES = 2 * 1024 * 1024
_MAX_RELEASES = 4096
_SHA256 = "0123456789abcdef"


class CatalogError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class WheelMetadata:
    filename: str
    version: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class CatalogRelease:
    plugin_id: str
    wheel: WheelMetadata
    manifest: PluginManifest
    manifest_sha256: str
    source: Mapping[str, object]
    review: Mapping[str, str]


def _object(value: object, fields: set[str], name: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != fields:
        raise CatalogError(f"{name} has missing or unknown fields")
    return value


def _text(value: object, name: str, limit: int = 4096) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > limit
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise CatalogError(f"{name} is invalid")
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name, 64)
    if len(text) != 64 or any(character not in _SHA256 for character in text):
        raise CatalogError(f"{name} is not a lowercase SHA-256 value")
    return text


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise CatalogError("catalog data is not canonical JSON") from None


def _change_projection(change: object) -> dict[str, object]:
    return {
        "kind": getattr(change, "kind"),
        "path": getattr(change, "path"),
        "before": getattr(change, "before"),
        "after": getattr(change, "after"),
        "requires_approval": getattr(change, "requires_approval"),
    }


def parse_index(value: str | bytes) -> tuple[CatalogRelease, ...]:
    try:
        raw = value.encode("utf-8") if isinstance(value, str) else value
    except (AttributeError, UnicodeEncodeError):
        raise CatalogError("catalog index must be UTF-8 JSON") from None
    if len(raw) > _MAX_INDEX_BYTES:
        raise CatalogError("catalog index exceeds the size limit")
    try:
        decoded = strict_json_loads(raw)
    except (UnicodeDecodeError, ValueError, TypeError, RecursionError):
        raise CatalogError("catalog index is not strict JSON") from None
    root = _object(decoded, {"schema_version", "kind", "releases"}, "catalog index")
    if root["schema_version"] != 1 or root["kind"] != "sasori.curated-local-metadata-index":
        raise CatalogError("catalog index identity is unsupported")
    entries = root["releases"]
    if not isinstance(entries, list) or len(entries) > _MAX_RELEASES:
        raise CatalogError("catalog releases must be a bounded array")

    releases = []
    identities: set[tuple[str, str]] = set()
    prior: dict[str, list[tuple[str, PluginManifest]]] = {}
    for entry in entries:
        data = _object(
            entry,
            {"plugin_id", "wheel", "source", "review", "manifest", "upgrade"},
            "catalog release",
        )
        plugin_id = _text(data["plugin_id"], "plugin_id", 128)
        wheel_data = _object(data["wheel"], {"filename", "version", "size", "sha256"}, "wheel")
        filename = _text(wheel_data["filename"], "wheel.filename", 512)
        if Path(filename).name != filename or not filename.endswith(".whl"):
            raise CatalogError("wheel.filename must be a wheel basename")
        version = _text(wheel_data["version"], "wheel.version", 128)
        size = wheel_data["size"]
        if type(size) is not int or not 0 < size <= 10 * 1024 * 1024 * 1024:
            raise CatalogError("wheel.size is invalid")
        wheel = WheelMetadata(filename, version, size, _sha256(wheel_data["sha256"], "wheel.sha256"))

        source = _object(
            data["source"],
            {"repository", "revision", "wheel_origin", "provenance"},
            "source",
        )
        for field in ("repository", "revision", "wheel_origin"):
            _text(source[field], f"source.{field}", 8192)
        if source["provenance"] is not None:
            _text(source["provenance"], "source.provenance", 8192)

        review = _object(
            data["review"],
            {"status", "reviewed_by", "reviewed_at", "evidence"},
            "review",
        )
        if review["status"] != "approved":
            raise CatalogError("only approved releases belong in the curated index")
        review_values = {
            field: _text(review[field], f"review.{field}", 4096)
            for field in ("status", "reviewed_by", "reviewed_at", "evidence")
        }

        envelope = _object(
            data["manifest"],
            {"canonicalization", "size", "sha256", "value"},
            "manifest envelope",
        )
        if envelope["canonicalization"] != "sasori-manifest-json-v1":
            raise CatalogError("manifest canonicalization is unsupported")
        canonical_manifest = _canonical(envelope["value"])
        manifest_size = envelope["size"]
        manifest_hash = _sha256(envelope["sha256"], "manifest.sha256")
        if type(manifest_size) is not int or manifest_size != len(canonical_manifest):
            raise CatalogError("manifest.size does not match canonical bytes")
        if hashlib.sha256(canonical_manifest).hexdigest() != manifest_hash:
            raise CatalogError("manifest.sha256 does not match canonical bytes")
        try:
            manifest = parse_manifest(canonical_manifest)
        except Exception as error:
            raise CatalogError(f"plugin manifest is invalid: {type(error).__name__}") from None
        if manifest.plugin_id != plugin_id or manifest.version != version:
            raise CatalogError("catalog, wheel, and manifest identities do not match")
        identity = (plugin_id, version)
        if identity in identities:
            raise CatalogError("catalog release identity is duplicated")
        identities.add(identity)

        previous = prior.get(plugin_id, [])
        upgrade = data["upgrade"]
        if not previous:
            if upgrade is not None:
                raise CatalogError("the first plugin release cannot have an upgrade diff")
        else:
            upgrade_data = _object(upgrade, {"from_manifest_sha256", "changes"}, "upgrade")
            previous_hash = _sha256(upgrade_data["from_manifest_sha256"], "upgrade.from_manifest_sha256")
            candidate = next((item for item in previous if item[0] == previous_hash), None)
            if candidate is None:
                raise CatalogError("upgrade does not reference an earlier release of this plugin")
            expected_changes = [
                _change_projection(change)
                for change in manifest_upgrade_diff(candidate[1], manifest)
            ]
            if not json_values_equal(upgrade_data["changes"], expected_changes):
                raise CatalogError("upgrade changes do not match the manifest diff")
        prior.setdefault(plugin_id, []).append((manifest_hash, manifest))
        releases.append(
            CatalogRelease(
                plugin_id,
                wheel,
                manifest,
                manifest_hash,
                MappingProxyType(dict(source)),
                MappingProxyType(review_values),
            )
        )
    return tuple(releases)


def verify_local_wheel(path: Path, release: CatalogRelease) -> bool:
    try:
        if path.is_symlink() or not path.is_file() or path.name != release.wheel.filename:
            return False
        if path.stat().st_size != release.wheel.size:
            return False
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest() == release.wheel.sha256
    except OSError:
        return False


def local_status(
    releases: tuple[CatalogRelease, ...],
    *,
    entry_points: Iterable[object] | None = None,
    enabled: Mapping[str, str] | None = None,
    wheel_dir: Path | None = None,
) -> tuple[dict[str, str], ...]:
    catalog_plugin_ids = {release.plugin_id for release in releases}
    if enabled is not None:
        unknown_enabled = sorted(set(enabled) - catalog_plugin_ids)
        if unknown_enabled:
            raise CatalogError(
                "enabled state contains plugin IDs absent from the curated catalog: "
                + ", ".join(unknown_enabled)
            )
    discovered = {item.plugin_id: item for item in discover_plugins(entry_points=entry_points)}
    result = []
    for release in releases:
        installed = discovered.get(release.plugin_id)
        if installed is None:
            installed_state = "not_installed"
        elif (
            installed.distribution == release.manifest.distribution
            and installed.version == release.manifest.version
            and installed.entry_point_group == release.manifest.execution.entry_point_group
            and installed.entry_point_value == release.manifest.execution.entry_point_value
        ):
            installed_state = "distribution_metadata_match"
        else:
            installed_state = "installed_metadata_mismatch"
        if enabled is None:
            enabled_state = "unknown"
        elif release.plugin_id not in enabled:
            enabled_state = "disabled"
        elif enabled[release.plugin_id] == release.manifest_sha256:
            enabled_state = "enabled_manifest_match"
        else:
            enabled_state = "enabled_manifest_mismatch"
        if wheel_dir is None:
            wheel_state = "not_checked"
        else:
            wheel_path = wheel_dir / release.wheel.filename
            wheel_state = (
                "artifact_match"
                if verify_local_wheel(wheel_path, release)
                else "mismatch" if wheel_path.exists() else "missing"
            )
        result.append(
            {
                "plugin_id": release.plugin_id,
                "version": release.manifest.version,
                "installed": installed_state,
                "enabled": enabled_state,
                "wheel_file": wheel_state,
                "effective_access": "FULL HOST PROCESS PRIVILEGES",
            }
        )
    return tuple(result)


def _enabled_state(path: Path) -> dict[str, str]:
    try:
        raw = path.read_bytes()
        decoded = strict_json_loads(raw)
    except (OSError, UnicodeDecodeError, ValueError, TypeError, RecursionError):
        raise CatalogError("enabled state is not strict JSON") from None
    if not isinstance(decoded, dict):
        raise CatalogError("enabled state must be an object")
    return {
        _text(plugin_id, "enabled plugin_id", 128): _sha256(value, "enabled manifest hash")
        for plugin_id, value in decoded.items()
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sasori-catalog")
    parser.add_argument("index", type=Path)
    parser.add_argument("--enabled-state", type=Path)
    parser.add_argument("--wheel-dir", type=Path)
    parser.add_argument("--json", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        releases = parse_index(arguments.index.read_bytes())
        enabled = _enabled_state(arguments.enabled_state) if arguments.enabled_state else None
        statuses = local_status(
            releases,
            enabled=enabled,
            wheel_dir=arguments.wheel_dir,
        )
    except (OSError, CatalogError, PluginLoadError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 2
    payload = {"ok": True, "releases": len(releases), "status": statuses}
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True) if arguments.json else f"Validated {len(releases)} curated release(s).")
    if arguments.wheel_dir and any(item["wheel_file"] != "artifact_match" for item in statuses):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
