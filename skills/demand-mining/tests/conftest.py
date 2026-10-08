"""Pytest bootstrap: put scripts/ on sys.path and pin the clock + salt for determinism."""
import os
import sys
import types
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

# deterministic clock + stable pseudonym salt so every test run is byte-identical
os.environ.setdefault("SCHEDULE_NOW", "2026-06-25T12:00:00Z")
os.environ.setdefault("DEMAND_MINING_NOW", "2026-06-25T12:00:00Z")
os.environ.setdefault("DEMAND_MINING_PSEUDONYM_SALT", "test-salt-do-not-use-in-prod")
os.environ.setdefault("DEMAND_MINING_DRYRUN", "1")


def _unexpected_model_call(*args, **kwargs):
    raise AssertionError("offline tests must replace llmcall.call explicitly")


# Bot modules import this interface during collection. Keep model execution denied
# even on machines where a real provider installation is available.
_llmcall = types.ModuleType("llmcall")
_llmcall.call = _unexpected_model_call
sys.modules["llmcall"] = _llmcall


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_private_proof_cache():
    """A reused PRIVATE proof must never leak from one test's synthetic companion into another."""
    def clear():
        module = sys.modules.get("data_safety")
        if module is not None and hasattr(module, "clear_proof_cache"):
            module.clear_proof_cache()
    clear()
    yield
    clear()
