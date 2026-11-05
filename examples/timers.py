#!/usr/bin/env python3
"""Durable timers with at-most-once delivery, and the WAIT line parser.

A tick (run every minute by the OS scheduler) delivers due timers:
  armed --(due)--> sending --(send ok)--> delivered
                       \\--(send error)--> armed again (attempts += 1), failed after MAX_ATTEMPTS
  a timer found in `sending` at the start of a tick crashed mid-send: it becomes failed, never resent blind
  a due timer whose ledger row is closed is cancelled with a reason

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
from typing import Callable

MAX_ATTEMPTS = 5
LOCK_TIMEOUT = 20.0
_local = threading.local()  # re-entrancy is per thread; other threads must really wait
DEFAULT_DELAY = timedelta(minutes=60)
WAIT_LINE = re.compile(r"^\s*WAIT\b\s*(?P<kind>\S+)?\s*(?P<ref>\S+)?\s*(?P<check_by>.+)?$")
KINDS = {"ci", "review", "deploy", "render", "date", "person", "other"}

@contextmanager
def file_lock(path, timeout: float = LOCK_TIMEOUT):
    """Exclusive advisory lock, re-entrant within this process. Same pattern as ledger_store.file_lock.

    add() and tick() are read-modify-write cycles over one JSON file. Unlocked, two concurrent adds
    lose timers (a lost timer is a missed wake), and two overlapping ticks both deliver the same
    timer, breaking the at-most-once promise in docs/waking.md.
    """
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
                if time.monotonic() > deadline:
                    raise TimeoutError(f"{path} is held by pid "
                                       f"{(Path(path).read_text().strip() or 'unknown')}")
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
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> list[dict]:
        return json.loads(self.path.read_text()) if self.path.exists() else []

    def save(self, timers: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent)
        with os.fdopen(fd, "w") as fh:
            json.dump(timers, fh, indent=2)
        os.replace(tmp, self.path)

    def add(self, row: str, due: str, name: str, prompt: str, target: str = "coordinator",
            owner: str = "coordinator", source: str = "cli", now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        when, note = parse_due(due, now)
        timer = {"id": "t-" + secrets.token_hex(3), "row": row, "name": name, "due": when.isoformat(),
                 "target": target, "owner": owner, "prompt": prompt, "state": "armed",
                 "attempts": 0, "source": source, "note": note}
        with file_lock(self.path.with_suffix(".lock")):
            self.save(self.load() + [timer])
        return timer

    def from_wait(self, line: str, row: str, owner: str, now: datetime | None = None) -> dict | None:
        now = now or datetime.now(timezone.utc)
        wait = parse_wait(line, now)
        if wait is None:
            return None
        prompt = (f"{row}: worker {owner} is waiting on {wait['kind']} {wait['ref'] or '(no ref)'}. "
                  "Check it, then resume or close the worker.")
        return self.add(row, wait["due"].isoformat(), f"wait {wait['kind']}", prompt,
                        owner=owner, source="wait-line", now=now)

    def tick(self, send: Callable[[str, str], str], open_rows: set[str], now: datetime | None = None) -> list[str]:
        """Deliver due timers under one lock. Persists the `sending` mark before each send.

        A second tick that starts while this one is inside `send` does not wait and does not deliver:
        it exits, because the running tick owns the delivery. Ticks overlap whenever a send blocks on a
        busy session, and two ticks delivering the same timer is exactly the double-message the
        `sending` state was meant to prevent.
        """
        try:
            with file_lock(self.path.with_suffix(".lock"), timeout=0):
                return self._tick(send, open_rows, now)
        except TimeoutError:
            return ["another tick is running; delivered nothing"]

    def _tick(self, send: Callable[[str, str], str], open_rows: set[str], now: datetime | None = None) -> list[str]:
        now = now or datetime.now(timezone.utc)
        timers, log = self.load(), []
        for t in timers:
            if t["state"] == "sending":
                t["state"], t["note"] = "failed", "found in sending state; not resent"
                log.append(f"{t['id']} failed (crashed mid-send)")
        self.save(timers)
        for t in timers:
            if t["state"] != "armed" or datetime.fromisoformat(t["due"]) > now:
                continue
            if t["row"] not in open_rows:
                t["state"], t["note"] = "cancelled", f"row {t['row']} is closed"
                log.append(f"{t['id']} cancelled (row closed)")
                self.save(timers)
                continue
            t["state"] = "sending"
            self.save(timers)
            try:
                receipt = send(t["target"], f"[timer {t['id']} {t['name']}] {t['prompt']}")
            except Exception as error:
                t["attempts"] += 1
                t["state"] = "failed" if t["attempts"] >= MAX_ATTEMPTS else "armed"
                t["note"] = f"send error: {type(error).__name__}"
                log.append(f"{t['id']} send error, attempt {t['attempts']}")
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
