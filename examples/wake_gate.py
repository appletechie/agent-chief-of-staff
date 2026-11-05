#!/usr/bin/env python3
"""Wake gate: decide whether the coordinator needs a full pass.

Each collector returns {item: text}. The gate hashes every item, compares with the last saved snapshot,
and prints CHANGED or UNCHANGED on the first line, then one line per changed part.

Rules this sketch implements:
  - a collector that raises counts as CHANGED and keeps its previous snapshot;
  - a quiet stretch longer than max_quiet forces CHANGED, and so does a last-pass time in the future (clock skew,
    restored or copied state), which would otherwise make every stretch look short; the saved time is clamped to now;
  - only the lease holder saves the shared snapshot (persist=False for companions); with fence=lease.fenced set,
    the save also needs the caller's current epoch and refuses a stale one, and the check and the save happen
    inside the fence, so a takeover cannot land between them (see lease.py). expect_epoch without a fence is an
    error: the caller believes the save is fenced and it is not;
  - parts listed in `new_only` (e.g. health findings) change only when a new item appears.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, ContextManager

Collector = Callable[[], dict]


class StaleEpoch(RuntimeError):
    """The lease was taken after the caller read its epoch: this session must not save the shared snapshot."""


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


@dataclass
class Result:
    changed: bool
    lines: list[str] = field(default_factory=list)
    snapshot: dict = field(default_factory=dict)

    def render(self) -> str:
        return "\n".join(["CHANGED" if self.changed else "UNCHANGED", *self.lines])


def evaluate(collectors: dict[str, Collector], previous: dict, now: datetime,
             max_quiet: timedelta = timedelta(hours=4), new_only: frozenset = frozenset()) -> Result:
    old_parts = previous.get("parts", {})
    parts, lines = {}, []
    for name, collect in collectors.items():
        try:
            items = {str(k): digest(str(v)) for k, v in collect().items()}
        except Exception as error:  # an unreadable source must never look quiet
            parts[name] = old_parts.get(name, {})
            lines.append(f"{name}: unreadable ({type(error).__name__})")
            continue
        parts[name] = items
        before = old_parts.get(name, {})
        added = sorted(set(items) - set(before))
        removed = sorted(set(before) - set(items))
        edited = sorted(k for k in items.keys() & before.keys() if items[k] != before[k])
        if name in new_only:
            removed, edited = [], []
        bits = [f"+{k}" for k in added] + [f"-{k}" for k in removed] + [f"~{k}" for k in edited]
        if bits:
            shown = ", ".join(bits[:8]) + (f" (+{len(bits) - 8} more)" if len(bits) > 8 else "")
            lines.append(f"{name}: {shown}")
    lines = lines or []
    if "__unreadable__" in previous:
        lines.append(f"previous snapshot unreadable: {previous['__unreadable__']}")
    last_pass = previous.get("last_changed_at")
    if last_pass and datetime.fromisoformat(last_pass) > now:
        lines.append(f"last pass {last_pass} is in the future; forcing a pass")
        last_pass = now.isoformat()  # clamp: never save a future time
    elif not lines and last_pass and now - datetime.fromisoformat(last_pass) > max_quiet:
        lines.append(f"quiet for more than {max_quiet}; forcing a pass")
    if not previous:
        lines.append("no previous snapshot")
    changed = bool(lines)
    snapshot = {
        "parts": parts,
        "checked_at": now.isoformat(),
        "last_changed_at": now.isoformat() if changed else last_pass or now.isoformat(),
    }
    return Result(changed, lines, snapshot)


def run(collectors: dict[str, Collector], state_path: Path, persist: bool, now: datetime | None = None,
        fence: Callable[[], ContextManager[int]] | None = None, expect_epoch: int | None = None,
        **kwargs) -> Result:
    if fence is None and expect_epoch is not None:
        raise ValueError("expect_epoch passed but no fence is configured; the save would not be fenced")
    now = now or datetime.now(timezone.utc)
    state_path = Path(state_path)
    try:
        previous = json.loads(state_path.read_text()) if state_path.exists() else {}
        if not isinstance(previous, dict):
            raise ValueError("snapshot is not an object")
    except (json.JSONDecodeError, ValueError) as error:
        previous = {"__unreadable__": str(error)}   # marks the previous snapshot unavailable; saves a fresh one
    result = evaluate(collectors, previous, now, **kwargs)
    if persist:
        with fence() if fence is not None else nullcontext(expect_epoch) as current:
            if current != expect_epoch:
                raise StaleEpoch(f"lease epoch is {current}, caller holds {expect_epoch}; snapshot not saved")
            state_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=state_path.parent)
            with os.fdopen(fd, "w") as fh:
                json.dump(result.snapshot, fh, indent=2)
            os.replace(tmp, state_path)
    return result


def file_names(directory: Path) -> Collector:
    return lambda: {p.name: p.stat().st_size for p in sorted(Path(directory).glob("*")) if p.is_file()}


def ledger_rows(ledger: Path) -> Collector:
    def collect():
        rows = {}
        for line in Path(ledger).read_text().splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if cells and cells[0].startswith("R-"):
                rows[cells[0]] = line
        return rows
    return collect


if __name__ == "__main__":
    import sys
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    is_primary = "--no-save" not in sys.argv
    sources = {
        "ledger": ledger_rows(root / "LEDGER.md"),
        "reports": file_names(root / "reports"),
    }
    print(run(sources, root / "state" / "wake-gate.json", persist=is_primary).render())
