#!/usr/bin/env python3
"""A minimal ledger store: one Markdown table, locked writes, spill to notes, close with proof.

Layout (relative to a root directory):
    LEDGER.md                   open rows
    ledger/notes/<id>.md        overflow text per row
    ledger/closed-YYYY-MM.md    closed rows with proof
    state/ledger.lock           advisory lock

Standard library only. Uses fcntl, so POSIX; swap file_lock() for another primitive elsewhere.

Fencing: build the Ledger with fence=lease.fenced (a context manager that blocks takeovers and yields the lease's
current epoch) and every write must pass expect_epoch; a stale epoch is refused. The fence is taken before the
ledger lock and held for the whole write, so a takeover cannot land between the check and the write (see lease.py).
Passing expect_epoch without a fence is an error: the caller believes it is fenced and is not.
"""
from __future__ import annotations

import fcntl
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, ContextManager

COLUMNS = ["id", "key", "owner", "next action", "waits on", "due"]
FIELDS = ["id", "key", "owner", "next", "waits", "due"]
NONE = {"", "—", "-"}
ROW_BUDGET = 500  # characters, owner cell excluded; a warning, never an error
ROW_ID = re.compile(r"^R-(\d{3,})$")
KEY = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")
OWNER = re.compile(r"^(owner|coordinator(?: \(owner closed: [\w-]+\))?|worker-[0-9a-f]{6,})$")
DUE = re.compile(r"^(\d{2}-\d{2}(?: \d{2}:\d{2}Z)?|\d{4}-\d{2}-\d{2})$")
QUEUES = ("ACTIVE", "DECISION", "HANDS", "PEOPLE", "TIME")
LOCK_TIMEOUT = 20.0
SPILL_ORDER = ("next", "waits")  # free-text cells cut to fit the budget; id, key and due are short by format

_local = threading.local()  # re-entrancy is per thread; other threads must really wait


class LockTimeout(RuntimeError):
    pass


class StaleEpoch(RuntimeError):
    """The lease was taken after the caller read its epoch: re-read the lease before writing again."""


@contextmanager
def file_lock(path: Path, timeout: float = LOCK_TIMEOUT):
    """Exclusive advisory lock, re-entrant within this process. Records the holder for error messages."""
    path = Path(path).resolve()
    _held = _local.__dict__.setdefault("held", {})
    if path in _held:
        _held[path] += 1
        try:
            yield
        finally:
            _held[path] -= 1
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
                    holder = Path(path).read_text().strip() or "unknown"
                    raise LockTimeout(f"{path} held by {holder}")
                time.sleep(0.05)
        os.ftruncate(fd, 0)
        os.write(fd, f"pid={os.getpid()}".encode())
        _held[path] = 1
        try:
            yield
        finally:
            del _held[path]
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class Ledger:
    def __init__(self, root: Path, fence: Callable[[], ContextManager[int]] | None = None,
                 require_key: Callable[[dict], bool] | None = None):
        """require_key(row) -> True where your policy needs an issue key, e.g. lambda r: r["owner"].startswith("worker-")."""
        self.root = Path(root)
        self.path = self.root / "LEDGER.md"
        self.notes = self.root / "ledger" / "notes"
        self.lock = self.root / "state" / "ledger.lock"
        self.fence = fence
        self.require_key = require_key

    @contextmanager
    def _fenced(self, expect_epoch: int | None):
        """Hold the fence (lease lock, shared) for the whole write; refuse a stale or missing epoch."""
        if self.fence is None:
            if expect_epoch is not None:
                raise ValueError("expect_epoch passed but no fence is configured; the write would not be fenced")
            yield
            return
        with self.fence() as current:
            if expect_epoch != current:
                raise StaleEpoch(f"lease epoch is {current}, caller holds {expect_epoch}; re-read the lease")
            yield

    # ---------- reading ----------

    def _lines(self) -> list[str]:
        if not self.path.exists():
            return ["# Ledger", "", "## Open", "", table_line(COLUMNS), "|" + "---|" * len(COLUMNS)]
        return self.path.read_text().splitlines()

    def rows(self) -> list[dict]:
        out = []
        for n, line in enumerate(self._lines(), 1):
            cells = split(line)
            if cells and ROW_ID.match(cells[0]):
                row = dict(zip(FIELDS, cells + [""] * (len(FIELDS) - len(cells))))
                row["_line"], row["_cols"] = n, len(cells)
                out.append(row)
        return out

    def closed_ids(self) -> set[str]:
        ids = set()
        for archive in sorted((self.root / "ledger").glob("closed-*.md")):
            for line in archive.read_text().splitlines():
                cells = split(line)
                if cells and ROW_ID.match(cells[0]):
                    ids.add(cells[0])
        return ids

    def next_free_id(self) -> str:
        used = {r["id"] for r in self.rows()} | self.closed_ids()
        top = max((int(ROW_ID.match(i).group(1)) for i in used), default=0)
        return f"R-{top + 1:03d}"

    # ---------- writing ----------

    def write_row(self, row_id: str | None, expect_epoch: int | None = None, **fields) -> dict:
        """Create (row_id=None) or update a row. Long text spills to notes; returns the row written."""
        with self._fenced(expect_epoch), file_lock(self.lock):  # lock order: fence, then store
            lines = self._lines()
            rows = {r["id"]: r for r in self.rows()}
            if row_id is None:
                row_id = self.next_free_id()
                current = {"id": row_id, "key": "—", "owner": "coordinator", "next": "", "waits": "—", "due": "—"}
            elif row_id in rows:
                current = {k: rows[row_id][k] for k in FIELDS}
            else:
                raise KeyError(f"{row_id} is not open; pass row_id=None to open a new row")
            current.update({k: clean(v) for k, v in fields.items() if k in FIELDS})
            if not current["next"] or current["next"] in NONE:
                raise ValueError(f"{row_id}: next action is required")
            current = self._spill(current)
            new_line = table_line([current[k] for k in FIELDS])
            if row_id in rows:
                lines[rows[row_id]["_line"] - 1] = new_line
            else:
                lines.append(new_line)
            atomic_write(self.path, "\n".join(lines) + "\n")
            return current

    def close_row(self, row_id: str, proof: str, now: datetime | None = None, expect_epoch: int | None = None) -> str:
        """Move a row to ledger/closed-YYYY-MM.md with its proof. Proof is mandatory.

        The archive line is written before the ledger line is removed. A crash between the two leaves the id in
        both places, which check() reports; finish by re-running close_row or deleting the ledger line."""
        if not proof or not proof.strip():
            raise ValueError("a row closes only with proof")
        now = now or datetime.now(timezone.utc)
        with self._fenced(expect_epoch), file_lock(self.lock):
            lines = self._lines()
            match = [r for r in self.rows() if r["id"] == row_id]
            if not match:
                raise KeyError(f"{row_id} is not open")
            row = match[0]
            archive = self.root / "ledger" / f"closed-{now:%Y-%m}.md"
            archive.parent.mkdir(parents=True, exist_ok=True)
            if not archive.exists():
                archive.write_text("| id | key | closed | proof | last next action |\n|---|---|---|---|---|\n")
            with archive.open("a") as fh:
                fh.write(table_line([row_id, row["key"], f"{now:%Y-%m-%d %H:%MZ}", clean(proof), row["next"]]) + "\n")
            del lines[row["_line"] - 1]
            atomic_write(self.path, "\n".join(lines) + "\n")
            return str(archive)

    def _spill(self, row: dict) -> dict:
        """Over budget: save the full text to the row's note, then cut next action, then waits on, until it fits.

        In waits on, a clause that starts with "then" (a later step, often the owner's) is never cut: the longest
        other clause is cut from its end, so the first clause still sets the queue and the later step still shows."""
        if row_chars(row) <= ROW_BUDGET:
            return row
        self.notes.mkdir(parents=True, exist_ok=True)
        note = self.notes / f"{row['id']}.md"
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%MZ")
        with note.open("a") as fh:
            fh.write(f"\n## {stamp}\nnext action: {row['next']}\nwaits on: {row['waits']}\n")
        row = dict(row)
        pointer = f" (detail: ledger/notes/{row['id']}.md)"
        for k in SPILL_ORDER:
            over = row_chars(row) - ROW_BUDGET
            if over <= 0:
                break
            row[k] = cut_waits(row[k], over, pointer) if k == "waits" else cut(row[k], over, pointer)
        return row

    # ---------- checking ----------

    def check(self) -> tuple[list[str], list[str]]:
        """Return (errors, warnings). Errors should stop any further write in the same turn."""
        errors, warnings = [], []
        lines = self._lines()
        header = next((split(l) for l in lines if split(l)[:1] == ["id"]), None)
        if header != COLUMNS:
            errors.append("header must be | " + " | ".join(COLUMNS) + " |")
        seen, closed = set(), self.closed_ids()
        for r in self.rows():
            where = f"line {r['_line']} {r['id']}"
            if r["_cols"] != len(COLUMNS):
                errors.append(f"{where}: {r['_cols']} columns, expected {len(COLUMNS)}")
                continue
            if r["id"] in seen:
                errors.append(f"{where}: duplicate id")
            if r["id"] in closed:
                errors.append(f"{where}: id also appears in a closed archive")
            seen.add(r["id"])
            if r["key"] not in NONE and not KEY.match(r["key"]):
                errors.append(f"{where}: key must be one issue key or —")
            if r["next"] in NONE:
                errors.append(f"{where}: no next action")
            if not OWNER.match(r["owner"]):
                errors.append(f"{where}: owner must be owner, coordinator or a worker id")
            if r["due"] not in NONE and not DUE.match(r["due"]):
                errors.append(f"{where}: due must be MM-DD, MM-DD HH:MMZ or YYYY-MM-DD")
            if row_chars(r) > ROW_BUDGET:
                warnings.append(f"{where}: {row_chars(r)} chars, budget {ROW_BUDGET}")
            if self.require_key and r["key"] in NONE and self.require_key(r):
                warnings.append(f"{where}: no issue key, and policy requires one for this row")
        return errors, warnings


def queue_of(row: dict) -> str:
    """Derive a row's queue from its owner and the first clause of waits-on. Later clauses start with 'then'.

    A row the owner holds with nothing in waits-on is DECISION, not ACTIVE: the brief lists it with options,
    and no pass dispatches a worker onto the owner's own task."""
    first = next((c.strip() for c in (row.get("waits") or "").split(";")
                  if c.strip() and not c.strip().lower().startswith("then")), "")
    low = first.lower()
    if first in NONE:
        return "DECISION" if (row.get("owner") or "").strip() == "owner" else "ACTIVE"
    if low.startswith(("worker", "coordinator")):
        return "ACTIVE"
    if low.startswith("owner"):
        return "HANDS" if "hands" in low else "DECISION"
    if low.startswith(("time", "timer", "date", "soak")) or re.search(r"\b\d{2}-\d{2}\b|\b\d{2}:\d{2}\b", first):
        return "TIME"
    return "PEOPLE"


def split(line: str) -> list[str]:
    line = line.strip()
    if not (line.startswith("|") and line.endswith("|")):
        return []
    return [c.strip() for c in line[1:-1].split("|")]


def table_line(cells) -> str:
    return "| " + " | ".join(cells) + " |"


def clean(value) -> str:
    return str(value).replace("|", "/").replace("\n", " ").strip()


def row_chars(row: dict) -> int:
    return len(table_line([row[k] for k in FIELDS if k != "owner"]))


def cut(text: str, over: int, pointer: str) -> str:
    keep = max(len(text) - over - len(pointer), 40)
    return text[:keep].rstrip() + pointer if keep < len(text) else text


def cut_waits(text: str, over: int, pointer: str) -> str:
    clauses = text.split(";")
    cuttable = [i for i, c in enumerate(clauses) if not c.strip().lower().startswith("then")]
    if not cuttable:
        return text
    i = max(cuttable, key=lambda j: len(clauses[j]))
    clauses[i] = cut(clauses[i], over, pointer)
    return ";".join(clauses)


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as root:
        ledger = Ledger(Path(root))
        a = ledger.write_row(None, key="ISSUE-1", next="Fix the retry loop", waits="—")
        b = ledger.write_row(None, owner="owner", next="Pick a price: A or B", waits="owner: decision", due="01-16")
        ledger.write_row(a["id"], owner="worker-7f3a2c")
        print(ledger.path.read_text())
        print("queues:", {r["id"]: queue_of(r) for r in ledger.rows()})
        print("closed to:", ledger.close_row(b["id"], proof='owner: "A" 2026-01-16 14:02Z'))
        print("next id:", ledger.next_free_id(), "(never reuses", b["id"] + ")")
        print("check:", ledger.check())
