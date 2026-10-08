"""Revalidate a PRIVATE destination before every durable log append."""
from contextlib import contextmanager
from pathlib import Path
import sys
import traceback

from data_safety import require_private


class PrivateLogError(SystemExit):
    """Stop the daemon even when an event callback catches ordinary exceptions."""


class PrivateLog:
    encoding = "utf-8"

    def __init__(self, path, *, binary=False):
        self.path = Path(path).expanduser().absolute()
        self.binary = binary
        self.closed = False
        self.failure = None
        try:
            from data_safety import authorize_write
            admitted = authorize_write(self.path)
            Path(admitted["path"]).parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            self._stop(exc)

    def _stop(self, exc):
        self.failure = exc
        raise PrivateLogError(f"daemon logging stopped: {exc}") from exc

    def write(self, value):
        if self.failure is not None:
            self._stop(self.failure)
        if self.closed:
            self._stop(ValueError("private log is closed"))
        if not value:
            return 0
        try:
            from data_safety import authorize_write
            admitted = authorize_write(self.path)
            options = {} if self.binary else {"encoding": "utf-8", "newline": ""}
            with open(admitted["path"], "ab" if self.binary else "a", **options) as stream:
                count = stream.write(value)
                if count != len(value):
                    raise OSError("short private log append")
                stream.flush()
            return count
        except Exception as exc:
            self._stop(exc)

    def flush(self):
        # Writes are flushed and closed before returning; no buffered DATA remains.
        if self.failure is not None:
            self._stop(self.failure)

    def close(self):
        self.closed = True

    def isatty(self):
        return False

    def fileno(self):
        raise OSError("private log handles cannot be inherited by a child")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


@contextmanager
def private_output(path):
    """Redirect daemon output while preserving private startup-failure records."""
    previous = sys.stdout, sys.stderr
    stream = PrivateLog(path)
    sys.stdout = sys.stderr = stream
    try:
        yield stream
    except PrivateLogError:
        raise
    except BaseException:
        traceback.print_exc(file=stream)
        raise
    finally:
        sys.stdout, sys.stderr = previous
        stream.close()
