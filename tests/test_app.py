import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from sasori import (  # noqa: E402
    MAX_APP_ID_BYTES,
    Harness,
    ModelReply,
    SQLiteStore,
    is_valid_app_id,
)
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

    def test_public_app_id_contract_preserves_trailing_punctuation_aliases(self):
        self.assertEqual(MAX_APP_ID_BYTES, 64)
        for value in ("a", "custom-", "custom.", "custom_", "0" + "-" * 63):
            with self.subTest(value=value):
                self.assertTrue(is_valid_app_id(value))
        for value in (
            None,
            "",
            "UPPER",
            "-leading",
            "with space",
            "é",
            "0" + "-" * 64,
        ):
            with self.subTest(value=value):
                self.assertFalse(is_valid_app_id(value))

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

    def test_loader_passes_app_alias_only_to_factories_that_accept_it(self):
        store = SQLiteStore()
        received = []

        def alias_factory(supplied, *, app_id):
            received.append(app_id)
            return Harness(FinalModel(), store=supplied)

        self.module.create = alias_factory
        harness = load_harness(
            "sasori_test_app:create", store, app_id="custom-research"
        )
        self.assertIs(harness.store, store)
        self.assertEqual(received, ["custom-research"])

        self.module.create = lambda supplied: Harness(FinalModel(), store=supplied)
        self.assertIs(
            load_harness(
                "sasori_test_app:create", store, app_id="ignored-by-legacy-factory"
            ).store,
            store,
        )
        store.close()

    def test_rejects_invalid_or_missing_factories(self):
        store = SQLiteStore()
        for spec in ("", "module", "../bad:create", "sasori_test_app:missing"):
            with self.assertRaises(AppLoadError):
                load_harness(spec, store)
        store.close()


if __name__ == "__main__":
    unittest.main()
