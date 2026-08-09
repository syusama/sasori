import hashlib
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from sasori import Event, Message  # noqa: E402
from sasori.sqlite_store import SQLiteStore  # noqa: E402
from sasori_artifacts import (  # noqa: E402
    MAX_ARTIFACT_BYTES,
    ArtifactConflict,
    ArtifactCorrupted,
    ArtifactInvalid,
    ArtifactLimit,
    ArtifactNotFound,
    ArtifactStore,
    artifact_projection,
    validate_artifact_id,
)


class ArtifactStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.database = self.base / "runs.sqlite3"
        self.root = self.base / "artifacts"
        self.run_store = SQLiteStore(self.database)
        self.start_run("run-1")
        self.store = ArtifactStore(self.run_store, self.root)

    def tearDown(self):
        self.store.close()
        self.run_store.close()
        self.temp.cleanup()

    def start_run(self, run_id):
        self.run_store.start(
            run_id,
            (Message("user", "artifact fixture"),),
            Event("run.started", run_id, 0),
        )

    def blob(self, ref):
        key = f"sha256/{ref.content_sha256[:2]}/{ref.content_sha256}"
        return self.root / "blobs" / Path(*key.split("/"))

    def test_public_ref_and_durable_event_expose_only_stable_metadata(self):
        content = b'{"ok":true}'
        ref = self.store.put(
            "run-1",
            content,
            artifact_id="artifact-report",
            declared_filename="report.html",
            declared_media_type="text/html",
        )

        expected = {
            "version": 1,
            "artifact_id": "artifact-report",
            "run_id": "run-1",
            "content_sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
            "filename": "report.html",
            "media_type": "application/json",
            "created_seq": 2,
        }
        self.assertEqual(artifact_projection(ref), expected)
        self.assertTrue(ref.can_preview_text)
        self.assertEqual(self.store.list("run-1"), (ref,))
        self.assertEqual(self.store.get("run-1", ref.artifact_id).content, content)

        events = self.run_store.stored_events("run-1")
        self.assertEqual([item.seq for item in events], [1, 2])
        self.assertEqual(events[-1].event.type, "artifact.available")
        self.assertEqual(dict(events[-1].event.data["artifact"]), expected)
        serialized = str(dict(events[-1].event.data["artifact"]))
        for forbidden in (
            "storage_key",
            str(self.root),
            "declared_media_type",
            "previewable",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_restart_preserves_ref_event_and_verified_bytes_without_duplication(self):
        text = "永恒的机关"
        first = self.store.put(
            "run-1",
            text.encode("utf-8"),
            artifact_id="artifact-final",
            declared_filename="result.md",
            declared_media_type="text/markdown",
        )
        before = self.run_store.stored_events("run-1")
        self.store.close()
        self.run_store.close()

        self.run_store = SQLiteStore(self.database)
        self.store = ArtifactStore(self.run_store, self.root)
        reopened = self.store.get("run-1", "artifact-final")
        self.assertEqual(reopened.ref, first)
        self.assertEqual(reopened.content.decode("utf-8"), text)
        self.assertEqual(reopened.ref.media_type, "text/plain; charset=utf-8")
        self.assertEqual(self.run_store.stored_events("run-1"), before)
        self.assertEqual(
            self.store.put(
                "run-1",
                text.encode("utf-8"),
                artifact_id="artifact-final",
                declared_filename="result.md",
                declared_media_type="text/markdown",
            ),
            first,
        )
        self.assertEqual(len(self.run_store.stored_events("run-1")), 2)

    def test_ids_are_idempotent_and_same_digest_can_have_two_run_associations(self):
        options = {
            "artifact_id": "artifact-fixed",
            "declared_filename": "result.txt",
            "declared_media_type": "text/plain",
        }
        first = self.store.put("run-1", b"same", **options)
        self.assertEqual(self.store.put("run-1", b"same", **options), first)
        with self.assertRaises(ArtifactConflict):
            self.store.put("run-1", b"different", **options)

        self.start_run("run-2")
        with self.assertRaises(ArtifactConflict):
            self.store.put("run-2", b"same", **options)
        second = self.store.put(
            "run-2", b"same", declared_filename="result.txt"
        )
        self.assertNotEqual(second.artifact_id, first.artifact_id)
        self.assertEqual(second.content_sha256, first.content_sha256)
        self.assertEqual(self.blob(second), self.blob(first))
        self.assertEqual(len(self.store.list("run-1")), 1)
        self.assertEqual(len(self.store.list("run-2")), 1)

    def test_cross_run_missing_tampered_and_deleted_blobs_fail_closed(self):
        first = self.store.put(
            "run-1",
            b"verified",
            artifact_id="artifact-a",
            declared_filename="a.txt",
        )
        self.start_run("run-2")
        with self.assertRaises(ArtifactNotFound):
            self.store.get("run-2", first.artifact_id)
        with self.assertRaises(ArtifactNotFound):
            self.store.get("run-1", "artifact-missing")

        first_path = self.blob(first)
        first_path.chmod(0o666)
        first_path.write_bytes(b"tamper!!")
        with self.assertRaises(ArtifactCorrupted):
            self.store.get("run-1", first.artifact_id)
        with self.assertRaises(ArtifactCorrupted):
            self.store.put(
                "run-2",
                b"verified",
                artifact_id="artifact-b",
                declared_filename="b.txt",
            )
        self.assertEqual(first_path.read_bytes(), b"tamper!!")

        second = self.store.put(
            "run-1",
            b"second",
            artifact_id="artifact-c",
            declared_filename="c.bin",
        )
        second_path = self.blob(second)
        second_path.chmod(0o666)
        second_path.unlink()
        with self.assertRaises(ArtifactCorrupted):
            self.store.get("run-1", second.artifact_id)

    def test_input_bounds_database_checks_and_append_only_triggers(self):
        for value in ("", "../x", "x/y", "x y", "a" * 129, None):
            with self.subTest(value=value), self.assertRaises(ArtifactInvalid):
                validate_artifact_id(value)
        for filename in ("", ".", "../x", "a/b", "a\\b", "a\n.txt"):
            with self.subTest(filename=filename), self.assertRaises(ArtifactInvalid):
                self.store.put("run-1", b"x", declared_filename=filename)
        for media_type in ("text", "text/plain\r\nx: y", "x" * 128):
            with self.subTest(media_type=media_type), self.assertRaises(ArtifactInvalid):
                self.store.put(
                    "run-1",
                    b"x",
                    declared_filename="x.txt",
                    declared_media_type=media_type,
                )
        with self.assertRaises(ArtifactInvalid):
            self.store.put("../run", b"x", declared_filename="x.txt")
        with self.assertRaises(ArtifactInvalid):
            self.store.put("run-1", bytearray(b"x"), declared_filename="x.txt")
        with self.assertRaises(ArtifactInvalid):
            self.store.put(
                "run-1",
                b"x" * (MAX_ARTIFACT_BYTES + 1),
                declared_filename="large.bin",
            )

        ref = self.store.put("run-1", b"x", declared_filename="x.txt")
        with self.assertRaises(sqlite3.DatabaseError):
            self.run_store._db.execute(  # type: ignore[union-attr]
                "UPDATE artifacts SET size = 0 WHERE artifact_id = ?",
                (ref.artifact_id,),
            )
        with self.assertRaises(sqlite3.DatabaseError):
            self.run_store._db.execute(  # type: ignore[union-attr]
                "DELETE FROM artifacts WHERE artifact_id = ?", (ref.artifact_id,)
            )

        before = self.run_store.stored_events("run-1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.run_store.register_artifact(
                "run-1",
                artifact_id="artifact-invalid-row",
                content_sha256="not-a-sha256",
                size=0,
                declared_filename="bad.bin",
                declared_media_type=None,
                detected_media_type="application/octet-stream",
            )
        self.assertEqual(self.run_store.stored_events("run-1"), before)
        self.assertIsNone(
            self.run_store.stored_artifact("run-1", "artifact-invalid-row")
        )

    def test_store_has_one_owner_and_v3_migrates_to_v4(self):
        with self.assertRaises(ArtifactConflict):
            ArtifactStore(self.run_store, self.root)
        self.store.close()
        self.run_store.close()

        database = sqlite3.connect(self.database)
        database.execute("DROP TABLE artifacts")
        database.execute("PRAGMA user_version = 3")
        database.commit()
        database.close()

        self.run_store = SQLiteStore(self.database)
        self.store = ArtifactStore(self.run_store, self.root)
        self.assertEqual(
            self.run_store._db.execute("PRAGMA user_version").fetchone()[0],  # type: ignore[union-attr]
            4,
        )
        columns = {
            row[1]
            for row in self.run_store._db.execute(  # type: ignore[union-attr]
                "PRAGMA table_info(artifacts)"
            )
        }
        self.assertNotIn("storage_key", columns)
        ref = self.store.put("run-1", b"after", declared_filename="after.txt")
        self.assertEqual(ref.created_seq, 2)

    def test_artifact_limit_rolls_back_event_and_association(self):
        for index in range(128):
            self.store.put(
                "run-1",
                bytes([index]),
                declared_filename=f"artifact-{index}.bin",
            )
        before = self.run_store.stored_events("run-1")
        with self.assertRaises(ArtifactLimit):
            self.store.put("run-1", b"overflow", declared_filename="overflow.bin")
        self.assertEqual(len(self.store.list("run-1")), 128)
        self.assertEqual(self.run_store.stored_events("run-1"), before)

    def test_symlink_blob_is_rejected_when_platform_allows_it(self):
        ref = self.store.put("run-1", b"plain", declared_filename="plain.txt")
        path = self.blob(ref)
        target = path.with_name("target")
        path.chmod(0o666)
        path.rename(target)
        try:
            os.symlink(target.name, path)
        except (OSError, NotImplementedError) as error:
            target.rename(path)
            self.skipTest(f"symbolic links are unavailable: {error}")
        with self.assertRaises(ArtifactCorrupted):
            self.store.get("run-1", ref.artifact_id)


if __name__ == "__main__":
    unittest.main()
