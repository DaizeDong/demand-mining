"""One companion owns the selected settings and Demand's pool layout."""
import pytest

import lib
import data_safety


def test_alias_selects_settings_and_pool(tmp_path, monkeypatch):
    for key in ("DEMAND_MINING_CONFIG", "DEMAND_MINING_CONFIG_DIR", "DEMAND_MINING_DATA_DIR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DEMAND_MINING_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(data_safety, "require_private", lambda path: {"path": str(path)})
    assert lib.find_config_dir() == tmp_path
    assert data_safety.data_root() == tmp_path / "pool"


def test_data_only_selects_pool_parent_as_settings(tmp_path, monkeypatch):
    pool = tmp_path / "pool"
    pool.mkdir()
    for key in ("DEMAND_MINING_CONFIG", "DEMAND_MINING_CONFIG_DIR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DEMAND_MINING_DATA_DIR", str(pool))
    assert lib.find_config_dir() == tmp_path


def test_config_and_data_cannot_mix_companions(tmp_path, monkeypatch):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    (second / "pool").mkdir(parents=True)
    monkeypatch.setenv("DEMAND_MINING_CONFIG", str(first))
    monkeypatch.setenv("DEMAND_MINING_DATA_DIR", str(second / "pool"))
    with pytest.raises((ValueError, RuntimeError), match="same companion|conflict"):
        lib.find_config_dir()
