import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from sasori import Harness, ModelReply, SQLiteStore  # noqa: E402
from sasori.app import AppLoadError, load_harness  # noqa: E402


class FinalModel:
    async def complete(self, messages, tools):
        return ModelReply(content="done")


class AppTests(unittest.TestCase):
    def setUp(self):
        self.module = types.ModuleType("sasori_test_app")
        sys.modules[self.module.__name__] = self.module

    def tearDown(self):
        sys.modules.pop(self.module.__name__, None)

    def test_loads_only_a_harness_bound_to_the_supplied_store(self):
        store = SQLiteStore()
        self.module.create = lambda supplied: Harness(FinalModel(), store=supplied)
        self.assertIs(load_harness("sasori_test_app:create", store).store, store)

        other = SQLiteStore()
        self.module.create = lambda supplied: Harness(FinalModel(), store=other)
        with self.assertRaises(AppLoadError):
            load_harness("sasori_test_app:create", store)
        other.close()
        self.module.create = lambda supplied: object()
        with self.assertRaises(AppLoadError):
            load_harness("sasori_test_app:create", store)
        store.close()

    def test_rejects_invalid_or_missing_factories(self):
        store = SQLiteStore()
        for spec in ("", "module", "../bad:create", "sasori_test_app:missing"):
            with self.assertRaises(AppLoadError):
                load_harness(spec, store)
        store.close()


if __name__ == "__main__":
    unittest.main()
