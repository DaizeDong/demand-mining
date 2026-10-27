"""Machine role: a secondary machine (CLAUDE_MACHINE_ROLE=secondary) causes no outside side effects.

The value is read on every call from HKCU\\Environment as well as from this process's environment,
so a process started before the role changed obeys it too. Either source saying secondary makes
this a secondary machine; a missing value means primary.

The machine id (CLAUDE_MACHINE_ID) names this machine's lane in per-machine paths. It is set once
and never changes; an unset id raises instead of falling back to any default.
"""
import os
import sys

MESSAGE = 'secondary machine: do this on the primary'


def _user_environment(name):
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
            return str(winreg.QueryValueEx(key, name)[0])
    except (ImportError, OSError):
        return ''


def is_secondary():
    values = [os.environ.get('CLAUDE_MACHINE_ROLE', ''), _user_environment('CLAUDE_MACHINE_ROLE')]
    return any(value.strip().lower() == 'secondary' for value in values)


def refuse_on_secondary():
    """On the secondary machine print one line and exit 3; otherwise return."""
    if is_secondary():
        sys.stderr.write(MESSAGE + '\n')
        raise SystemExit(3)


def machine_id():
    """This machine's fixed id from the process environment or HKCU\\Environment; raises when unset."""
    value = os.environ.get('CLAUDE_MACHINE_ID', '').strip() or _user_environment('CLAUDE_MACHINE_ID').strip()
    if not value:
        raise RuntimeError('CLAUDE_MACHINE_ID is not set; set it once per machine as a user environment variable')
    return value
