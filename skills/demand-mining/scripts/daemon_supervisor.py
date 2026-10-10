#!/usr/bin/env python3
"""Restart the gateway daemon and mediate its output into current PRIVATE logs.

The child receives stdout/stderr pipes, never a log file descriptor. The supervisor
checks the destination immediately before appending each chunk, including output
without a newline. A refused destination stops the child at once and nothing is
written or restarted until a fresh admission succeeds. Admission is retried with
backoff for --admission-retry-seconds; a refusal that outlasts that window stops
the supervisor. One failed proof (a Git process that could not start, a file read
that raced a writer) therefore no longer leaves the daemon down until the next logon.

pythonw.exe discards stderr, so a fatal exit is recorded before the process ends: in
supervisor.log when that destination is still admitted, otherwise in a local note
outside every repository (%LOCALAPPDATA%/demand-mining/supervisor-exit.log). The note
holds only the exception type and message, never daemon output.

The supervisor and the daemon run under pythonw.exe, which has no console. Both make
CREATE_NO_WINDOW the default for their own children (see no_console.py), and the daemon
itself is launched with that flag, so no Git proof or helper process opens a window.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from no_console import install_no_console_window_default, no_window_kwargs
from data_safety import DestinationError, data_root, require_private
from private_log import PrivateLog, PrivateLogError

# The supervisor log path, once known, so a fatal exit can be recorded there.
_SUPERVISOR_LOG = None


def _log(logf, msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] supervisor: {msg}\n"
    logf.write(line)
    logf.flush()


def _describe(exc) -> str:
    """One line naming the exception and its causes; bounded, without daemon output."""
    parts, seen = [], set()
    while exc is not None and id(exc) not in seen and len(parts) < 4:
        seen.add(id(exc))
        parts.append(f"{type(exc).__name__}: {exc}")
        exc = exc.__cause__ or exc.__context__
    return " ".join(" <- ".join(parts).split())[:2000]


def _exit_note_path():
    """A per-user location outside every repository, or None where none is defined."""
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return os.path.join(base, "demand-mining", "supervisor-exit.log")
    base = os.environ.get("XDG_STATE_HOME")
    if base:
        return os.path.join(base, "demand-mining", "supervisor-exit.log")
    return None


def _record_fatal(reason: str) -> str:
    """Leave the exit reason on disk before the process ends; returns where it went."""
    if _SUPERVISOR_LOG is not None:
        try:
            with PrivateLog(_SUPERVISOR_LOG) as log:
                _log(log, f"supervisor exiting: {reason}")
            return "log"
        except BaseException:
            pass  # the destination itself is refused; fall back to the local note
    path = _exit_note_path()
    if path is None:
        return "none"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as note:
            note.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] supervisor exiting "
                       f"(pid {os.getpid()}): {reason}\n")
        return "note"
    except Exception:
        return "none"


def _pythonw_for(python_path: str) -> str:
    """Prefer a windowless interpreter for the daemon."""
    if python_path.lower().endswith("python.exe"):
        cand = python_path[:-len("python.exe")] + "pythonw.exe"
        if os.path.isfile(cand):
            return cand
    return python_path


def _stop_child(child):
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


def _run_child(argv, env, log):
    require_private(log.path)
    child = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, bufsize=0, **no_window_kwargs())
    try:
        while True:
            chunk = child.stdout.read(65536)
            if not chunk:
                break
            log.write(chunk)
        return child.wait()
    except BaseException:
        _stop_child(child)
        raise
    finally:
        child.stdout.close()


def main() -> int:
    install_no_console_window_default()
    ap = argparse.ArgumentParser(description="keep-alive supervisor for demand_bot.py")
    ap.add_argument("--config-dir", required=True, help="PRIVATE companion config directory")
    ap.add_argument("--python", default=sys.executable, help="interpreter to run the daemon with")
    ap.add_argument("--mode", choices=("dry", "shadow", "live"), default="shadow")
    ap.add_argument("--interval", default="90")
    ap.add_argument("--display-interval", default="300")
    ap.add_argument("--log-dir", default=None)
    ap.add_argument("--min-backoff", type=float, default=5.0)
    ap.add_argument("--max-backoff", type=float, default=300.0)
    ap.add_argument("--admission-retry-seconds", type=float, default=3600.0,
                    help="how long a refused log or config destination is re-proved before the supervisor stops")
    args = ap.parse_args()
    global _SUPERVISOR_LOG

    os.environ["DEMAND_MINING_CONFIG"] = args.config_dir
    daemon = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demand_bot.py")
    pyw = _pythonw_for(args.python)
    env = dict(os.environ, DEMAND_MINING_CONFIG=args.config_dir, GIT_OPTIONAL_LOCKS="0")
    backoff = args.min_backoff
    retry_window = float(getattr(args, "admission_retry_seconds", 3600.0))
    sup_path = None
    sup_log = None
    started_once = False
    refused_since, refusal, admission_delay = None, None, args.min_backoff

    while True:
        try:
            # Nothing below writes or launches before require_private and a fresh log admission.
            require_private(args.config_dir)
            if sup_path is None:
                args.log_dir = args.log_dir or str(data_root() / "logs")
                sup_path = _SUPERVISOR_LOG = os.path.join(args.log_dir, "supervisor.log")
            if sup_log is None:
                sup_log = PrivateLog(sup_path)
            if not started_once:
                _log(sup_log, f"start mode={args.mode} python={pyw} daemon={daemon} config={args.config_dir}")
                started_once = True
            if refused_since is not None:
                _log(sup_log, f"admission recovered after {time.time() - refused_since:.0f}s; "
                              f"refused with: {refusal}")
                refused_since, refusal, admission_delay = None, None, args.min_backoff
            stamp = time.strftime("%Y-%m-%d")
            with PrivateLog(os.path.join(args.log_dir, f"daemon-{stamp}.log"), binary=True) as child_log:
                started = time.time()
                try:
                    rc = _run_child(
                        [pyw, "-u", "-X", "utf8", "-B", daemon, "--mode", args.mode,
                         "--interval", str(args.interval), "--display-interval", str(args.display_interval)],
                        env, child_log)
                except Exception as exc:
                    rc = -1
                    _log(sup_log, f"launch failed: {exc!r}")
            ran = time.time() - started
            _log(sup_log, f"daemon exited rc={rc} after {ran:.0f}s")
            backoff = args.min_backoff if ran > 60 else min(args.max_backoff, backoff * 2)
            _log(sup_log, f"restarting in {backoff:.0f}s")
            time.sleep(backoff)
        except (PrivateLogError, DestinationError) as exc:
            # The child is already stopped (_run_child stops it on any exception). Drop the
            # refused log handle; a new one is admitted from scratch before anything is written.
            sup_log = None
            refusal = _describe(exc)
            now = time.time()
            if refused_since is None:
                refused_since = now
            if now - refused_since >= retry_window:
                raise PrivateLogError(
                    f"admission refused for {now - refused_since:.0f}s, giving up: {refusal}") from exc
            time.sleep(admission_delay)
            admission_delay = min(args.max_backoff, admission_delay * 2)


def run() -> int:
    """Entry point: record any fatal exit before pythonw discards it."""
    try:
        return main()
    except BaseException as exc:
        _record_fatal(_describe(exc))
        raise


if __name__ == "__main__":
    sys.exit(run())
