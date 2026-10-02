"""Destination-bound PRIVATE proof and durable writes for demand-mining DATA.

The visibility receipt is the operator's existing local pii-guard receipt. Missing
or unknown visibility is a refusal, including for directories outside this tool.
This module does not change that receipt or perform a network visibility query.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import hashlib
import importlib.util
import stat
from urllib.parse import urlsplit
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time


class DestinationError(RuntimeError):
    """The actual output destination has no verified PRIVATE repository."""


def git(root, *args, env=None, allowed=(0,), transport_proof=None):
    child_env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    if env:
        if any(key not in {"GIT_INDEX_FILE", "GIT_OPTIONAL_LOCKS", "GIT_LITERAL_PATHSPECS"} for key in env):
            raise DestinationError("Git child environment override is not an admitted backup option")
        for key, value in env.items():
            if value is None:
                child_env.pop(key, None)
            else:
                child_env[key] = value
    child_env["GIT_OPTIONAL_LOCKS"] = "0"
    _verify_context_environment(child_env)
    if args and args[0] == "push":
        if transport_proof is None:
            raise DestinationError("push requires an admitted transport proof")
        current = require_private_push(root, transport_proof["repository"])
        if current != transport_proof:
            raise DestinationError("Git transport changed after backup admission")
    result = subprocess.run(["git", "-C", str(root), *map(str, args)],
                            capture_output=True, text=True, encoding="utf-8",
                            env=child_env, timeout=60)
    if result.returncode not in allowed:
        # Git stderr can include private file contents or credentials in a URL.
        raise RuntimeError(f"Git {args[0]} failed (exit {result.returncode}) in {root}")
    return result


def _visibility(repository):
    receipt = Path.home() / ".pii-guard" / "visibility.json"
    try:
        data = json.loads(receipt.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise DestinationError("PRIVATE visibility receipt is missing or invalid") from exc
    if not isinstance(data, dict) or not isinstance(data.get("_verified"), dict):
        raise DestinationError("PRIVATE visibility receipt has an invalid schema")
    value = data.get(repository)
    verified = data["_verified"].get(repository, {})
    if not isinstance(verified, dict):
        raise DestinationError("PRIVATE visibility receipt has an invalid repository proof")
    return value if verified.get("v") == value else "UNKNOWN"


def _verify_context_environment(environment=None):
    environment = os.environ if environment is None else environment
    administrative = {"git_dir", "git_work_tree", "git_common_dir", "git_config",
                      "git_config_count", "git_config_parameters", "git_exec_path",
                      "git_proxy_command"}
    if any(key.casefold() in administrative or key.casefold().startswith(
            ("git_config_key_", "git_config_value_")) for key in environment):
        raise DestinationError("Git context or transport command override is unproved")


def _verify_ssh_transport():
    """Use the shared guard's static SSH policy without executing an SSH command."""
    path = Path(__file__).resolve().parents[3] / "guards/tools/data_boundary.py"
    guidance = "Cannot prove SSH transport; initialize the current guards kit or use canonical GitHub HTTPS"
    try:
        for node in (path, *path.parents):
            info = node.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 1024:
                raise DestinationError(guidance)
        if not path.is_file() or path.stat().st_nlink != 1:
            raise DestinationError(guidance)
        spec = importlib.util.spec_from_file_location("_demand_ssh_boundary", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        verifier = getattr(module, "_ssh_configuration_problem", None)
        if not callable(verifier) or verifier() is not None:
            raise DestinationError(guidance)
    except (OSError, ValueError, UnicodeError, ImportError, AttributeError, RuntimeError) as exc:
        raise DestinationError(guidance) from exc


def _canonical_destination(url):
    if not isinstance(url, str) or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url):
        raise DestinationError("origin is not a canonical GitHub repository")
    if url.startswith("git@github.com:"):
        name, ssh = url[len("git@github.com:"):], True
    else:
        try:
            parsed = urlsplit(url)
            ssh = parsed.scheme == "ssh"
            if (parsed.scheme not in ("https", "ssh") or parsed.hostname != "github.com"
                    or parsed.username != ("git" if ssh else None) or parsed.password
                    or parsed.query or parsed.fragment or parsed.port is not None
                    or parsed.netloc.endswith(":")):
                raise DestinationError("origin is not a canonical GitHub repository")
            name = parsed.path.removeprefix("/")
        except ValueError as exc:
            raise DestinationError("origin is not a canonical GitHub repository") from exc
    name = name.removesuffix(".git")
    if (not re.fullmatch(r"[a-zA-Z0-9-]+/[a-zA-Z0-9_.-]+", name)
            or name.rsplit("/", 1)[-1] in (".", "..")):
        raise DestinationError("origin is not a canonical GitHub repository")
    return name.lower(), ssh


def _transport_proof(root):
    _verify_context_environment()
    actual_root = Path(git(root, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if actual_root != Path(root).resolve():
        raise DestinationError("Git repository context differs from the admitted root")
    raw = git(root, "config", "--null", "--list").stdout
    entries = []
    remote_names = {"origin"}
    for entry in raw.split("\0"):
        if entry:
            key, separator, value = entry.partition("\n")
            if not key or "." not in key or any(ord(char) < 32 for char in key):
                raise DestinationError("Git transport configuration is invalid")
            section, _, remainder = key.partition(".")
            name, separator_name, _ = remainder.rpartition(".")
            if section.casefold() == "remote" and separator_name:
                if not name or name.startswith("-") or any(char.isspace() for char in name):
                    raise DestinationError("Git remote name is unproved")
                remote_names.add(name)
            entries.append((key.casefold(), value if separator else None))
    keys = {key for key, value in entries}
    if any(key.startswith("remote.") and key.rsplit(".", 1)[-1] in
           {"vcs", "uploadpack", "receivepack"} for key in keys):
        raise DestinationError("Git remote transport command is unproved")
    for key, value in entries:
        selector = key == "remote.pushdefault" or (
            key.startswith("branch.") and key.rsplit(".", 1)[-1] in {"remote", "pushremote"})
        if selector and value not in remote_names:
            raise DestinationError("Git push selection is unproved; configure a named PRIVATE remote")
    destinations = {}
    repositories = set()
    origin_repository = None
    for name in sorted(remote_names):
        urls = []
        for direction in ((), ("--push",)):
            values = git(root, "remote", "get-url", "--all", *direction, name).stdout.splitlines()
            if not values or any(not value for value in values):
                raise DestinationError("Git remote has no complete transport destinations")
            urls.extend(values)
        destinations[name] = urls
        remote_repositories = set()
        for url in urls:
            repository, ssh = _canonical_destination(url)
            remote_repositories.add(repository)
            if ssh:
                if keys & {"core.sshcommand", "ssh.variant"} or any(
                        key.casefold() in {"git_ssh", "git_ssh_command", "git_ssh_variant"} for key in os.environ):
                    raise DestinationError("Git SSH transport override is unproved")
                _verify_ssh_transport()
            else:
                _verify_https_transport(entries)
        if name == "origin":
            if len(remote_repositories) != 1:
                raise DestinationError("Git fetch and push destinations identify different repositories")
            origin_repository = next(iter(remote_repositories))
        repositories.update(remote_repositories)
    for repository in sorted(repositories):
        visibility = _visibility(repository)
        if visibility != "PRIVATE":
            raise DestinationError(f"repository {repository} visibility is {visibility or 'UNKNOWN'}")
    environment = {key: value for key, value in os.environ.items()
                   if key.casefold().startswith(("git_", "http", "https", "all_proxy", "no_proxy", "ssl_", "curl_"))}
    for key in list(environment):
        if key.casefold() in {"git_index_file", "git_literal_pathspecs", "git_optional_locks"}:
            del environment[key]
    binding = {"root": str(Path(root).resolve()), "repository": origin_repository,
               "destinations": destinations, "configuration": entries, "environment": environment}
    fingerprint = hashlib.sha256(json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"root": binding["root"], "repository": origin_repository, "sha256": fingerprint}



_HTTPS_PERFORMANCE_KEYS = {
    "version", "maxrequests", "minsessions", "postbuffer", "lowspeedlimit",
    "lowspeedtime", "keepaliveidle", "keepaliveinterval", "keepalivecount",
}
_HTTPS_PERFORMANCE_ENV = {"git_http_low_speed_limit", "git_http_low_speed_time"}


def _verify_https_environment():
    """Refuse known overrides before HTTPS transport or visibility proof."""
    for name in os.environ:
        key = name.casefold()
        if (key in {"http_proxy", "https_proxy", "all_proxy", "curl_ca_bundle",
                    "ssl_cert_file", "ssl_cert_dir", "curl_ssl_backend", "git_exec_path"}
                or key.startswith(("git_ssl_", "git_proxy_ssl_"))
                or (key.startswith("git_http_") and key not in _HTTPS_PERFORMANCE_ENV)):
            raise DestinationError("Companion HTTPS transport has an unproved environment override")


def _verify_https_transport(config_entries):
    """Refuse unproved Git HTTPS settings, including every URL-scoped occurrence."""
    _verify_https_environment()
    # Keep every occurrence: an empty final value cannot erase an earlier override.
    for key, value in config_entries:
        key = key.casefold()
        option = key.rsplit(".", 1)[-1]
        if key.startswith("remote.") and option.startswith("proxy"):
            raise DestinationError("Companion HTTPS transport has an unproved remote proxy")
        if not key.startswith("http."):
            continue
        if option in _HTTPS_PERFORMANCE_KEYS:
            continue
        if option == "sslverify" and value is not None and value.strip().casefold() in {"true", "yes", "on", "1"}:
            continue
        raise DestinationError("Companion HTTPS transport has an unproved HTTP configuration override")

def _repository(url):
    return _canonical_destination(url)[0]


def require_private(path):
    _verify_context_environment()
    target = Path(path).expanduser().resolve()
    probe = target if target.is_dir() else target.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        root = Path(git(probe, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
        transport = _transport_proof(root)
        repository = transport["repository"]
        visibility = "PRIVATE"
        tool_root = Path(__file__).resolve().parents[3]
        if target.is_relative_to(tool_root):
            raise DestinationError("destination is inside the public tool tree")
        if not target.is_relative_to(root):
            raise DestinationError("destination escaped its repository")
        if visibility != "PRIVATE":
            raise DestinationError(f"repository {repository} visibility is {visibility or 'UNKNOWN'}")
    except (RuntimeError, OSError) as exc:
        raise DestinationError(f"DATA destination refused: {target}: {exc}") from exc
    return {"path": str(target), "root": str(root), "repository": repository,
            "visibility": "PRIVATE", "basis": "local verified visibility receipt plus effective Git transport",
            "transport": transport}


def require_private_push(root, repository):
    """Recheck every resolved fetch/push URL and current transport before backup."""
    proof = _transport_proof(Path(root).resolve())
    if proof["repository"] != repository:
        raise DestinationError("backup destination is not the admitted PRIVATE repository")
    return proof


def data_root():
    from lib import find_config_dir
    override = os.environ.get("DEMAND_MINING_DATA_DIR")
    if override is not None:
        if not override.strip():
            raise DestinationError("DEMAND_MINING_DATA_DIR is empty")
        root = Path(override).expanduser().resolve()
    else:
        config = find_config_dir()
        if config is None:
            raise DestinationError("DATA is uninitialized; configure a PRIVATE companion repository")
        root = config / "pool"
    require_private(root)
    return root


def sync_directory(path):
    if os.name != "nt":
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic_bytes(path, content):
    path = Path(require_private(path)["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return path


def atomic_json(path, value):
    return atomic_bytes(path, (json.dumps(value, sort_keys=True, ensure_ascii=False,
                                          indent=2) + "\n").encode("utf-8"))


@contextmanager
def file_lock(path, timeout=30.0):
    """OS-owned exclusive lock; process exit releases it, contention never bypasses it."""
    path = Path(require_private(path)["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"DATA lock unavailable: {path}") from exc
                time.sleep(0.05)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
