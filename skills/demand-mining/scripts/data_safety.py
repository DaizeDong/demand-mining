"""Local DATA path checks and durable writes for demand-mining.

Where DATA lives is decided by companion resolution (config_paths), never an in-repo
fallback: an uninitialized companion is a write error. A destination is refused only by
local path checks that start no process: it must not be inside this public source tree,
must not traverse parents or filesystem aliases (symbolic links, reparse points,
hardlinked files) and must sit inside a Git worktree so the DATA stays versioned.
Repository visibility is not checked at runtime; publication safety is enforced at
commit time by the Guards hooks and CI.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import stat
import os
from pathlib import Path
import subprocess
import time

SOURCE_ROOT = Path(__file__).resolve().parents[3]


class DestinationError(RuntimeError):
    """The output destination failed the local DATA path checks."""


GIT_TIMEOUT_SECONDS = 60.0


def git(root, *args, env=None, allowed=(0,)):
    """Run one Git command in the companion (backup only); never used to check a destination."""
    child_env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    for key, value in (env or {}).items():
        if value is None:
            child_env.pop(key, None)
        else:
            child_env[key] = value
    child_env["GIT_OPTIONAL_LOCKS"] = "0"
    from no_console import no_window_kwargs
    try:
        result = subprocess.run(["git", "-C", str(root), *map(str, args)],
                                capture_output=True, text=True, encoding="utf-8",
                                env=child_env, timeout=GIT_TIMEOUT_SECONDS, **no_window_kwargs())
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Git {args[0] if args else ''} timed out after "
                           f"{GIT_TIMEOUT_SECONDS:g}s in {root}") from exc
    if result.returncode not in allowed:
        # Git stderr can include private file contents or credentials in a URL.
        raise RuntimeError(f"Git {args[0]} failed (exit {result.returncode}) in {root}")
    return result


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
    """Find the nearest Git boundary without launching Git."""
    start = target if target.is_dir() else target.parent
    for directory in (start, *start.parents):
        if os.path.lexists(directory / ".git"):
            _unaliased_path(directory / ".git")
            return directory
    raise DestinationError("DATA destination has no containing Git worktree; "
                           "initialize the PRIVATE companion repository")


def check_destination(path):
    """Return {"path", "root"} for a DATA destination that passes the local path checks."""
    target = _unaliased_path(path)
    try:
        if (target.is_relative_to(SOURCE_ROOT) or SOURCE_ROOT.is_relative_to(target)
                or ".git" in {part.casefold() for part in target.parts}):
            raise DestinationError("destination is inside the public tool tree or Git metadata")
        root = _repository_root(target)
        if root.is_relative_to(SOURCE_ROOT) or SOURCE_ROOT.is_relative_to(root):
            raise DestinationError("destination repository is the public tool repository")
    except (RuntimeError, OSError) as exc:
        raise DestinationError(f"DATA destination refused: {target}: {exc}") from exc
    return {"path": str(target), "root": str(root)}


def data_root():
    from config_paths import data_directory
    root = data_directory()
    if root is None:
        raise DestinationError("DATA is uninitialized; configure a PRIVATE companion repository")
    check_destination(root)
    return root


def sync_directory(path):
    if os.name != "nt":
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic_bytes(path, content):
    admission = check_destination(path)
    path = Path(admission["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    import uuid
    name = Path(admission["root"]) / ".staging" / ("atomic-" + uuid.uuid4().hex + ".tmp")
    name.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    temporary_identity = os.fstat(fd)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        candidate = name.lstat()
        if not stat.S_ISREG(candidate.st_mode) or candidate.st_nlink != 1 or not os.path.samestat(temporary_identity, candidate):
            raise DestinationError("DATA temporary file changed identity before publication")
        os.replace(name, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(name):
            try:
                candidate = _unaliased_path(name).lstat()
                if not os.path.samestat(temporary_identity, candidate):
                    raise DestinationError("temporary cleanup refused after identity changed")
                os.unlink(name)
            except (OSError, RuntimeError):
                import sys
                print("DATA temporary cleanup refused; inspect the retained staging file", file=sys.stderr)
    return path


def atomic_json(path, value):
    return atomic_bytes(path, (json.dumps(value, sort_keys=True, ensure_ascii=False,
                                          indent=2) + "\n").encode("utf-8"))


@contextmanager
def file_lock(path, timeout=30.0, *, create=True):
    """OS-owned exclusive lock; process exit releases it, contention never bypasses it."""
    path = Path(check_destination(path)["path"])
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
