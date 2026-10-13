"""Atomic DATA writes are admitted by local path checks only and start no process."""
import json
from pathlib import Path
import subprocess

import pytest

import data_safety as storage

ROOT = Path(__file__).resolve().parents[3]
CONTENT = {"schema_version": 1, "label": "Synthetic transaction"}


@pytest.fixture
def no_process(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("path checks start no process"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("path checks start no process"))


def companion(tmp_path):
    root = tmp_path / "synthetic-companion"
    (root / ".git").mkdir(parents=True)
    return root


def test_atomic_write_inside_a_companion_worktree(tmp_path, no_process):
    root = companion(tmp_path)
    target = root / "pool/runs/synthetic/state.json"
    storage.atomic_json(target, CONTENT)
    assert json.loads(target.read_text(encoding="utf-8")) == CONTENT
    assert list((root / ".staging").iterdir()) == []
    assert storage.check_destination(target) == {"path": str(target.resolve()), "root": str(root.resolve())}


@pytest.mark.parametrize("where", ["unversioned", "source", "git-metadata", "parent-traversal"])
def test_refused_destination_writes_nothing(tmp_path, no_process, where):
    root = companion(tmp_path)
    target = {
        "unversioned": tmp_path / "loose/state.json",
        "source": ROOT / "pool/synthetic-state.json",
        "git-metadata": root / ".git/synthetic-state.json",
        "parent-traversal": root / "pool/../../loose/state.json",
    }[where]
    before = set(tmp_path.rglob("*"))
    with pytest.raises(storage.DestinationError):
        storage.atomic_json(target, CONTENT)
    assert set(tmp_path.rglob("*")) == before
    assert not (ROOT / "pool").exists()


def test_existing_hardlinked_destination_is_refused(tmp_path, no_process):
    root = companion(tmp_path)
    original = root / "synthetic-original.json"
    original.write_text("synthetic retained", encoding="utf-8")
    target = root / "pool/state.json"
    target.parent.mkdir()
    target.hardlink_to(original)
    with pytest.raises(storage.DestinationError, match="hardlinked"):
        storage.atomic_json(target, CONTENT)
    assert original.read_text(encoding="utf-8") == "synthetic retained"


def test_uninitialized_data_root_is_a_write_error(monkeypatch):
    import config_paths
    monkeypatch.setattr(config_paths, "data_directory", lambda: None)
    with pytest.raises(storage.DestinationError, match="uninitialized"):
        storage.data_root()
