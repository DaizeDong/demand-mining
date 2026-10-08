"""Exercise real source-contract ownership at production atomic write seams."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]


def generated():
    spec = importlib.util.spec_from_file_location("artifact_fixture_builder", ROOT / "tools/make_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.artifact_write_scenario()


@pytest.mark.parametrize("state", ["declared", "undeclared", "ignored"])
def test_atomic_writer_enforces_source_contract(tmp_path, monkeypatch, state):
    sample = generated()
    target = tmp_path / (sample["undeclared"] if state == "undeclared" else sample["allowed"])
    proof = SimpleNamespace(root=str(tmp_path), repositories=("example/synthetic-config",), signature="synthetic-proof")
    def read(snapshot, *args):
        ignored = state == "ignored" and args[0] == "check-ignore" and args[-1] == sample["allowed"]
        return SimpleNamespace(returncode=(0 if ignored else 1) if args[0] == "check-ignore" else 0, stdout="synthetic-head")
    boundary = SimpleNamespace(prove_private_companion=lambda *a: proof,
                               read_private_companion_git=read, GitError=RuntimeError)
    import data_safety as storage
    monkeypatch.setattr(storage, "require_private", lambda path: {
        "path": str(path), "root": str(tmp_path), "transport": "synthetic-proof"})
    monkeypatch.setattr(storage._storage_contract(), "load_boundary", lambda: boundary)
    before = set(tmp_path.rglob("*"))
    if state == "declared":
        storage.atomic_json(target, sample["content"])
        assert target.is_file()
        assert json.loads(target.read_text(encoding="utf-8")) == sample["content"]
    else:
        with pytest.raises((RuntimeError, ValueError)):
            storage.atomic_json(target, sample["content"])
        assert not target.exists()
        assert set(tmp_path.rglob("*")) == before


@pytest.mark.parametrize("change", ["publication", "temporary"])
def test_atomic_publication_rejects_changed_proof_or_temporary(tmp_path, monkeypatch, change):
    sample = generated()
    target = tmp_path / sample["allowed"]
    target.parent.mkdir(parents=True)
    before = json.dumps(sample["content"]).encode()
    target.write_bytes(before)
    proof = SimpleNamespace(root=str(tmp_path), repositories=("example/synthetic-config",), signature="before")
    boundary = SimpleNamespace(
        prove_private_companion=lambda *args: proof,
        read_private_companion_git=lambda snapshot, *args: SimpleNamespace(
            returncode=1 if args[0] == "check-ignore" else 0, stdout="synthetic-head"),
        GitError=RuntimeError)
    import data_safety as storage
    monkeypatch.setattr(storage, "require_private", lambda path: {
        "path": str(path), "root": str(tmp_path), "transport": "cached-preflight"})
    monkeypatch.setattr(storage._storage_contract(), "load_boundary", lambda: boundary)
    original = storage.authorize_write
    target_checks = []
    replacements = []

    def change_before_publication(path):
        if Path(path) == target:
            target_checks.append(path)
            if len(target_checks) == 2:
                if change == "publication":
                    proof.signature = "changed"
                else:
                    temporary, = (tmp_path / ".staging").glob("atomic-*.tmp")
                    temporary.rename(temporary.with_suffix(".held"))
                    temporary.write_bytes(before)
                    replacements.append(temporary)
        return original(path)

    monkeypatch.setattr(storage, "authorize_write", change_before_publication)
    with pytest.raises(storage.DestinationError, match="changed"):
        storage.atomic_json(target, sample["content"])
    assert target.read_bytes() == before
    if replacements:
        assert replacements[0].read_bytes() == before
