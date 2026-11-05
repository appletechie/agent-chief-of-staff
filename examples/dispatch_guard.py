#!/usr/bin/env python3
"""Duplicate-dispatch guard: refuse to start a worker when the work is already under way.

Every external lookup is injected, so the guard is testable offline and works with any VCS host,
issue tracker or agent runner. A lookup that fails becomes a warning, never a silent pass.

Claims are written under an exclusive lock, and claim() refuses a name another session holds, so two sessions
that both passed guard() at the same moment cannot both claim the same row: the second claim raises ClaimHeld.

A claim that parses but has a bad field (not an object, no session or worker, an `at` that is not an ISO 8601 time
with a time zone) can't be aged out or attributed, so it never expires and never reads as free: guard() refuses that
name and claim() treats it as held, until a person repairs or removes it.
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

CLAIM_TTL = timedelta(hours=6)
LOCK_TIMEOUT = 20.0

_local = threading.local()


class LockTimeout(TimeoutError):
    pass


class ClaimsCorrupt(RuntimeError):
    """A corrupt claims file must not be mistaken for an empty one."""


class ClaimHeld(RuntimeError):
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
        raise ValueError("claims need an aware time (datetime.now(timezone.utc), not datetime.now())")
    return now


def claim_problem(claim) -> str | None:
    """Why a claim record can't be trusted, or None."""
    if not isinstance(claim, dict):
        return f"not an object ({type(claim).__name__})"
    for name in ("session", "worker"):
        if not isinstance(claim.get(name), str) or not claim[name].strip():
            return f"{name!r} is missing or not a string"
    if aware(claim.get("at")) is None:
        return f"'at' {str(claim.get('at'))[:40]!r} is not an ISO 8601 time with a time zone"
    return None


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


@dataclass
class Sources:
    open_prs_naming: Callable[[str], Iterable[dict]]   # key -> [{"repo", "number", "author"}] across all orgs, any author
    assignee: Callable[[str], str | None]              # key -> assignee login or None
    live_workers: Callable[[], Iterable[dict]]         # -> [{"id", "row", "key", "host"}] on every host
    allowed_assignees: frozenset = frozenset({"owner", "coordinator"})
    recent_dispatches: Callable[[str], Iterable[dict]] = lambda key: ()  # key -> [{"key", "session", "at"}]


@dataclass
class Verdict:
    refusals: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    override: str | None = None

    @property
    def allowed(self) -> bool:
        return not self.refusals or bool(self.override)


class Claims:
    """claims.json: {name: {"session", "worker", "at"}} where name is a row id or an issue key."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = self.path.with_suffix(".lock")

    def load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError, UnicodeError) as error:
            raise ClaimsCorrupt(f"claims file {self.path} is corrupt: {str(error)}")
        if not isinstance(data, dict):
            raise ClaimsCorrupt(f"claims file {self.path} is not an object")
        return data

    def live(self, now: datetime) -> dict:
        """Claims younger than CLAIM_TTL, plus every claim with a bad field (kept as is: it never expires)."""
        return {k: v for k, v in self.load().items() if claim_problem(v) or now - aware(v["at"]) < CLAIM_TTL}

    def claim(self, names: Iterable[str], session: str, worker: str, now: datetime,
              override: str | None = None) -> None:
        """Claim every name or none. A name another session holds refuses unless override gives a reason."""
        names, now = list(names), clock(now)
        with file_lock(self.lock):
            data = self.live(now)
            taken = [f"{n} (invalid claim: {claim_problem(data[n])})" if claim_problem(data[n]) else
                     f"{n} (session {data[n]['session']})" for n in names
                     if n in data and (claim_problem(data[n]) or data[n]["session"] != session)]
            if taken and not (override and override.strip()):
                raise ClaimHeld("already claimed: " + ", ".join(taken))
            for name in names:
                data[name] = {"session": session, "worker": worker, "at": now.isoformat()}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent)
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp, self.path)


def guard(key: str | None, row: str, session: str, sources: Sources, claims: Claims,
          now: datetime | None = None, add_worker: bool = False, allow_duplicate: str | None = None) -> Verdict:
    now = clock(now)
    v = Verdict()
    if allow_duplicate is not None and not allow_duplicate.strip():
        raise ValueError("--allow-duplicate needs a reason")

    if key:
        try:
            for pr in sources.open_prs_naming(key):
                try:
                    ref = (pr["repo"], pr["number"], pr["author"])
                except (KeyError, TypeError) as error:
                    v.warnings.append(f"malformed PR record ({type(error).__name__}); continuing the search")
                    v.refusals.append("PR search contains a malformed record; rule out a duplicate by hand")
                    continue
                v.refusals.append(f"open PR {pr['repo']}#{pr['number']} by {pr['author']} names {key}")
        except Exception as error:
            v.warnings.append(f"PR search failed ({type(error).__name__}); duplicate PRs not ruled out")
            v.refusals.append("duplicate PR search is incomplete; cannot prove no competing PR is open")
        try:
            who = sources.assignee(key)
            if who and who not in sources.allowed_assignees:
                v.refusals.append(f"{key} is assigned to {who}")
        except Exception as error:
            v.warnings.append(f"assignee lookup failed ({type(error).__name__})")

    try:
        for w in sources.live_workers():
            if not isinstance(w, dict):
                v.refusals.append(f"live worker list contains invalid {w!r}; duplicate coverage unknown")
                continue
            if w.get("row") == row or (key and w.get("key") == key):
                if w.get("id") is None:
                    v.refusals.append("a live worker matches but lacks an id; duplicate coverage unclear")
                elif add_worker and w.get("row") == row:
                    v.warnings.append(f"adding a second worker beside {w['id']} on {row}")
                else:
                    v.refusals.append(f"live worker {w['id']} on {w.get('host', '?')} carries {w.get('row') or w.get('key')}")
    except Exception as error:
        v.warnings.append(f"worker listing failed ({type(error).__name__})")

    # dispatch.json catches the case where the claims expired: refuse a recent dispatch from another session,
    # until the claims TTL would have caught it.
    try:
        cutoff = now - CLAIM_TTL
        for dispatch in sources.recent_dispatches(key or ""):
            try:
                if dispatch["session"] == session:
                    continue
                when = datetime.fromisoformat(dispatch["at"])
                if when.tzinfo is None:
                    raise ValueError("naive dispatch time")
            except (KeyError, TypeError, ValueError, OverflowError) as error:
                # treat the whole record (including the cutoff comparison) as unsafe, not just the parse.
                v.warnings.append(f"malformed recent dispatch ({error}); continuing the lookup")
                continue
            if when >= cutoff:
                v.refusals.append(f"recent dispatch for {dispatch.get('key') or key} by session {dispatch.get('session')} at {dispatch['at']}")
    except Exception as error:
        v.warnings.append(f"recent dispatch lookup failed ({type(error).__name__})")

    live_claims = {}
    try:
        live_claims = claims.live(now)
    except ClaimsCorrupt as error:
        v.refusals.append(f"claims file unreadable, cannot rule out a competing claim: {error}")
    for name in filter(None, (row, key)):
        if name not in live_claims:
            continue
        held, problem = live_claims[name], claim_problem(live_claims[name])
        if problem:
            v.refusals.append(f"{name} has an invalid claim ({problem}); repair or remove it in {claims.path.name}")
        elif held["session"] != session:
            v.refusals.append(f"{name} claimed by session {held['session']} (worker {held['worker']}) at {held['at']}")

    if v.refusals and allow_duplicate:
        v.override = allow_duplicate
    return v


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as root:
        claims = Claims(Path(root) / "claims.json")
        teammate_pr = Sources(
            open_prs_naming=lambda key: [{"repo": "org/app", "number": 139, "author": "teammate"}],
            assignee=lambda key: "owner",
            live_workers=lambda: [],
        )
        v = guard("ISSUE-308", "R-020", "coordinator-a1", teammate_pr, claims)
        print("allowed:", v.allowed, "refusals:", v.refusals)
        clear = Sources(open_prs_naming=lambda key: [], assignee=lambda key: None, live_workers=lambda: [])
        v = guard("ISSUE-309", "R-021", "coordinator-a1", clear, claims)
        print("allowed:", v.allowed, "refusals:", v.refusals)
