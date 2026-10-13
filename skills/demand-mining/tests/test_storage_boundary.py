"""Generated native DATA boundary regressions; reproduce with tools/make_fixtures.py."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest

import data_safety
import dedup
from private_log import PrivateLog, PrivateLogError
from native_fixture import native_reminder, require_reminder_source, run_native_reminder

REMINDER = None


@pytest.fixture
def native_storage(tmp_path, monkeypatch):
    reminder_source = require_reminder_source()
    for key in list(os.environ):
        if key.upper().startswith(('GIT_', 'DEMAND_MINING_', 'SCHEDULE_')):
            monkeypatch.delenv(key, raising=False)
    profile = tmp_path / 'profile'
    profile.mkdir()
    monkeypatch.setenv('HOME', str(profile))
    monkeypatch.setenv('USERPROFILE', str(profile))
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', os.devnull)
    monkeypatch.setenv('GIT_TERMINAL_PROMPT', '0')

    def no_network(*args, **kwargs):
        raise AssertionError('native storage regression attempted network access')
    monkeypatch.setattr(socket.socket, 'connect', no_network)

    def repository(path):
        path.mkdir(parents=True)
        subprocess.run(['git', '-C', str(path), 'init', '-q'], capture_output=True, check=True)
        subprocess.run(['git', '-C', str(path), '-c', 'user.name=Synthetic Fixture',
                        '-c', 'user.email=user1@example.com', 'commit', '--allow-empty',
                        '-m', 'Initialize synthetic companion'], capture_output=True, check=True)
        return path

    monkeypatch.setattr(sys.modules[__name__], 'REMINDER', native_reminder(tmp_path, reminder_source))
    private = repository(tmp_path/'private')
    other = repository(tmp_path/'other')
    return private, other, repository


@pytest.mark.parametrize('verb', ['init', 'list'])
def test_missing_shared_database_is_never_created(native_storage, verb):
    private, _, _ = native_storage
    missing = private / 'missing-shared.db'
    client = dedup.LedgerClient(cmd=[sys.executable, '-B', str(REMINDER)],
                                db_path=str(missing), product_id='synthetic-product')
    with pytest.raises(ValueError, match='existing'):
        client._run(verb, [])
    assert not missing.exists()


def test_explicitly_initialized_shared_database_remains_usable(native_storage):
    private, _, _ = native_storage
    db = private / 'existing-shared.db'
    command = [sys.executable, '-B', str(REMINDER)]
    initialized = run_native_reminder(command, '--db', str(db), 'init')
    assert json.loads(initialized.stdout)['ok'] is True and db.stat().st_size > 0
    client = dedup.LedgerClient(cmd=command, db_path=str(db), product_id='synthetic-product')
    assert client.init()['ok'] is True
    assert client.list_active() == []


def test_private_log_cannot_append_through_hardlink_to_another_file(native_storage):
    private, other, _ = native_storage
    original = other / 'published.txt'
    original.write_text('synthetic other fixture\n', encoding='utf-8')
    alias = private / 'daemon.log'
    os.link(original, alias)
    before = original.read_bytes()
    assert alias.stat().st_nlink == 2
    with pytest.raises(PrivateLogError):
        with PrivateLog(alias) as stream:
            stream.write('synthetic runtime observation\n')
    assert original.read_bytes() == before


def test_plain_private_log_still_appends(native_storage):
    private, _, _ = native_storage
    log = private / 'pool/logs/daemon.log'
    with PrivateLog(log) as stream:
        stream.write('synthetic first\n')
        stream.write('synthetic second\n')
    assert log.read_text(encoding='utf-8') == 'synthetic first\nsynthetic second\n'


def test_repeated_atomic_write_supports_a_long_data_path(native_storage):
    private, _, _ = native_storage
    directory = private / 'pool/logs'
    while len(str(directory/'state.json')) < 321:
        directory = directory / ('a' * 60)
    target = directory / 'state.json'
    assert len(str(target)) >= 321
    data_safety.atomic_json(target, {'synthetic_attempt': 1})
    data_safety.atomic_json(target, {'synthetic_attempt': 2})
    assert json.loads(target.read_text(encoding='utf-8')) == {'synthetic_attempt': 2}


def test_nearest_nested_repository_remains_authoritative(native_storage, monkeypatch):
    private, _, repository = native_storage
    nested = repository(private/'nested')
    target = nested/'pool/runs/synthetic/state.json'
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('path checks start no process'))
    data_safety.atomic_json(target, {'synthetic': True})
    checked = data_safety.check_destination(target)
    assert Path(checked['root']) == nested.resolve()
    assert not (nested / '.staging').exists() or list((nested / '.staging').iterdir()) == []


def junction(target, alias):
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(alias))
    else:
        alias.symlink_to(target, target_is_directory=True)


@pytest.mark.parametrize("verb", ["init", "list"])
def test_empty_database_is_not_an_initialized_shared_store(native_storage, verb):
    private, _, _ = native_storage
    database = private / "empty.db"
    database.touch()
    client = dedup.LedgerClient(cmd=[sys.executable, "-B", str(REMINDER)], db_path=str(database))
    with pytest.raises(ValueError, match="existing"):
        client._run(verb, [])
    assert database.stat().st_size == 0


def test_private_directory_alias_is_refused(native_storage):
    private, _, _ = native_storage
    target = private / "actual"
    target.mkdir()
    alias = private / "alias"
    junction(target, alias)
    with pytest.raises(data_safety.DestinationError):
        data_safety.atomic_json(alias / "state.json", {"synthetic": True})
    assert not (target / "state.json").exists()


def test_dangling_git_boundary_cannot_fall_back_to_outer_private_repo(native_storage):
    private, _, _ = native_storage
    nested = private / "nested"
    nested.mkdir()
    target = private / "temporary-admin"
    target.mkdir()
    junction(target, nested / ".git")
    target.rmdir()
    with pytest.raises(data_safety.DestinationError):
        data_safety.atomic_json(nested / "state.json", {"synthetic": True})
    assert not (nested / "state.json").exists()
