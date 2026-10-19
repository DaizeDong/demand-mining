"""Private backup of declared run files to the companion's origin using an isolated Git index."""
import os
from pathlib import Path

from data_safety import check_destination, file_lock, git


def _backup_locked(paths):
    checked = [check_destination(path) for path in paths]
    if not checked or len({item["root"] for item in checked}) != 1:
        raise ValueError("backup files must belong to one companion repository")
    if any(not Path(item["path"]).is_file() for item in checked):
        raise ValueError("each declared backup path must be a regular file")
    root = Path(checked[0]["root"])
    relatives = sorted({Path(item["path"]).relative_to(root).as_posix() for item in checked})
    import uuid
    index = str(root / (".demand-backup-index-" + uuid.uuid4().hex))
    environment = {"GIT_INDEX_FILE": index, "GIT_OPTIONAL_LOCKS": "0", "GIT_LITERAL_PATHSPECS": "1"}
    try:
        git(root, "read-tree", "HEAD", env=environment)
        git(root, "add", "--", *relatives, env=environment)
        delta = git(root, "diff", "--cached", "--quiet", "--exit-code", env=environment, allowed=(0, 1))
        if delta.returncode == 1:
            # commit --only runs against the default index and rejects a path that index has
            # never seen, which is every file of a new run. Stage just these paths there first;
            # --only still leaves anything else staged in the default index out of the commit.
            shared = {**environment, "GIT_INDEX_FILE": None}
            git(root, "add", "--", *relatives, env=shared)
            git(root, "commit", "--only", "-m", "data: archive demand-mining run",
                "--", *relatives, env=shared)
        # Rejected pushes stay pending; no pull/rebase/autostash mutates other work.
        git(root, "push", "origin", "HEAD", env=environment)
        return {"status": "confirmed", "files": relatives, "committed": delta.returncode == 1}
    finally:
        for path in (index, index + ".lock"):
            if os.path.exists(path):
                os.unlink(path)


def backup(paths):
    if not paths:
        raise ValueError("backup requires declared DATA paths")
    root = check_destination(paths[0])["root"]
    with file_lock(Path(root) / ".demand-backup.lock", timeout=300):
        return _backup_locked(paths)
