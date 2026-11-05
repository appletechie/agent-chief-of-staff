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

A lease file that does not parse, or parses with a bad field (no primary, a renewed_at or since that is not an ISO
8601 time with a time zone, a ttl_s or epoch that is not a positive whole number), raises LeaseError from every
read: state, holder, epoch, fenced, renew and take. Nothing repairs it automatically. A scheduled job (the timer
tick, the merge gate, intake) calls primary_gate() first: a LeaseError there reads as "not primary here", one log
line, and the job skips. No lease file is the same: nobody is primary until init(), primary_gate() skips and
fenced() raises LeaseError, so nothing writes at "epoch 0" by accident (#25). A lease written before epochs existed
(no epoch field) is still valid, epoch 0.

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


def aware(value) -> datetime | None:
    """The datetime of an ISO 8601 string that carries a time zone, else None. A naive time is ambiguous: invalid."""
    if not isinstance(value, str):
        return None
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.utcoffset() is not None else None


def clock(now: datetime | None) -> datetime:
    """now, or the current UTC time. A naive clock would write a naive time that every later read rejects: refuse it."""
    if now is None:
        return datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("lease writes need an aware time (datetime.now(timezone.utc), not datetime.now())")
    return now


def whole(value, minimum: int = 1) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def lease_problem(lease: dict) -> str | None:
    """Why a parsed lease can't be trusted, or None. epoch, host and since are optional (older leases lack them)."""
    if not isinstance(lease.get("primary"), str) or not lease["primary"].strip():
        return "'primary' is missing or not a string"
    if "host" in lease and not isinstance(lease["host"], str):
        return f"'host' is {type(lease['host']).__name__}, not a string"
    if not whole(lease.get("ttl_s")):
        return f"'ttl_s' {str(lease.get('ttl_s'))[:40]!r} is not a whole number of seconds > 0"
    if "epoch" in lease and not whole(lease["epoch"]):
        return f"'epoch' {str(lease['epoch'])[:40]!r} is not a whole number >= 1"
    for name in ("renewed_at", "since"):
        if (name == "renewed_at" or name in lease) and aware(lease.get(name)) is None:
            return f"{name!r} {str(lease.get(name))[:40]!r} is not an ISO 8601 time with a time zone"
    return None


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
        """The lease, or None when there is no lease file. Path.exists() is not used: it reads False for a file
        under an unreadable directory, and that would make "can't read the lease" look like "no lease" (#25)."""
        try:
            text = self.path.read_text()
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError) as error:
            raise LeaseError(f"state file {self.path} is unreadable: {error!s}; recover it before running the lease") from error
        try:
            lease = json.loads(text)
        except ValueError as error:  # JSONDecodeError, and the int digit limit (a 4301-digit number)
            raise LeaseError(f"state file {self.path} is corrupt: {error!s}; recover it before running the lease") from error
        if not isinstance(lease, dict):
            raise LeaseError(f"state file {self.path} is not an object (got {type(lease).__name__})")
        problem = lease_problem(lease)
        if problem:
            raise LeaseError(f"state file {self.path}: {problem}; repair it by hand (keep the epoch) before running the lease")
        return lease

    def _write(self, data: dict) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, self.path)
        return data

    def state(self, now: datetime | None = None) -> str:
        now = clock(now)
        lease = self.read()
        if not lease:
            return "none"
        renewed = aware(lease["renewed_at"])
        return "held" if now - renewed <= timedelta(seconds=lease["ttl_s"]) else "expired"

    def holder(self) -> str | None:
        lease = self.read()
        return lease["primary"] if lease else None

    def is_primary(self, session_id: str) -> bool:
        return self.holder() == session_id

    def epoch(self) -> int:
        lease = self.read()
        return lease.get("epoch", 0) if lease else 0

    @contextmanager
    def fenced(self, timeout: float = LOCK_TIMEOUT):
        """Yield the current epoch while no init, renew or take can run. Pass this (lease.fenced) as a writer's fence.
        With no lease there is no primary to fence for: LeaseError, so a fenced write never runs at "epoch 0" (#25)."""
        with file_lock(self.lock, timeout=timeout, shared=True):
            if self.read() is None:
                raise LeaseError(f"no lease at {self.path}; nobody is primary until init()")
            yield self.epoch()

    def init(self, session_id: str, host: str, now: datetime | None = None) -> dict:
        identity(session_id, "the primary's session id")
        now = clock(now)
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
            lease["renewed_at"] = clock(now).isoformat()
            return self._write(lease)

    def take(self, session_id: str, host: str, reason: str, now: datetime | None = None,
             force: bool = False) -> dict:
        if not isinstance(reason, str) or not reason.strip():
            raise LeaseError("a takeover needs a reason")
        identity(session_id, "the new primary's session id")
        now = clock(now)
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

def identity(value, what: str) -> None:
    """Refuse a blank or non-string id before it is written: lease_problem() would reject the file on the next read,
    so a blank primary would turn a working lease into a broken one (#25)."""
    if not isinstance(value, str) or not value.strip():
        raise LeaseError(f"{what} must be a non-empty string; nothing written")


def primary_gate(lease: Lease, job: str, log=print) -> int | None:
    """For a scheduled job: the epoch to pass as expect_epoch, or None to skip this run. A broken lease file is
    "not primary here": one log line, and the job does nothing (it never crashes, and never acts unfenced). So is no
    lease file at all: nobody is primary until init() writes one (#25); an epoch-less lease is still epoch 0."""
    try:
        if lease.read() is None:
            log(f"{job}: no lease; skipped (nobody is primary until the lease is initialised)")
            return None
        return lease.epoch()
    except LeaseError as error:
        log(f"{job}: not primary here; skipped ({error})")
        return None


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as root:
        lease = Lease(Path(root) / "lease.json")
        t0 = datetime(2026, 1, 16, 9, 0, tzinfo=timezone.utc)
        lease.init("coordinator-a1", "host-a", now=t0)
        print("state at +1h:", lease.state(t0 + timedelta(hours=1)))
        print("state at +3h:", lease.state(t0 + timedelta(hours=3)), "(flagged, not taken over)")
        lease.take("coordinator-b2", "host-b", "old primary silent past its TTL", now=t0 + timedelta(hours=3))
        print("holder:", lease.holder(), "epoch:", lease.epoch(), "previous:", lease.read()["previous"])
