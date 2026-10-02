#!/usr/bin/env python3
"""Restart the gateway daemon and mediate its output into current PRIVATE logs.

The child receives stdout/stderr pipes, never a log file descriptor. The supervisor
checks the destination immediately before appending each chunk, including output
without a newline. A refused destination stops the child and the supervisor.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from data_safety import data_root, require_private
from private_log import PrivateLog


def _log(logf, msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] supervisor: {msg}\n"
    logf.write(line)
    logf.flush()


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
                             stderr=subprocess.STDOUT, bufsize=0)
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
    ap = argparse.ArgumentParser(description="keep-alive supervisor for demand_bot.py")
    ap.add_argument("--config-dir", required=True, help="PRIVATE companion config directory")
    ap.add_argument("--python", default=sys.executable, help="interpreter to run the daemon with")
    ap.add_argument("--mode", choices=("dry", "shadow", "live"), default="shadow")
    ap.add_argument("--interval", default="90")
    ap.add_argument("--display-interval", default="300")
    ap.add_argument("--log-dir", default=None)
    ap.add_argument("--min-backoff", type=float, default=5.0)
    ap.add_argument("--max-backoff", type=float, default=300.0)
    args = ap.parse_args()

    os.environ["DEMAND_MINING_CONFIG"] = args.config_dir
    require_private(args.config_dir)
    args.log_dir = args.log_dir or str(data_root() / "logs")
    daemon = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demand_bot.py")
    pyw = _pythonw_for(args.python)
    env = dict(os.environ, DEMAND_MINING_CONFIG=args.config_dir, GIT_OPTIONAL_LOCKS="0")
    backoff = args.min_backoff

    with PrivateLog(os.path.join(args.log_dir, "supervisor.log")) as sup_log:
        _log(sup_log, f"start mode={args.mode} python={pyw} daemon={daemon} config={args.config_dir}")
        while True:
            require_private(args.config_dir)
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


if __name__ == "__main__":
    sys.exit(main())
