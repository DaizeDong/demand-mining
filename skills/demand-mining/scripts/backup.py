"""Checked private backup of declared run files using an isolated Git index."""
import os
from pathlib import Path
import tempfile

from data_safety import file_lock, git, require_private, require_private_push


def _backup_locked(paths):
    proofs = [require_private(path) for path in paths]
    if not proofs or len({proof["root"] for proof in proofs}) != 1:
        raise ValueError("backup files must belong to one verified PRIVATE repository")
    if any(not Path(proof["path"]).is_file() for proof in proofs):
        raise ValueError("each declared backup path must be a regular file")
    root = Path(proofs[0]["root"])
    transport = require_private_push(root, proofs[0]["repository"])
    relatives = sorted({Path(proof["path"]).relative_to(root).as_posix() for proof in proofs})
    fd, index = tempfile.mkstemp(prefix=".demand-backup-index-", dir=root)
    os.close(fd)
    os.unlink(index)
    environment = {"GIT_INDEX_FILE": index, "GIT_OPTIONAL_LOCKS": "0", "GIT_LITERAL_PATHSPECS": "1"}
    try:
        git(root, "read-tree", "HEAD", env=environment)
        git(root, "add", "--", *relatives, env=environment)
        delta = git(root, "diff", "--cached", "--quiet", "--exit-code", env=environment, allowed=(0, 1))
        if delta.returncode == 1:
            git(root, "commit", "--only", "-m", "data: archive demand-mining run",
                "--", *relatives, env={**environment, "GIT_INDEX_FILE": None})
        # Rejected pushes stay pending; no pull/rebase/autostash mutates other work.
        if require_private_push(root, proofs[0]["repository"]) != transport:
            raise ValueError("backup transport changed during preparation")
        git(root, "push", "origin", "HEAD", env=environment, transport_proof=transport)
        return {"status": "confirmed", "repository": proofs[0]["repository"],
                "files": relatives, "committed": delta.returncode == 1}
    finally:
        for path in (index, index + ".lock"):
            if os.path.exists(path):
                os.unlink(path)


def backup(paths):
    if not paths:
        raise ValueError("backup requires declared DATA paths")
    proof = require_private(paths[0])
    with file_lock(Path(proof["root"]) / ".demand-backup.lock", timeout=300):
        return _backup_locked(paths)
