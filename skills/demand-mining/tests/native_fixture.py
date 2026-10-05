"""Real reminder CLI adapter with only remote GitHub visibility replaced offline."""
from pathlib import Path


def native_reminder(tmp_path, reminder):
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
