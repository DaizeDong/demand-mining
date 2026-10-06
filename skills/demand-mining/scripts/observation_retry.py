"""Bounded retry and private quarantine for live observations that cannot be saved yet.

The live daemon polls every `interval` seconds. A pending observation used to be retried on
every poll forever and logged a line each time; one observation held by the privacy screen
was retried 190 times in a day. Each retry and each log line ran the PRIVATE companion
proof, so the retry loop was also what multiplied Git launches.

RetryBackoff counts polls. After the n-th consecutive failure the next attempt waits
2**(n-1) polls, capped at RETRY_CAP_SECONDS, so with the default 90 s interval the waits are
90 s, 180 s, 360 s and so on up to one hour. The first retry is always the next poll.
record_failure() reports whether the visible state changed (first failure or a different
error), and callers log only then.

An observation held by the privacy screen PRIVACY_HOLD_LIMIT times is moved to
`<pool dir>/quarantine/` in the PRIVATE companion instead of being retried again. The
record keeps the ingest-redacted observation so an operator can review and replay it;
nothing is discarded. The quarantine write uses the same PRIVATE admission as every other
DATA write and raises when that admission fails, in which case the observation stays
pending.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

RETRY_CAP_SECONDS = 3600.0
PRIVACY_HOLD_LIMIT = 8
QUARANTINE_DIR = "quarantine"
QUARANTINE_SCHEMA = "demand-mining/privacy-quarantine/v1"


class RetryBackoff:
    def __init__(self, interval, cap_seconds=RETRY_CAP_SECONDS):
        interval = float(interval or 0)
        self.interval = interval if interval > 0 else 1.0
        self.cap_polls = max(1, int(math.floor(float(cap_seconds) / self.interval)))
        self.reset()

    def reset(self):
        """Clear the failure state; returns how many failures preceded the reset."""
        previous = getattr(self, "failures", 0)
        self.failures = 0
        self.privacy_holds = 0
        self.waiting = 0
        self.error = None
        return previous

    def ready(self):
        """Consume one poll; False while the current backoff is still running."""
        if self.waiting > 0:
            self.waiting -= 1
            return False
        return True

    def record_failure(self, error_name, *, privacy_hold=False):
        """Schedule the next attempt. Returns True when the visible state changed."""
        changed = self.failures == 0 or error_name != self.error
        self.failures += 1
        if privacy_hold:
            self.privacy_holds += 1
        self.error = error_name
        self.waiting = self.wait_polls() - 1
        return changed

    def wait_polls(self):
        if self.failures <= 0:
            return 1
        return min(self.cap_polls, 2 ** min(self.failures - 1, 30))

    def delay_seconds(self):
        return self.wait_polls() * self.interval

    def exhausted(self):
        return self.privacy_holds >= PRIVACY_HOLD_LIMIT


def quarantine_path(pool_path, record):
    identity = record.get("observation", {}).get("observation_id") or json.dumps(
        record.get("observation", {}), sort_keys=True, ensure_ascii=False, default=str)
    name = "privacy-hold-" + hashlib.sha256(str(identity).encode("utf-8")).hexdigest()[:32] + ".json"
    return Path(pool_path).parent / QUARANTINE_DIR / name


def quarantine_observation(pool_path, record, *, writer=None):
    """Persist one held observation in the PRIVATE companion; raises if that is refused."""
    if writer is None:
        from data_safety import atomic_json as writer
    path = quarantine_path(pool_path, record)
    writer(path, {"schema": QUARANTINE_SCHEMA, **record})
    return path
