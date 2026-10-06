"""Destination-bound PRIVATE proof and durable writes for demand-mining DATA.

The visibility receipt is the operator's existing local pii-guard receipt. Missing
or unknown visibility is a refusal, including for directories outside this tool.
This module does not change that receipt or perform a network visibility query.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import importlib.util
import stat
from urllib.parse import urlsplit
import os
from pathlib import Path
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
    from no_console import no_window_kwargs
    result = subprocess.run(["git", "-C", str(root), *map(str, args)],
                            capture_output=True, text=True, encoding="utf-8",
                            env=child_env, timeout=60, **no_window_kwargs())
    if result.returncode not in allowed:
        # Git stderr can include private file contents or credentials in a URL.
        raise RuntimeError(f"Git {args[0]} failed (exit {result.returncode}) in {root}")
    return result


def _guard_api():
    """Load the pinned kit's public companion API; missing kits are a hard failure."""
    path = Path(__file__).resolve().parents[3] / "guards/tools/data_boundary.py"
    try:
        _unaliased_path(path)
        if not path.is_file():
            raise DestinationError("initialize the pinned guards submodule before writing DATA")
        spec = importlib.util.spec_from_file_location("_demand_companion_boundary", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not callable(getattr(module, "prove_private_companion", None)):
            raise DestinationError("the pinned guards kit lacks the supported companion API")
        return module
    except (OSError, ImportError, AttributeError, ValueError) as exc:
        raise DestinationError("cannot load the pinned guards companion API") from exc


def _companion_proof(root):
    """Adapter seam for the shared verifier, using its local visibility receipt by default."""
    return _guard_api().prove_private_companion(root)


def _unaliased_path(path):
    target = Path(path).expanduser().absolute()
    if ".." in target.parts:
        raise DestinationError("DATA destination must not contain parent traversal")
    for node in (target, *target.parents):
        try:
            info = node.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 1024:
            raise DestinationError("DATA destination must not use symbolic links or reparse points")
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise DestinationError("DATA destination must not use hardlinked files")
    return target.resolve()


def _repository_root(target):
    """Find the nearest Git boundary without launching Git from a long DATA directory."""
    start = target if target.is_dir() else target.parent
    for directory in (start, *start.parents):
        if os.path.lexists(directory / ".git"):
            _unaliased_path(directory / ".git")
            return directory
    raise DestinationError("DATA destination has no containing Git worktree")


def _verify_context_environment(environment=None):
    environment = os.environ if environment is None else environment
    administrative = {"git_dir", "git_work_tree", "git_common_dir", "git_config",
                      "git_config_count", "git_config_parameters", "git_exec_path",
                      "git_proxy_command"}
    if any(key.casefold() in administrative or key.casefold().startswith(
            ("git_config_key_", "git_config_value_")) for key in environment):
        raise DestinationError("Git context or transport command override is unproved")



def _transport_proof(root):
    _verify_context_environment()
    root = _unaliased_path(root)
    try:
        proof = _companion_proof(root)
    except (RuntimeError, OSError, ValueError) as exc:
        raise DestinationError(f"PRIVATE companion proof failed: {exc}") from exc
    if Path(proof.root).resolve() != root or not proof.repositories:
        raise DestinationError("companion proof does not identify the requested repository")
    # Demand's backup explicitly pushes origin. Map those already-attested routes to the
    # shared proof's repository identities without reimplementing its transport policy.
    origin_repositories = set()
    for direction in ((), ("--push",)):
        urls = git(root, "remote", "get-url", "--all", *direction, "origin").stdout.splitlines()
        if not urls:
            raise DestinationError("backup origin has no complete transport destinations")
        for url in urls:
            repository = (urlsplit(url).path.lstrip("/") if "://" in url
                          else url.partition(":")[2]).removesuffix(".git").lower()
            if repository not in proof.repositories:
                raise DestinationError("backup origin is not in the attested companion routes")
            origin_repositories.add(repository)
    if len(origin_repositories) != 1:
        raise DestinationError("Git fetch and push destinations identify different repositories")
    return {"root": str(root), "repository": next(iter(origin_repositories)),
            "repositories": tuple(proof.repositories), "sha256": proof.signature}


# A successful companion proof launches about twenty Git processes. Long-running writers
# (daemon log appends, pool upserts) ask for it many times a minute, so a SUCCESSFUL proof
# is reused for one repository root while a cheap local signature of every proof input is
# unchanged, and never for longer than PROOF_CACHE_TTL seconds. A failed proof is never
# stored and evicts any stored success. Backup pushes always run a fresh proof.
PROOF_CACHE_TTL = 600.0
_PROOF_CACHE = {}


def clear_proof_cache():
    _PROOF_CACHE.clear()


def _stat_token(path):
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return None
    return (info.st_mtime_ns, info.st_size, info.st_ino)


def _git_admin_files(root):
    """Return the repository configuration files that select its remotes."""
    marker = Path(root) / ".git"
    if marker.is_dir():
        return [marker / "config"], ""
    # A worktree or submodule points at its administration directory from a .git file.
    text = marker.read_text(encoding="utf-8", errors="replace")
    pointer = text.strip().removeprefix("gitdir:").strip()
    admin = Path(pointer) if os.path.isabs(pointer) else Path(root) / pointer
    files = [admin / "config", admin / "config.worktree", admin / "commondir"]
    common = admin / "commondir"
    if common.is_file():
        value = common.read_text(encoding="utf-8", errors="replace").strip()
        files.append((Path(value) if os.path.isabs(value) else admin / value) / "config")
    return files, text


def _git_program():
    """The first git executable on PATH, located without launching anything."""
    names = ("git.exe", "git.cmd", "git") if os.name == "nt" else ("git",)
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        directory = directory.strip().strip('"')
        if not directory:
            continue
        for name in names:
            candidate = Path(directory) / name
            if candidate.is_file():
                return candidate
    return None


def _proof_signature(root):
    """A no-subprocess fingerprint of the inputs the companion proof reads.

    It covers the repository's own Git configuration, user and system Git configuration,
    SSH client configuration, the visibility receipt, the pinned guard kit and the whole
    process environment. Any change forces a fresh proof. Files reached only through a Git
    include are not listed; a change there is picked up when the entry expires.
    Returns None when a fingerprint cannot be taken, which disables reuse.
    """
    try:
        home = Path.home()
        admin_files, pointer = _git_admin_files(root)
        files = [*admin_files,
                 os.environ.get("GIT_CONFIG_GLOBAL") or home / ".gitconfig",
                 Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "git/config",
                 home / ".pii-guard/visibility.json", home / ".ssh/config",
                 Path(__file__).resolve().parents[3] / "guards/tools/data_boundary.py",
                 Path(__file__).resolve().parents[3] / "guards/tools/datadir.py"]
        program_data = os.environ.get("ProgramData")
        if program_data:
            files += [Path(program_data) / "Git/config", Path(program_data) / "ssh/ssh_config"]
        git_program = _git_program()
        if git_program is not None:
            for base in git_program.resolve().parents[:3]:
                files += [base / "etc/gitconfig", base / "etc/ssh/ssh_config"]
        files.append("/etc/gitconfig")
        files.append("/etc/ssh/ssh_config")
        stats = [(str(item), _stat_token(item)) for item in files]
        environment = sorted(os.environ.items())
    except (OSError, ValueError, RuntimeError):
        return None
    # The proof seams are part of the identity: replacing either forces a fresh proof.
    payload = json.dumps([str(root), pointer, stats, environment, id(_companion_proof), id(git)],
                         sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _admitted_transport(root):
    """Return a current transport proof, reusing a recent success for unchanged inputs."""
    _verify_context_environment()
    key = str(root)
    ttl = PROOF_CACHE_TTL
    if ttl <= 0:
        return _transport_proof(root)
    signature = _proof_signature(root)
    now = time.monotonic()
    if signature is not None:
        cached = _PROOF_CACHE.get(key)
        if cached is not None and cached[0] == signature and now < cached[1]:
            return dict(cached[2])
    _PROOF_CACHE.pop(key, None)
    transport = _transport_proof(root)  # raises on failure; nothing is stored
    if signature is not None and _proof_signature(root) == signature:
        _PROOF_CACHE[key] = (signature, now + ttl, dict(transport))
    return transport


def require_private(path):
    target = _unaliased_path(path)
    try:
        tool_root = Path(__file__).resolve().parents[3]
        if target.is_relative_to(tool_root):
            raise DestinationError("destination is inside the public tool tree")
        root = _repository_root(target)
        transport = _admitted_transport(root)
        if not target.is_relative_to(root):
            raise DestinationError("destination escaped its repository")
    except (RuntimeError, OSError) as exc:
        raise DestinationError(f"DATA destination refused: {target}: {exc}") from exc
    return {"path": str(target), "root": str(root), "repository": transport["repository"],
            "repositories": transport["repositories"], "visibility": "PRIVATE",
            "basis": "pinned guards companion proof", "transport": transport}


def require_private_push(root, repository):
    """Recheck every resolved fetch/push URL and current transport before backup."""
    proof = _transport_proof(root)
    if proof["repository"] != repository:
        raise DestinationError("backup destination is not the admitted PRIVATE repository")
    return proof


def data_root():
    from lib import find_config_dir
    override = os.environ.get("DEMAND_MINING_DATA_DIR")
    if override is not None:
        if not override.strip():
            raise DestinationError("DEMAND_MINING_DATA_DIR is empty")
        root = Path(override).expanduser().absolute()
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
    admission = require_private(path)
    path = Path(admission["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if require_private(path)["transport"] != admission["transport"]:
            raise DestinationError("DATA companion changed before atomic publication")
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
def file_lock(path, timeout=30.0, *, create=True):
    """OS-owned exclusive lock; process exit releases it, contention never bypasses it."""
    path = Path(require_private(path)["path"])
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b" if create else "r+b") as handle:
        # Windows can lock a byte beyond EOF. Initializing it before acquiring
        # the lock races with another opener that already owns that byte.
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
