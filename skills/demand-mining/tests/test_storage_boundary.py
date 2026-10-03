"""Generated native DATA boundary regressions; reproduce with tools/make_fixtures.py."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import datetime

import pytest

import data_safety
import dedup
from private_log import PrivateLog, PrivateLogError

REMINDER = Path.home() / '.claude/skills/schedule-reminder/scripts/reminder.py'


@pytest.fixture
def native_storage(tmp_path, monkeypatch):
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
    receipts = {}

    def repository(path, name, visibility):
        path.mkdir(parents=True)
        for args in [('init', '-q'), ('config', 'remote.origin.url',
                                     'https://github.com/example-owner/'+name+'.git')]:
            subprocess.run(['git', '-C', str(path), *args], capture_output=True, check=True)
        receipts['example-owner/'+name] = visibility
        target = profile / '.pii-guard/visibility.json'
        target.parent.mkdir(exist_ok=True)
        target.write_text(json.dumps({**receipts, '_refreshed': datetime.datetime.now(datetime.timezone.utc).isoformat(), '_verified': {
            key: {'v': value} for key, value in receipts.items()}}), encoding='utf-8')
        return path

    private = repository(tmp_path/'private', 'private-vault', 'PRIVATE')
    public = repository(tmp_path/'public', 'public-tool', 'PUBLIC')
    return private, public, repository


@pytest.mark.parametrize('verb', ['init', 'list'])
def test_missing_shared_database_is_never_created(native_storage, verb):
    private, _, _ = native_storage
    assert REMINDER.is_file(), 'native regression requires the staged schedule-reminder CLI'
    missing = private / 'missing-shared.db'
    client = dedup.LedgerClient(cmd=[sys.executable, '-B', str(REMINDER)],
                                db_path=str(missing), product_id='synthetic-product')
    with pytest.raises(ValueError, match='existing'):
        client._run(verb, [])
    assert not missing.exists()


def test_explicitly_initialized_shared_database_remains_usable(native_storage):
    private, _, _ = native_storage
    assert REMINDER.is_file(), 'native regression requires the staged schedule-reminder CLI'
    db = private / 'existing-shared.db'
    command = [sys.executable, '-B', str(REMINDER)]
    initialized = subprocess.run([*command, '--db', str(db), 'init'],
                                 capture_output=True, text=True, check=True)
    assert json.loads(initialized.stdout)['ok'] is True and db.stat().st_size > 0
    client = dedup.LedgerClient(cmd=command, db_path=str(db), product_id='synthetic-product')
    assert client.init()['ok'] is True
    assert client.list_active() == []


def test_private_log_cannot_append_through_hardlink_to_public_file(native_storage):
    private, public, _ = native_storage
    original = public / 'published.txt'
    original.write_text('synthetic public fixture\n', encoding='utf-8')
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
    log = private / 'daemon.log'
    with PrivateLog(log) as stream:
        stream.write('synthetic first\n')
        stream.write('synthetic second\n')
    assert log.read_text(encoding='utf-8') == 'synthetic first\nsynthetic second\n'


def test_repeated_atomic_write_supports_a_long_data_path(native_storage):
    private, _, _ = native_storage
    directory = private
    while len(str(directory/'state.json')) < 321:
        directory = directory / ('a' * 60)
    target = directory / 'state.json'
    assert len(str(target)) >= 321
    data_safety.atomic_json(target, {'synthetic_attempt': 1})
    data_safety.atomic_json(target, {'synthetic_attempt': 2})
    assert json.loads(target.read_text(encoding='utf-8')) == {'synthetic_attempt': 2}


@pytest.mark.parametrize('visibility', ['PUBLIC', 'PRIVATE'])
def test_nearest_nested_repository_remains_authoritative(native_storage, visibility):
    private, _, repository = native_storage
    nested = repository(private/'nested', 'nested-vault', visibility)
    target = nested/'deep/state.json'
    if visibility == 'PUBLIC':
        with pytest.raises(data_safety.DestinationError):
            data_safety.atomic_json(target, {'synthetic': True})
        assert not target.exists()
    else:
        data_safety.atomic_json(target, {'synthetic': True})
        proof = data_safety.require_private(target)
        assert Path(proof['root']) == nested
        assert proof['repository'] == 'example-owner/nested-vault'


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
    with pytest.raises(data_safety.DestinationError):
        data_safety.require_private_push(alias, "example-owner/private-vault")


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
