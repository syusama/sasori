import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import sasori.sqlite_store as sqlite_store  # noqa: E402
from sasori import SQLiteStore, StoreError  # noqa: E402


_PROBE = """
import sys
from sasori import ConcurrentRunError, SQLiteStore
try:
    store = SQLiteStore(sys.argv[1])
except ConcurrentRunError:
    raise SystemExit(17)
store.close()
"""


class ProcessLockTests(unittest.TestCase):
    def test_file_store_owner_lock_crosses_process_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "owner.sqlite3"
            owner = SQLiteStore(path)
            self.addCleanup(owner.close)
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
            blocked = subprocess.run(
                [sys.executable, "-B", "-c", _PROBE, str(path)],
                env=environment,
                check=False,
                timeout=5,
            )
            self.assertEqual(blocked.returncode, 17)
            owner.close()
            reopened = subprocess.run(
                [sys.executable, "-B", "-c", _PROBE, str(path)],
                env=environment,
                check=False,
                timeout=5,
            )
            self.assertEqual(reopened.returncode, 0)

    def test_context_manager_and_closed_state(self):
        with self.assertRaisesRegex(RuntimeError, "^boom$"):
            with SQLiteStore() as store:
                self.assertFalse(store.closed)
                raise RuntimeError("boom")
        self.assertTrue(store.closed)
        store.close()
        with self.assertRaisesRegex(StoreError, "^store is closed$"):
            store.load("missing")
        with self.assertRaisesRegex(StoreError, "^store is closed$"):
            store.__enter__()

    def test_close_detaches_lock_before_cleanup(self):
        store = SQLiteStore()
        process_lock = object()
        store._process_lock = process_lock
        with mock.patch.object(
            sqlite_store,
            "_release_process_lock",
            side_effect=OSError("unlock failed"),
        ) as release:
            with self.assertRaisesRegex(OSError, "^unlock failed$"):
                store.close()
            store.close()
        self.assertTrue(store.closed)
        release.assert_called_once_with(process_lock)

    def test_failed_initialization_attempts_all_cleanup(self):
        database = mock.Mock()
        database.execute.side_effect = RuntimeError("setup failed")
        database.close.side_effect = OSError("close failed")
        process_lock = object()
        with (
            mock.patch.object(
                sqlite_store, "_acquire_process_lock", return_value=process_lock
            ),
            mock.patch.object(
                sqlite_store.sqlite3, "connect", return_value=database
            ),
            mock.patch.object(sqlite_store, "_release_process_lock") as release,
        ):
            with self.assertRaisesRegex(RuntimeError, "^setup failed$") as failure:
                SQLiteStore("state.sqlite3")
        database.close.assert_called_once_with()
        release.assert_called_once_with(process_lock)
        self.assertIn("close failed", failure.exception.__notes__[0])


if __name__ == "__main__":
    unittest.main()
