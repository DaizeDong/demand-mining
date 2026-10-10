"""A companion ignore rule must never hide an artifact the storage contract declares versioned.

Regression for DemandMiningDaemon (2026-10-08): the companion ignored pool/** without
re-including pool/logs/, write admission refused the supervisor's own log, and the
supervisor exited on every start. All companions here are synthetic temporary repositories.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import companion_ignores
import data_safety

ROOT = Path(__file__).resolve().parents[3]
CONTRACT = json.loads((ROOT / "storage.contract.json").read_text(encoding="utf-8"))
sys.path.insert(0, str(ROOT / "scripts"))
import init_config  # noqa: E402


def companion(tmp_path, ignore_text):
    root = tmp_path / "companion"
    root.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    subprocess.run(["git", "init", "-q", str(root)], check=True, env=env)
    (root / ".gitignore").write_bytes(ignore_text.encode("utf-8"))
    return root


def hidden_ids(root):
    return {artifact_id for artifact_id, _ in companion_ignores.ignored_versioned(root, CONTRACT)}


# The shape of the companion rules before the fix: everything under pool/ ignored and only
# top-level jsonl re-included.
BROAD_POOL = "pool/**\n!pool/.gitkeep\n!pool/*.jsonl\n"


def test_broad_pool_ignore_hides_the_supervisor_log(tmp_path):
    root = companion(tmp_path, BROAD_POOL)
    assert "diagnostic_logs" in hidden_ids(root)
    assert companion_ignores.main(["--companion", str(root)]) == 1


def test_reincluding_the_log_directory_admits_it(tmp_path):
    root = companion(tmp_path, BROAD_POOL + "!pool/logs/\n!pool/logs/*\n")
    assert "diagnostic_logs" not in hidden_ids(root)


def test_the_check_agrees_with_write_admission(tmp_path):
    # The probe is only useful if it reports what admission would refuse. Admission uses the
    # same `git check-ignore --no-index`; compare on the exact log path the supervisor opens.
    root = companion(tmp_path, BROAD_POOL)
    probe = subprocess.run(["git", "-C", str(root), "check-ignore", "--no-index", "-q",
                            "pool/logs/supervisor.log"])
    assert probe.returncode == 0
    assert companion_ignores.sample_path("pool/logs/**") == "pool/logs/x"


def test_fresh_companion_template_hides_no_versioned_artifact(tmp_path):
    root = companion(tmp_path, init_config.GITIGNORE)
    assert hidden_ids(root) == set()
    assert companion_ignores.main(["--companion", str(root)]) == 0


@pytest.mark.parametrize("artifact_id", sorted(companion_ignores.EXEMPT))
def test_every_exemption_names_a_declared_versioned_artifact(artifact_id):
    rows = [a for a in CONTRACT["artifacts"] if a["artifact_id"] == artifact_id]
    assert len(rows) == 1, "stale exemption: " + artifact_id
    assert rows[0].get("persistence", "versioned") != "transient"


def test_every_sample_path_is_owned_by_its_own_artifact():
    module = data_safety._storage_contract()
    for artifact_id, path in companion_ignores.versioned_artifacts(CONTRACT):
        owners = [a["artifact_id"] for a in module.owners(CONTRACT, path)]
        assert owners == [artifact_id], (artifact_id, path, owners)


def test_unusable_companion_is_not_reported_clean(tmp_path):
    assert companion_ignores.main(["--companion", str(tmp_path / "absent")]) == 2
