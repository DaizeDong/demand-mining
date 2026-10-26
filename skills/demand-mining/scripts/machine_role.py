"""Machine role: a secondary machine (CLAUDE_MACHINE_ROLE=secondary) causes no outside side effects.

The value is read on every call from HKCU\\Environment as well as from this process's environment,
so a process started before the role changed obeys it too. Either source saying secondary makes
this a secondary machine; a missing value means primary.
"""
import os
import sys

MESSAGE = 'secondary machine: do this on the primary'


def is_secondary():
    values = [os.environ.get('CLAUDE_MACHINE_ROLE', '')]
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
            values.append(str(winreg.QueryValueEx(key, 'CLAUDE_MACHINE_ROLE')[0]))
    except (ImportError, OSError):
        pass
    return any(value.strip().lower() == 'secondary' for value in values)


def refuse_on_secondary():
    """On the secondary machine print one line and exit 3; otherwise return."""
    if is_secondary():
        sys.stderr.write(MESSAGE + '\n')
        raise SystemExit(3)
