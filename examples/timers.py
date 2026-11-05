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

import json
import os
import re
import secrets
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

MAX_ATTEMPTS = 5
DEFAULT_DELAY = timedelta(minutes=60)
WAIT_LINE = re.compile(r"^\s*WAIT\b\s*(?P<kind>\S+)?\s*(?P<ref>\S+)?\s*(?P<check_by>.+)?$")
KINDS = {"ci", "review", "deploy", "render", "date", "person", "other"}


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
    due, note = parse_due(m.group("check_by"), now)
    return {"kind": kind, "ref": m.group("ref") or "", "due": due, "note": note}


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
        """Deliver due timers. Persists the `sending` mark before each send. Returns one log line per action."""
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
