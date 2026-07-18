#!/usr/bin/env python3
"""Keep-alive supervisor for the live-tap gateway daemon (demand_bot.py).

A gateway daemon must run 24/7. discord.py already reconnects through network blips on its own, so
this layer exists only for the harder failures: a hard process crash, an OOM, a machine sleep/wake
that kills the socket for good. The supervisor (re)launches demand_bot.py, waits, and on any exit
restarts it with capped exponential backoff, teeing everything to a daily log. Register it once as a
Windows logon task (so a reboot brings it back too); see register-daemon-task.ps1.

Run it under pythonw.exe so there is no console window. Child stdout/stderr are redirected to the log
file, which also means the child's sys.stdout is a real file (not None as bare pythonw would give),
so the daemon's own logging keeps working.

  pythonw daemon_supervisor.py --config-dir <companion> --python <python.exe> --mode shadow
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time


def _log(logf, msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] supervisor: {msg}\n"
    try:
        logf.write(line)
        logf.flush()
    except Exception:
        pass


def _pythonw_for(python_path: str) -> str:
    """Prefer a windowless pythonw.exe next to the given python.exe, so the child has no console."""
    if python_path.lower().endswith("python.exe"):
        cand = python_path[:-len("python.exe")] + "pythonw.exe"
        if os.path.isfile(cand):
            return cand
    return python_path


def main() -> int:
    ap = argparse.ArgumentParser(description="keep-alive supervisor for demand_bot.py")
    ap.add_argument("--config-dir", required=True, help="companion config dir (sets DEMAND_MINING_CONFIG)")
    ap.add_argument("--python", default=sys.executable, help="interpreter to run the daemon with")
    ap.add_argument("--mode", choices=("dry", "shadow", "live"), default="shadow")
    ap.add_argument("--interval", default="90")
    ap.add_argument("--display-interval", default="300")
    ap.add_argument("--log-dir", default=os.path.expanduser("~/.demand-mining-logs"))
    ap.add_argument("--min-backoff", type=float, default=5.0)
    ap.add_argument("--max-backoff", type=float, default=300.0)
    args = ap.parse_args()

    os.makedirs(args.log_dir, exist_ok=True)
    daemon = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demand_bot.py")
    pyw = _pythonw_for(args.python)
    env = dict(os.environ, DEMAND_MINING_CONFIG=args.config_dir)
    backoff = args.min_backoff

    # the supervisor's own log persists across child restarts; the child appends to the same file
    sup_log = open(os.path.join(args.log_dir, "supervisor.log"), "a", encoding="utf-8", buffering=1)
    _log(sup_log, f"start mode={args.mode} python={pyw} daemon={daemon} config={args.config_dir}")

    while True:
        stamp = time.strftime("%Y-%m-%d")
        child_log = open(os.path.join(args.log_dir, f"daemon-{stamp}.log"), "a", encoding="utf-8", buffering=1)
        started = time.time()
        try:
            rc = subprocess.call(
                [pyw, daemon, "--mode", args.mode, "--interval", str(args.interval),
                 "--display-interval", str(args.display_interval)],
                env=env, stdout=child_log, stderr=child_log)
        except Exception as e:  # pragma: no cover (defensive: bad interpreter path etc.)
            rc = -1
            _log(sup_log, f"launch failed: {e!r}")
        finally:
            child_log.close()
        ran = time.time() - started
        _log(sup_log, f"daemon exited rc={rc} after {ran:.0f}s")
        # a run that stayed up a while was healthy; reset backoff. a fast crash-loop backs off.
        backoff = args.min_backoff if ran > 60 else min(args.max_backoff, backoff * 2)
        _log(sup_log, f"restarting in {backoff:.0f}s")
        time.sleep(backoff)


if __name__ == "__main__":
    sys.exit(main())
