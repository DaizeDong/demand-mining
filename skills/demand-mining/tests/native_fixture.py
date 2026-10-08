"""Real reminder CLI adapter with only remote GitHub visibility replaced offline."""
import os
from pathlib import Path
import subprocess


def require_reminder_source():
    """Resolve the explicit code dependency before fixtures isolate HOME."""
    setting = "DEMAND_MINING_TEST_REMINDER_ROOT"
    value = os.environ.get(setting)
    if not value:
        raise AssertionError(
            f"set {setting} to a complete schedule-reminder checkout with initialized Guards")
    root = Path(value).expanduser().resolve()
    scripts = root / "skills/schedule-reminder/scripts"
    required = [root / ".gitmodules", root / "guards/tools/data_boundary.py",
                root / "guards/tools/pii_guard.py"]
    required.extend(scripts / name for name in (
        "reminder.py", "store.py", "private_data.py", "reminder_action_store.py",
        "creation_guard.py"))
    missing = [str(path.relative_to(root)) for path in required if not path.is_file()]
    missing.extend(name for name in (".git", "guards/.git") if not (root / name).exists())
    if missing:
        raise AssertionError(
            f"incomplete schedule-reminder checkout at {root}; missing: {', '.join(missing)}")
    return scripts / "reminder.py"


def run_native_reminder(command, *arguments):
    """Keep the real CLI failure visible instead of hiding captured stderr."""
    result = subprocess.run(
        [*command, *arguments], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60)
    assert result.returncode == 0, (
        f"real reminder CLI failed (exit {result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
    return result


def native_reminder(tmp_path, reminder):
    reminder = Path(reminder)
    assert reminder.is_file(), f"original reminder CLI is missing: {reminder}"
    launcher = Path(tmp_path) / "synthetic_reminder.py"
    launcher.write_text(
        "import json, os, runpy, socket, sys\n"
        "from pathlib import Path\n"
        f"reminder = Path({str(reminder)!r})\n"
        "sys.path.insert(0, str(reminder.parent))\n"
        "import private_data\n"
        "def visibility(argv):\n"
        "    if len(argv) != 6 or argv[:3] != ['gh', 'repo', 'view'] or argv[4:] != ['--json', 'nameWithOwner,visibility']:\n"
        "        raise AssertionError('unexpected external query in native fixture')\n"
        "    receipt = Path(os.environ['USERPROFILE']) / '.pii-guard/visibility.json'\n"
        "    values = json.loads(receipt.read_text(encoding='utf-8'))\n"
        "    if argv[3] not in values:\n"
        "        raise AssertionError('undeclared synthetic repository')\n"
        "    return json.dumps({'nameWithOwner': argv[3], 'visibility': values[argv[3]]})\n"
        "def no_network(*args, **kwargs):\n"
        "    raise AssertionError('native fixture attempted network access')\n"
        "socket.socket.connect = no_network\n"
        "private_data._query = visibility\n"
        "runpy.run_path(str(reminder), run_name='__main__')\n",
        encoding="utf-8", newline="\n")
    return launcher
