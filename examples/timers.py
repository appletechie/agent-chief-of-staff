#!/usr/bin/env python3
"""Durable timers with at-most-once delivery, and the WAIT line parser.

A tick (run every minute by the OS scheduler) delivers due timers:
  armed --(due)--> sending --(send ok)--> delivered
                       \\--(send error)--> armed again (attempts += 1), failed once RETRY_WINDOW has passed
                                          since the first attempt
  a timer found in `sending` at the start of a tick crashed mid-send: it becomes failed, never resent blind
  a due timer whose ledger row is closed is cancelled with a reason

Every read-modify-write of timers.json happens under one exclusive lock. `add` waits for it (up to LOCK_TIMEOUT,
then fails loudly); `tick` never waits: a second tick finds the lock held and delivers nothing. The tick holds the
lock across each send, so `sending` on disk means a crashed tick (or one replaced mid-send, below), never one still
running.

Fencing: build the store with `fence=lease.fenced` (a context manager that blocks takeovers and yields the lease's
current epoch) and every write must pass `expect_epoch`; a stale epoch (the lease was taken since the caller read it)
is refused, and passing `expect_epoch` without a fence is an error. `add` holds the fence for its whole write (lock
order: fence, then the timer lock). `tick` takes the timer lock first (it must never wait for it), then holds the
fence for every write but releases it around each send, so a takeover never waits on a slow send; taking the fence
while holding the timer lock cannot deadlock, because init/renew/take never take a store lock. After the send it
re-takes the fence and re-checks the epoch before recording the outcome.
The bounded window: a takeover that lands during a send cannot stop that one send (it is already on its way), but the
replaced tick records nothing and stops. The timer stays `sending`, and the new primary's next tick marks it failed
("not resent"), so it is surfaced, never sent twice.

`send(target, text)` is injected: in production it queues a message into the coordinator's session,
which is delivered when the session is next free (never "start a run", which a busy agent may reject).
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, ContextManager

RETRY_WINDOW = timedelta(hours=4)  # retry send errors this long after the first attempt, then fail
LOCK_TIMEOUT = 20.0
DEFAULT_DELAY = timedelta(minutes=60)
WAIT_LINE = re.compile(r"^\s*WAIT\b\s*(?P<kind>\S+)?\s*(?P<ref>\S+)?\s*(?P<check_by>.+)?$")
KINDS = {"ci", "review", "deploy", "render", "date", "person", "other"}

_local = threading.local()


class LockTimeout(TimeoutError):
    pass


class StaleEpoch(RuntimeError):
    """The lease was taken after the caller read its epoch: this session is no longer the one it thinks it is."""


@contextmanager
def file_lock(path: Path, timeout: float = LOCK_TIMEOUT):
    """Exclusive advisory lock, re-entrant per thread. Copied from ledger_store.file_lock to keep this file standalone."""
    path = Path(path).resolve()
    held = _local.__dict__.setdefault("held", {})
    if path in held:
        held[path] += 1
        try:
            yield
        finally:
            held[path] -= 1
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockTimeout(f"{path} held by {Path(path).read_text().strip() or 'unknown'}")
                time.sleep(0.05)
        os.ftruncate(fd, 0)
        os.write(fd, f"pid={os.getpid()}".encode())
        held[path] = 1
        try:
            yield
        finally:
            del held[path]
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def parse_due(text: str | None, now: datetime) -> tuple[datetime, str | None]:
    """Accept +Nm / +Nh, HH:MMZ (next occurrence) or ISO. Never raise: fall back to now + 60 min with a note."""
    text = (text or "").strip()
    try:
        if m := re.fullmatch(r"\+(\d+)([mh])", text):
            n = int(m.group(1))
            return now + (timedelta(minutes=n) if m.group(2) == "m" else timedelta(hours=n)), None
        if m := re.fullmatch(r"(\d{1,2}):(\d{2})Z", text):
            due = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
            return (due if due > now else due + timedelta(days=1)), None
        due = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if due.tzinfo is None:
            raise ValueError("no time zone")
        return due, None
    except ValueError:
        return now + DEFAULT_DELAY, f"unreadable due {text!r}; defaulted to +60m"


def parse_wait(line: str, now: datetime) -> dict | None:
    """Parse `WAIT <kind> <ref> <check-by>` leniently. Only WAIT is required."""
    m = WAIT_LINE.match(line or "")
    if not m:
        return None
    kind = (m.group("kind") or "other").lower()
    if kind not in KINDS:
        kind = "other"
    ref, when = m.group("ref") or "", m.group("check_by")
    # "WAIT ci +45m": with the ref omitted, the time lands in the ref slot. If it parses as a time, it is one.
    if when is None and ref and parse_due(ref, now)[1] is None:
        ref, when = "", ref
    due, note = parse_due(when, now)
    return {"kind": kind, "ref": ref, "due": due, "note": note}


class Timers:
    def __init__(self, path: Path, fence: Callable[[], ContextManager[int]] | None = None):
        self.path = Path(path)
        self.lock = self.path.with_suffix(".lock")
        self.fence = fence

    @contextmanager
    def _fenced(self, expect_epoch: int | None):
        """Hold the fence (lease lock, shared) for a write; refuse a stale or missing epoch."""
        if self.fence is None:
            if expect_epoch is not None:
                raise ValueError("expect_epoch passed but no fence is configured; the write would not be fenced")
            yield
            return
        with self.fence() as current:
            if expect_epoch != current:
                raise StaleEpoch(f"lease epoch is {current}, caller holds {expect_epoch}; re-read the lease")
            yield

    def load(self) -> list[dict]:
        return json.loads(self.path.read_text()) if self.path.exists() else []

    def save(self, timers: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent)
        with os.fdopen(fd, "w") as fh:
            json.dump(timers, fh, indent=2)
        os.replace(tmp, self.path)

    def add(self, row: str, due: str, name: str, prompt: str, target: str = "coordinator",
            owner: str = "coordinator", source: str = "cli", now: datetime | None = None,
            expect_epoch: int | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        when, note = parse_due(due, now)
        with self._fenced(expect_epoch), file_lock(self.lock):
            timers = self.load()
            taken = {t["id"] for t in timers}
            timer_id = "t-" + secrets.token_hex(3)
            while timer_id in taken:
                timer_id = "t-" + secrets.token_hex(3)
            timer = {"id": timer_id, "row": row, "name": name, "due": when.isoformat(),
                     "target": target, "owner": owner, "prompt": prompt, "state": "armed",
                     "attempts": 0, "source": source, "note": note}
            self.save(timers + [timer])
        return timer

    def from_wait(self, line: str, row: str, owner: str, now: datetime | None = None,
                  expect_epoch: int | None = None) -> dict | None:
        now = now or datetime.now(timezone.utc)
        wait = parse_wait(line, now)
        if wait is None:
            return None
        prompt = (f"{row}: worker {owner} is waiting on {wait['kind']} {wait['ref'] or '(no ref)'}. "
                  "Check it, then resume or close the worker.")
        return self.add(row, wait["due"].isoformat(), f"wait {wait['kind']}", prompt,
                        owner=owner, source="wait-line", now=now, expect_epoch=expect_epoch)

    def tick(self, send: Callable[[str, str], str], open_rows: set[str], now: datetime | None = None,
             expect_epoch: int | None = None) -> list[str]:
        """Deliver due timers. Persists the `sending` mark before each send. Returns one log line per action."""
        try:
            with file_lock(self.lock, timeout=0):
                return self._tick(send, open_rows, now or datetime.now(timezone.utc), expect_epoch)
        except LockTimeout:
            return ["another tick is running; delivered nothing"]

    def _tick(self, send, open_rows: set[str], now: datetime, expect_epoch: int | None) -> list[str]:
        with self._fenced(expect_epoch):
            timers, log = self.load(), []
            for t in timers:
                if t["state"] == "sending":
                    t["state"], t["note"] = "failed", "found in sending state; not resent"
                    log.append(f"{t['id']} failed (crashed mid-send)")
            self.save(timers)
        for t in timers:
            if t["state"] != "armed" or datetime.fromisoformat(t["due"]) > now:
                continue
            with self._fenced(expect_epoch):  # a takeover since the last send stops the tick here
                if t["row"] not in open_rows:
                    t["state"], t["note"] = "cancelled", f"row {t['row']} is closed"
                    log.append(f"{t['id']} cancelled (row closed)")
                    self.save(timers)
                    continue
                t["state"] = "sending"
                t.setdefault("first_attempt_at", now.isoformat())
                self.save(timers)
            # The fence is released for the send so a takeover never waits on it; the timer lock is still held.
            try:
                receipt, error = send(t["target"], f"[timer {t['id']} {t['name']}] {t['prompt']}"), None
            except Exception as caught:
                receipt, error = None, caught
            with self._fenced(expect_epoch):  # taken over mid-send: record nothing, leave it `sending`
                if error is not None:
                    t["attempts"] += 1
                    expired = now - datetime.fromisoformat(t["first_attempt_at"]) >= RETRY_WINDOW
                    t["state"] = "failed" if expired else "armed"
                    t["note"] = f"send error: {type(error).__name__}"
                    log.append(f"{t['id']} send error, attempt {t['attempts']}" + ("; retry window over" if expired else ""))
                else:
                    t["state"], t["delivered_at"], t["receipt"] = "delivered", now.isoformat(), str(receipt)[:200]
                    log.append(f"{t['id']} delivered to {t['target']}")
                self.save(timers)
        return log

if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as root:
        store = Timers(Path(root) / "timers.json")
        start = datetime(2026, 1, 16, 14, 0, tzinfo=timezone.utc)
        store.from_wait("WAIT ci org/app#88 +45m", row="R-012", owner="worker-7f3a2c", now=start)
        store.add("R-099", "+5m", "stale", "this row is closed", now=start)
        sent = lambda target, text: f"queued for {target}"
        print(store.tick(sent, open_rows={"R-012"}, now=start + timedelta(hours=1)))
        print(json.dumps([{k: t[k] for k in ("id", "row", "state")} for t in store.load()], indent=2))
