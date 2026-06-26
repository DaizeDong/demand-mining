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
