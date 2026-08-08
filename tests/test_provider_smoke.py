import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))
SPEC = importlib.util.spec_from_file_location(
    "sasori_provider_smoke", ROOT / "scripts" / "provider_smoke.py"
)
provider_smoke = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = provider_smoke
SPEC.loader.exec_module(provider_smoke)

from sasori import ModelReply, ToolCall  # noqa: E402


class SmokeModel:
    async def complete(self, messages, tools):
        if not any(message.role == "tool" for message in messages):
            return ModelReply(
                tool_calls=(
                    ToolCall("smoke-call", "echo", {"text": provider_smoke.MARKER}),
                )
            )
        return ModelReply(content=provider_smoke.FINAL)


class ProviderSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_turn_tool_contract(self):
        await provider_smoke.smoke(SmokeModel())


if __name__ == "__main__":
    unittest.main()
