"""Pytest bootstrap: put scripts/ on sys.path and pin the clock + salt for determinism."""
import os
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

# deterministic clock + stable pseudonym salt so every test run is byte-identical
os.environ.setdefault("SCHEDULE_NOW", "2026-06-25T12:00:00Z")
os.environ.setdefault("DEMAND_MINING_NOW", "2026-06-25T12:00:00Z")
os.environ.setdefault("DEMAND_MINING_PSEUDONYM_SALT", "test-salt-do-not-use-in-prod")
os.environ.setdefault("DEMAND_MINING_DRYRUN", "1")


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
