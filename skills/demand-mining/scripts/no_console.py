"""Keep a windowless demand-mining process from opening console windows for its children.

A console program (git.exe, powershell.exe, python.exe) started by a process that has no
console of its own receives a NEW console. Under pythonw.exe (the daemon and its supervisor)
Windows shows that console as a window, through the default terminal when one is configured.
The privacy proof launches Git many times per durable write, so a windowless daemon once
produced thousands of terminal windows in a few hours.

Two layers prevent that:

* no_window_kwargs() is passed explicitly by every subprocess launch in this skill.
* install_no_console_window_default() runs at the start of the long-running entry points.
  When the process has no console it makes CREATE_NO_WINDOW the default for every
  subprocess.Popen in the process, including launches made by imported code such as the
  shared guard kit. A caller that explicitly asks for its own console or a detached process
  keeps that choice. With a console, children share it and the installer does nothing.

Both are no-ops off Windows.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
# Any of these means the caller already decided what console the child gets.
_EXPLICIT_CONSOLE_CHOICE = CREATE_NO_WINDOW | CREATE_NEW_CONSOLE | DETACHED_PROCESS
# Position of creationflags in subprocess.Popen.__init__(self, args, bufsize, executable,
# stdin, stdout, stderr, preexec_fn, close_fds, shell, cwd, env, universal_newlines,
# startupinfo, creationflags, ...), counted without self.
_CREATIONFLAGS_POSITION = 13
_MARKER = "_demand_mining_no_console_default"
_INSTALL_LOCK = threading.Lock()


def no_window_kwargs(platform: str | None = None) -> dict:
    """Keyword arguments that stop a console child from getting its own window."""
    return {"creationflags": CREATE_NO_WINDOW} if (platform or sys.platform) == "win32" else {}


def _console_window() -> int | None:
    try:
        import ctypes
        return int(ctypes.windll.kernel32.GetConsoleWindow() or 0)
    except (ImportError, AttributeError, OSError):
        return None


def console_missing(platform: str | None = None, executable: str | None = None,
                    console_window=_console_window) -> bool:
    """True when this Windows process has no console its console children could share."""
    if (platform or sys.platform) != "win32":
        return False
    window = console_window()
    if window is not None:
        return window == 0
    name = os.path.basename(executable if executable is not None else sys.executable or "")
    return name.lower() == "pythonw.exe"


def _with_default(flags) -> int:
    flags = int(flags or 0)
    return flags if flags & _EXPLICIT_CONSOLE_CHOICE else flags | CREATE_NO_WINDOW


def install_no_console_window_default(*, platform: str | None = None, executable: str | None = None,
                                      console_window=_console_window, popen=None) -> bool:
    """Make CREATE_NO_WINDOW the process-wide Popen default when there is no console.

    Returns True when the default is active after the call. Idempotent; a no-op off
    Windows and when the process has a console.
    """
    if not console_missing(platform, executable, console_window):
        return False
    popen = popen or subprocess.Popen
    with _INSTALL_LOCK:
        original = popen.__init__
        if getattr(original, _MARKER, False):
            return True

        def __init__(self, *args, **kwargs):
            if len(args) > _CREATIONFLAGS_POSITION:
                args = list(args)
                args[_CREATIONFLAGS_POSITION] = _with_default(args[_CREATIONFLAGS_POSITION])
            else:
                kwargs["creationflags"] = _with_default(kwargs.get("creationflags", 0))
            return original(self, *args, **kwargs)

        setattr(__init__, _MARKER, True)
        __init__.__wrapped__ = original
        __init__.__doc__ = original.__doc__
        popen.__init__ = __init__
    return True
