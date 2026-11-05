#!/usr/bin/env python3
"""Primary lease: exactly one coordinator session is primary, and a replaced primary cannot keep writing.

  init   write a lease when none exists (epoch 1)
  renew  the holder refreshes renewed_at (do it on every pass); returns the lease, including its epoch
  take   explicit handover with a reason; an expired lease is reported, never taken automatically.
         A held lease is taken only with force=True. Every take increments the epoch and records the
         previous holder and when it was last seen.
  epoch  the current epoch, the fencing token

Fencing: each writer (ledger write_row/close_row, timers add/tick, the wake-gate save) is built with
fence=lease.fenced and called with expect_epoch=<the epoch this session last read>. A writer refuses
when the two differ, so a primary that slept through a takeover cannot write when it wakes up: it must
re-read the lease, see it is no longer primary, and act as a companion.

  fenced  context manager: holds the lease lock SHARED and yields the current epoch. A writer compares the
          epoch and does its write inside it. init, renew and take hold the same lock EXCLUSIVE, so the epoch
          cannot move between a writer's check and its write: a take waits for writes in flight, and every
          write after it is refused. Writers do not block each other.

Lock order: the lease lock before a store lock. init, renew and take never take a store lock, so a writer that
waits for the lease lock while holding its store lock (Timers.tick, after a send) cannot deadlock. Inside fenced(),
the same thread cannot init, renew or take (that would be a lock upgrade); it gets a LeaseError instead of a hang.
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

TTL = timedelta(hours=2)
LOCK_TIMEOUT = 20.0

_local = threading.local()


class LeaseError(RuntimeError):
    pass


@contextmanager
def file_lock(path: Path, timeout: float = LOCK_TIMEOUT, shared: bool = False):
    """Advisory lock, exclusive unless shared=True, re-entrant per thread. Adapted from ledger_store.file_lock to keep
    this file standalone. A thread holding it shared cannot re-enter it exclusive (no upgrades)."""
    path = Path(path).resolve()
    held = _local.__dict__.setdefault("held", {})
    if path in held:
        if held[path][1] and not shared:
            raise LeaseError(f"{path} is held shared by this thread (a fenced write); finish it before init/renew/take")
        held[path][0] += 1
        try:
            yield
        finally:
            held[path][0] -= 1
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"{path} held by {Path(path).read_text().strip() or 'unknown'}")
                time.sleep(0.05)
        if not shared:  # shared holders would overwrite each other's pid
            os.ftruncate(fd, 0)
            os.write(fd, f"pid={os.getpid()}".encode())
        held[path] = [1, shared]
        try:
            yield
        finally:
            del held[path]
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class Lease:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = self.path.with_suffix(".lock")  # two simultaneous takes must not both get the same epoch

    def read(self) -> dict | None:
        if not self.path.exists():
            return None
        try:
            lease = json.loads(self.path.read_text())
        except json.JSONDecodeError as error:
            raise LeaseError(f"state file {self.path} is corrupt: {error.lineno}:{error.colno} {error.msg}; recover it before running the lease") from error
        if not isinstance(lease, dict):
            raise LeaseError(f"state file {self.path} is not an object (got {type(lease).__name__})")
        return lease

    def _write(self, data: dict) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, self.path)
        return data

    def state(self, now: datetime | None = None) -> str:
        now = now or datetime.now(timezone.utc)
        lease = self.read()
        if not lease:
            return "none"
        renewed = datetime.fromisoformat(lease["renewed_at"])
        return "held" if now - renewed <= timedelta(seconds=lease["ttl_s"]) else "expired"

    def holder(self) -> str | None:
        lease = self.read()
        return lease["primary"] if lease else None

    def is_primary(self, session_id: str) -> bool:
        return self.holder() == session_id

    def epoch(self) -> int:
        lease = self.read()
        return int(lease.get("epoch", 0)) if lease else 0

    @contextmanager
    def fenced(self, timeout: float = LOCK_TIMEOUT):
        """Yield the current epoch while no init, renew or take can run. Pass this (lease.fenced) as a writer's fence."""
        with file_lock(self.lock, timeout=timeout, shared=True):
            yield self.epoch()

    def init(self, session_id: str, host: str, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        with file_lock(self.lock):
            if self.read():
                raise LeaseError(f"lease exists (primary {self.holder()}); use take()")
            return self._write({"primary": session_id, "host": host, "since": now.isoformat(), "epoch": 1,
                                "renewed_at": now.isoformat(), "ttl_s": int(TTL.total_seconds()), "reason": "init"})

    def renew(self, session_id: str, now: datetime | None = None) -> dict:
        with file_lock(self.lock):
            lease = self.read()
            if not lease or lease["primary"] != session_id:
                raise LeaseError(f"{session_id} does not hold the lease")
            lease["renewed_at"] = (now or datetime.now(timezone.utc)).isoformat()
            return self._write(lease)

    def take(self, session_id: str, host: str, reason: str, now: datetime | None = None,
             force: bool = False) -> dict:
        if not reason.strip():
            raise LeaseError("a takeover needs a reason")
        now = now or datetime.now(timezone.utc)
        with file_lock(self.lock):
            state = self.state(now)
            if state == "held" and not force:
                raise LeaseError(f"lease is held by {self.holder()}; take it only when expired, or with force=True")
            old = self.read() or {}
            previous = {k: old.get(k) for k in ("primary", "host", "epoch", "renewed_at")} if old else None
            if previous:
                previous["state_at_take"] = state
            return self._write({"primary": session_id, "host": host, "since": now.isoformat(),
                                "epoch": int(old.get("epoch", 0)) + 1,
                                "renewed_at": now.isoformat(), "ttl_s": int(TTL.total_seconds()),
                                "reason": reason, "previous": previous})

if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as root:
        lease = Lease(Path(root) / "lease.json")
        t0 = datetime(2026, 1, 16, 9, 0, tzinfo=timezone.utc)
        lease.init("coordinator-a1", "host-a", now=t0)
        print("state at +1h:", lease.state(t0 + timedelta(hours=1)))
        print("state at +3h:", lease.state(t0 + timedelta(hours=3)), "(flagged, not taken over)")
        lease.take("coordinator-b2", "host-b", "old primary silent past its TTL", now=t0 + timedelta(hours=3))
        print("holder:", lease.holder(), "epoch:", lease.epoch(), "previous:", lease.read()["previous"])
