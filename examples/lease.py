#!/usr/bin/env python3
"""Primary lease: exactly one coordinator session is primary.

  init   write a lease when none exists
  renew  the holder refreshes renewed_at (do it on every pass)
  take   explicit handover with a reason; an expired lease is reported, never taken automatically
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

TTL = timedelta(hours=2)


class LeaseError(RuntimeError):
    pass


class Lease:
    def __init__(self, path: Path):
        self.path = Path(path)

    def read(self) -> dict | None:
        return json.loads(self.path.read_text()) if self.path.exists() else None

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

    def init(self, session_id: str, host: str, now: datetime | None = None) -> dict:
        if self.read():
            raise LeaseError(f"lease exists (primary {self.holder()}); use take()")
        now = now or datetime.now(timezone.utc)
        return self._write({"primary": session_id, "host": host, "since": now.isoformat(),
                            "renewed_at": now.isoformat(), "ttl_s": int(TTL.total_seconds()), "reason": "init"})

    def renew(self, session_id: str, now: datetime | None = None) -> dict:
        lease = self.read()
        if not lease or lease["primary"] != session_id:
            raise LeaseError(f"{session_id} does not hold the lease")
        lease["renewed_at"] = (now or datetime.now(timezone.utc)).isoformat()
        return self._write(lease)

    def take(self, session_id: str, host: str, reason: str, now: datetime | None = None) -> dict:
        if not reason.strip():
            raise LeaseError("a takeover needs a reason")
        now = now or datetime.now(timezone.utc)
        previous = self.read() or {}
        return self._write({"primary": session_id, "host": host, "since": now.isoformat(),
                            "renewed_at": now.isoformat(), "ttl_s": int(TTL.total_seconds()),
                            "reason": reason, "previous": previous.get("primary")})


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as root:
        lease = Lease(Path(root) / "lease.json")
        t0 = datetime(2026, 1, 16, 9, 0, tzinfo=timezone.utc)
        lease.init("coordinator-a1", "host-a", now=t0)
        print("state at +1h:", lease.state(t0 + timedelta(hours=1)))
        print("state at +3h:", lease.state(t0 + timedelta(hours=3)), "(flagged, not taken over)")
        lease.take("coordinator-b2", "host-b", "planned rollover", now=t0 + timedelta(hours=3))
        print("holder:", lease.holder())
