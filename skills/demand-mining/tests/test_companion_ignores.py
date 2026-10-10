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
from no_console import no_window_kwargs

ROOT = Path(__file__).resolve().parents[3]
CONTRACT = json.loads((ROOT / "storage.contract.json").read_text(encoding="utf-8"))
sys.path.insert(0, str(ROOT / "scripts"))
import init_config  # noqa: E402
import verify_config  # noqa: E402


def git_env():
    # A GIT_DIR or GIT_INDEX_FILE inherited from a hook would point these probes at
    # another repository.
    return {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}


def companion(tmp_path, ignore_text):
    root = tmp_path / "companion"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True, env=git_env(),
                   **no_window_kwargs())
    (root / ".gitignore").write_bytes(ignore_text.encode("utf-8"))
    return root


def hidden_ids(root):
    return {artifact_id for artifact_id, _ in companion_ignores.ignored_versioned(root, CONTRACT)}


def hidden_paths(root, artifact_id):
    return {path for owner, path in companion_ignores.ignored_versioned(root, CONTRACT)
            if owner == artifact_id}


# The shape of the companion rules before the fix: everything under pool/ ignored and only
# top-level jsonl re-included.
BROAD_POOL = "pool/**\n!pool/.gitkeep\n!pool/*.jsonl\n"
# The first repair (companion 5e0f71f): the directory and its direct children only.
ONE_LEVEL = BROAD_POOL + "!pool/logs/\n!pool/logs/*\n"
# Every depth below pool/logs/.
ANY_DEPTH = BROAD_POOL + "!pool/logs/\n!pool/logs/**\n"


def test_broad_pool_ignore_hides_the_supervisor_log(tmp_path):
    root = companion(tmp_path, BROAD_POOL)
    assert "diagnostic_logs" in hidden_ids(root)
    assert companion_ignores.main(["--companion", str(root)]) == 1


def test_reincluding_only_the_first_level_still_hides_nested_logs(tmp_path):
    root = companion(tmp_path, ONE_LEVEL)
    assert hidden_paths(root, "diagnostic_logs") == {"pool/logs/a/b.log"}
    assert companion_ignores.main(["--companion", str(root)]) == 1


def test_reincluding_the_log_directory_at_every_depth_admits_it(tmp_path):
    root = companion(tmp_path, ANY_DEPTH)
    assert "diagnostic_logs" not in hidden_ids(root)


@pytest.mark.parametrize("ignore_text,path,ignored", [
    (BROAD_POOL, "pool/logs/supervisor.log", True),
    (ONE_LEVEL, "pool/logs/supervisor.log", False),
    (ONE_LEVEL, "pool/logs/archive/daemon-2026-01-01.log", True),
    (ANY_DEPTH, "pool/logs/archive/daemon-2026-01-01.log", False),
])
def test_the_check_agrees_with_write_admission(tmp_path, ignore_text, path, ignored):
    # The probe is only useful if it reports what admission would refuse. Admission uses the
    # same `git check-ignore --no-index`; compare on real log paths at both probed depths.
    root = companion(tmp_path, ignore_text)
    probe = subprocess.run(["git", "-C", str(root), "check-ignore", "--no-index", "-q", path],
                           env=git_env(), **no_window_kwargs())
    assert probe.returncode == (0 if ignored else 1)
    assert ("diagnostic_logs" in hidden_ids(root)) == (ignore_text != ANY_DEPTH)


def test_double_star_is_probed_at_more_than_one_depth():
    assert companion_ignores.sample_paths("pool/logs/**") == ["pool/logs/x", "pool/logs/a/b.log"]
    assert companion_ignores.sample_path("pool/logs/**") == "pool/logs/x"
    assert companion_ignores.sample_paths("pool/runs/*/plan.json") == ["pool/runs/x/plan.json"]


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


def test_verify_config_reports_the_ignored_log(tmp_path):
    ok, detail = verify_config.ignore_policy_result(str(companion(tmp_path, ONE_LEVEL)))
    assert ok is False
    assert "diagnostic_logs (pool/logs/a/b.log)" in detail
    assert "pool/logs/x" not in detail


def test_verify_config_passes_a_clean_companion(tmp_path):
    root = companion(tmp_path, init_config.GITIGNORE)
    assert verify_config.ignore_policy_result(str(root)) == (True, "")


def test_verify_config_reports_a_broken_check_as_not_checked(tmp_path, monkeypatch):
    # None in sys.modules makes the import raise ImportError, as a missing file would.
    monkeypatch.setitem(sys.modules, "companion_ignores", None)
    root = companion(tmp_path, init_config.GITIGNORE)
    ok, detail = verify_config.ignore_policy_result(str(root))
    assert ok is False
    assert detail.startswith("not checked: ModuleNotFoundError")
    assert "companion_ignores" in detail
