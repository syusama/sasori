import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from sasori.plugins import manifest_upgrade_diff, parse_manifest  # noqa: E402
from sasori_market.catalog import (  # noqa: E402
    CatalogError,
    local_status,
    main,
    parse_index,
    verify_local_wheel,
)


def canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def manifest(version="1.0.0", *, network=False):
    return {
        "schema_version": 1,
        "plugin_id": "com.example.echo",
        "name": "Example Echo",
        "version": version,
        "summary": "Reviewed local example.",
        "distribution": "sasori-echo",
        "execution": {
            "mode": "trusted_process",
            "entry_point_group": "sasori.plugins",
            "entry_point_name": "com.example.echo",
            "entry_point_value": "sasori_echo:register",
        },
        "permissions": {
            "filesystem_read": [],
            "filesystem_write": [],
            "network_egress": ["https:example.invalid"] if network else [],
            "host_process": [],
            "secrets": [],
        },
        "tools": [],
        "skills": [],
        "workers": [],
        "dependencies": [],
    }


def changes(before, after):
    return [
        {
            "kind": change.kind,
            "path": change.path,
            "before": change.before,
            "after": change.after,
            "requires_approval": change.requires_approval,
        }
        for change in manifest_upgrade_diff(
            parse_manifest(canonical(before)),
            parse_manifest(canonical(after)),
        )
    ]


def release(value, wheel_bytes, *, previous=None):
    encoded = canonical(value)
    manifest_hash = hashlib.sha256(encoded).hexdigest()
    upgrade = None
    if previous is not None:
        upgrade = {
            "from_manifest_sha256": hashlib.sha256(canonical(previous)).hexdigest(),
            "changes": changes(previous, value),
        }
    return {
        "plugin_id": value["plugin_id"],
        "wheel": {
            "filename": f"sasori_echo-{value['version']}-py3-none-any.whl",
            "version": value["version"],
            "size": len(wheel_bytes),
            "sha256": hashlib.sha256(wheel_bytes).hexdigest(),
        },
        "source": {
            "repository": "https://example.invalid/echo.git",
            "revision": f"source-{value['version']}",
            "wheel_origin": "https://example.invalid/echo.whl",
            "provenance": None,
        },
        "review": {
            "status": "approved",
            "reviewed_by": "sasori-maintainers",
            "reviewed_at": "2026-08-07T00:00:00Z",
            "evidence": "local-review",
        },
        "manifest": {
            "canonicalization": "sasori-manifest-json-v1",
            "size": len(encoded),
            "sha256": manifest_hash,
            "value": value,
        },
        "upgrade": upgrade,
    }


def index(*releases):
    return {
        "schema_version": 1,
        "kind": "sasori.curated-local-metadata-index",
        "releases": list(releases),
    }


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_valid_two_release_index_reuses_manifest_and_upgrade_diff(self):
        first = manifest()
        second = manifest("1.1.0", network=True)
        releases = parse_index(
            canonical(
                index(
                    release(first, b"wheel-one"),
                    release(second, b"wheel-two", previous=first),
                )
            )
        )
        self.assertEqual([item.manifest.version for item in releases], ["1.0.0", "1.1.0"])
        self.assertEqual(releases[1].manifest.permissions.network_egress, ("https:example.invalid",))
        with self.assertRaises(TypeError):
            releases[0].review["status"] = "changed"

    def test_rejects_tamper_duplicates_unknown_fields_and_false_diff(self):
        first = manifest()
        second = manifest("1.1.0", network=True)
        valid_first = release(first, b"wheel-one")
        valid_second = release(second, b"wheel-two", previous=first)
        cases = []

        tampered = json.loads(json.dumps(valid_first))
        tampered["manifest"]["value"]["summary"] = "tampered"
        cases.append(index(tampered))
        wrong_version = json.loads(json.dumps(valid_first))
        wrong_version["wheel"]["version"] = "9.9.9"
        cases.append(index(wrong_version))
        false_diff = json.loads(json.dumps(valid_second))
        false_diff["upgrade"]["changes"] = []
        cases.append(index(valid_first, false_diff))
        wrong_previous = json.loads(json.dumps(valid_second))
        wrong_previous["upgrade"]["from_manifest_sha256"] = "0" * 64
        cases.append(index(valid_first, wrong_previous))
        duplicate = index(valid_first, valid_first)
        cases.append(duplicate)
        unknown = index(valid_first)
        unknown["unknown"] = True
        cases.append(unknown)

        for value in cases:
            with self.subTest(case=list(value)), self.assertRaises(CatalogError):
                parse_index(canonical(value))
        with self.assertRaises(CatalogError):
            parse_index(b'{"schema_version":1,"schema_version":1}')

    def test_local_status_never_loads_and_wheel_verification_is_exact(self):
        wheel_bytes = b"reviewed-wheel"
        parsed = parse_index(canonical(index(release(manifest(), wheel_bytes))))
        artifact = self.root / parsed[0].wheel.filename
        artifact.write_bytes(wheel_bytes)

        class EntryPoint:
            group = "sasori.plugins"
            name = "com.example.echo"
            value = "sasori_echo:register"
            dist = SimpleNamespace(name="sasori-echo", version="1.0.0")

            def load(self):
                raise AssertionError("catalog status must never import a plugin")

        status = local_status(parsed, entry_points=[EntryPoint()], wheel_dir=self.root)
        self.assertEqual(status[0]["installed"], "distribution_metadata_match")
        self.assertEqual(status[0]["enabled"], "unknown")
        self.assertEqual(status[0]["wheel_file"], "artifact_match")
        self.assertTrue(verify_local_wheel(artifact, parsed[0]))

        enabled = {parsed[0].plugin_id: parsed[0].manifest_sha256}
        self.assertEqual(
            local_status(parsed, entry_points=[EntryPoint()], enabled=enabled)[0]["enabled"],
            "enabled_manifest_match",
        )
        artifact.write_bytes(b"tampered")
        self.assertFalse(verify_local_wheel(artifact, parsed[0]))

    def test_unknown_enabled_plugin_fails_closed_for_api_and_cli(self):
        enabled = {"com.example.not-curated": "0" * 64}
        with self.assertRaisesRegex(CatalogError, "absent from the curated catalog"):
            local_status((), enabled=enabled)

        index_path = self.root / "index.json"
        index_path.write_bytes(canonical(index()))
        enabled_path = self.root / "enabled.json"
        enabled_path.write_bytes(canonical(enabled))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(
                [
                    str(index_path),
                    "--enabled-state",
                    str(enabled_path),
                    "--json",
                ]
            )
        self.assertEqual(code, 2)
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["ok"])
        self.assertIn("absent from the curated catalog", payload["error"])

    def test_checked_in_empty_index_and_cli_validate(self):
        path = Path(__file__).parents[1] / "catalog" / "index.json"
        self.assertEqual(parse_index(path.read_bytes()), ())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main([str(path), "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["releases"], 0)


if __name__ == "__main__":
    unittest.main()
