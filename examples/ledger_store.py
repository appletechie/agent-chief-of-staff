#!/usr/bin/env python3
"""A minimal ledger store: one Markdown table, locked writes, spill to notes, close with proof.

Layout (relative to a root directory):
    LEDGER.md                   open rows
    ledger/notes/<id>.md        overflow text per row
    ledger/closed-YYYY-MM.md    closed rows with proof
    state/ledger.lock           advisory lock

Standard library only. Uses fcntl, so POSIX; swap file_lock() for another primitive elsewhere.

Closing takes structured proof: close_row(row_id, kind, ref, text) with kind in PROOF_KINDS and a ref the kind can
be checked against (a SHA, a run id, a comment id, a read-back, the owner's quoted words with the time). A claim
("verified", "ok", "the worker said it works") or prose with no checkable token is refused, and check() fails any
archived row whose proof cell is not `kind: ref - text`. An optional can_close(row, proof) policy (for example
pr_merge_policy(lookup)) ties the proof to outside facts.

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
from dataclasses import dataclass
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
PROOF_KINDS = ("tests", "ci", "merged", "deployed", "config", "message", "owner", "folded", "void")
NOT_PROOF_WORDS = {"verified", "ok", "okay", "done", "works", "work", "worked", "working", "fine", "good", "confirmed",
                   "green", "passed", "passing", "pass", "success", "successful", "lgtm", "complete", "completed",
                   "fixed", "yes", "looks", "all", "it", "is", "the", "worker", "said", "says", "everything", "should",
                   "now", "tested", "and", "be"}
SHA = re.compile(r"\b[0-9a-f]{7,40}\b")
RUN_ID = re.compile(r"\b\d{6,}\b")
COUNTS = re.compile(r"\bRan \d+\b|\b\d+\s*(?:tests?|passed|passing|failed|failures|errors|skipped)\b|\b\d+\s*/\s*\d+\b"
                    r"|\b\d+ OK\b|\bOK \(", re.I)
CI_SUCCESS = re.compile(r"\bsuccess\b", re.I)
# A red outcome in a tests/ci/deployed ref refuses even when "success" or a count also appears (#22). Zero counts
# ("0 failed", "failures=0", "no errors") are removed first, so a clean summary stays green.
ZERO_RED = re.compile(r"(?<![\w.])0\s*(?:failed|failures?|errors?)(?![A-Za-z])|(?<![A-Za-z])(?:failed|failures?|errors?)\s*[=:]\s*0(?!\d)"
                      r"|\bno\s+(?:failures?|errors?|failed)(?![A-Za-z])", re.I)
RED_OUTCOME = re.compile(r"(?<![A-Za-z])(?:fail(?:ed|ure|ures|ing|s)?|errors?|cancell?ed|canceled|timed[ _-]?out|timeout"
                         r"|unhealthy)(?![A-Za-z])|\b(?:no|not|without)\s+(?:a\s+)?success|\bunsuccessful\b|\bnot\s+healthy\b"
                         r"|\brequired\b[^;,]*\bskipped\b|\bskipped\b[^;,]*\brequired\b", re.I)
RED_COUNT = re.compile(r"(?<![\w.])[1-9]\d*\s*(?:failed|failures?|errors?)(?![A-Za-z])"
                       r"|(?<![A-Za-z])(?:failed|failures?|errors?)\s*[=:]\s*[1-9]|\bFAILED\b")
NOTHING_RAN = re.compile(r"\bRan 0 tests?\b|(?<![\w.])0\s*(?:tests?|passed)\b", re.I)
SOME_RAN = re.compile(r"\bRan [1-9]|(?<![\w.])[1-9]\d*\s*(?:tests?|passed|passing)\b|\bOK\b|(?<![\w.])(\d+)\s*/\s*\1\b", re.I)
EXIT_CODE = re.compile(r"\bexit(?:\s+code)?\s*[:=]?\s*(-?\d+)", re.I)
PASS_RATIO = re.compile(r"(?<![\w.])(\d+)\s*/\s*(\d+)\s*(?:tests?|passed|passing|ok|green)\b", re.I)
LIVE_STATUS = re.compile(r"(?<![\w.-])(?:[1-5]\d\d|healthy|success|passed|PASS)(?![\w-]|\.\w)")  # not inside an id
ID_TOKEN = re.compile(r"(?<![\w.-])(?=[\w.-]*\d)[\w.-]{4,}(?![\w.-])")
READ_BACK = re.compile(r"read[ -]?back|readback|\w\s*(?:==?|->|→)\s*\S", re.I)
MESSAGE_ID = re.compile(r"\d{4,}")
QUOTED = re.compile(r"[\"'“‘][^\"“”‘’]+?[\"'”’]")
WHEN = re.compile(r"\b\d{1,2}:\d\d(?!\d)")  # a time of day, not a bare date
VOID_REASON = re.compile(r"\b(?:tests?|testing|fixture|accident\w*|mistake\w*|in error|by error|typo|stray|created by)\b",
                         re.I)
PROOF_CELL = re.compile(r"^(?P<kind>[a-z]+): (?P<ref>.+?) - (?P<text>.+)$")
PR_REF = re.compile(r"(?<![\w./-])([A-Za-z0-9][\w-]*/[\w.-]+)#(\d+)\b|github\.com/([\w-]+/[\w.-]+)/pull/(\d+)", re.I)

_local = threading.local()  # re-entrancy is per thread; other threads must really wait


class LockTimeout(RuntimeError):
    pass


class ProofRefused(ValueError):
    """The proof is not evidence: unknown kind, empty or prose-only ref, or a claim such as "verified" alone."""


class CloseRefused(ValueError):
    """The supplied close policy rejected this row/proof pair (PR not merged, review still open, ...)."""


@dataclass(frozen=True)
class Proof:
    kind: str
    ref: str
    text: str

    def __str__(self) -> str:
        return f"{self.kind}: {self.ref} - {self.text}"


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
                 require_key: Callable[[dict], bool] | None = None,
                 can_close: Callable[[dict, Proof], str | None] | None = None,
                 proof_since: datetime | None = None):
        """require_key(row) -> True where your policy needs an issue key, e.g. lambda r: r["owner"].startswith("worker-").
        can_close(row, proof) -> None to allow, else the reason to refuse (see pr_merge_policy).
        proof_since: check() leaves archived rows closed before this time alone (set it to when you adopted
        structured proof); None checks every archived row."""
        self.root = Path(root)
        self.path = self.root / "LEDGER.md"
        self.notes = self.root / "ledger" / "notes"
        self.lock = self.root / "state" / "ledger.lock"
        self.fence = fence
        self.require_key = require_key
        self.can_close = can_close
        self.proof_since = proof_since

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

    def closed_rows(self) -> list[dict]:
        out = []
        for archive in sorted((self.root / "ledger").glob("closed-*.md")):
            for line in archive.read_text().splitlines():
                cells = split(line)
                if cells and ROW_ID.match(cells[0]):
                    cells += [""] * (5 - len(cells))
                    out.append({"id": cells[0], "key": cells[1], "closed": cells[2], "proof": cells[3],
                                "_file": archive.name})
        return out

    def closed_ids(self) -> set[str]:
        return {r["id"] for r in self.closed_rows()}

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

    def close_row(self, row_id: str, kind: str, ref: str, text: str, now: datetime | None = None,
                  expect_epoch: int | None = None) -> str:
        """Move a row to ledger/closed-YYYY-MM.md with structured proof (see PROOF_KINDS and proof_problem).

        Refused before anything is written: a proof that is not evidence (ProofRefused), a folded target that is not
        an open row mentioning this one, or a can_close policy that says no (CloseRefused). The full proof goes to
        the row's note verbatim, the archive line is written next, then the ledger line is removed. A crash between
        the two leaves the id in both places, which check() reports; finish by re-running the same close_row call: it
        finds its own archived row (same id, kind and ref), appends nothing and removes the ledger line. An id already
        archived with other proof is refused (ids are never reused)."""
        proof = Proof(kind, " ".join(str(ref or "").split()), " ".join(str(text or "").split()))
        problem = proof_problem(proof.kind, proof.ref, proof.text)
        if problem:
            raise ProofRefused(f"{row_id} not closed: {problem}")
        now = now or datetime.now(timezone.utc)
        with self._fenced(expect_epoch), file_lock(self.lock):
            lines = self._lines()
            rows = self.rows()
            match = [r for r in rows if r["id"] == row_id]
            if not match:
                raise KeyError(f"{row_id} is not open")
            row = match[0]
            if kind == "folded":
                reason = self._fold_problem(row_id, proof.ref, rows)
                if reason:
                    raise CloseRefused(reason)
            if self.can_close is not None:
                reason = self.can_close(row, proof)
                if reason:
                    raise CloseRefused(reason)
            # A retry after a crash finds its own archived row: compare as archived (clean() rewrites pipes).
            archived = [r for r in self.closed_rows() if r["id"] == row_id]
            same = next((r for r in archived if (p := parse_proof(r["proof"])) and (p.kind, p.ref) == (proof.kind, clean(proof.ref))),
                        None)
            if archived and same is None:
                raise CloseRefused(f"{row_id} is already archived in {archived[0]['_file']} with other proof "
                                   f"({archived[0]['proof']}); ids are never reused")
            # store the proof's full text in the row's note before the archive (a table) cuts or rewrites it.
            self.notes.mkdir(parents=True, exist_ok=True)
            note_path, entry = self.notes / f"{row_id}.md", f"## proof\nkind: {proof.kind}\nref: {proof.ref}\n{proof.text}\n"
            if not (note_path.exists() and entry in note_path.read_text()):
                with note_path.open("a") as note:
                    note.write("\n" + entry)
            if same is not None:
                archive = self.root / "ledger" / same["_file"]
            else:
                archive = self.root / "ledger" / f"closed-{now:%Y-%m}.md"
                archive.parent.mkdir(parents=True, exist_ok=True)
                if not archive.exists():
                    archive.write_text("| id | key | closed | proof | last next action |\n|---|---|---|---|---|\n")
                with archive.open("a") as fh:
                    fh.write(table_line([row_id, row["key"], f"{now:%Y-%m-%d %H:%MZ}", clean(str(proof)), row["next"]]) + "\n")
            del lines[row["_line"] - 1]
            atomic_write(self.path, "\n".join(lines) + "\n")
            return str(archive)

    def _fold_problem(self, row_id: str, target: str, rows: list[dict]) -> str | None:
        """A folded row's target must be another open row that already names it (in a cell or its note)."""
        into = next((r for r in rows if r["id"] == target and r["id"] != row_id), None)
        if into is None:
            return f"folded target {target} is not another open row"
        note = self.notes / f"{target}.md"
        named = re.compile(rf"(?<![\w-]){re.escape(row_id)}(?!\w)")
        if not any(named.search(str(into[k])) for k in FIELDS if k != "id") and not (
                note.exists() and named.search(note.read_text())):
            return f"folded target {target} does not mention {row_id}; carry its open items there first"
        return None

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
        errors += self.closed_proof_errors()
        dup_errors, dup_warnings = self.duplicate_archive_ids()
        return errors + dup_errors, warnings + dup_warnings

    def duplicate_archive_ids(self) -> tuple[list[str], list[str]]:
        """An id archived more than once (a retried close that appended twice, or a hand edit) claims history twice.
        It is an error when any copy was closed at or after proof_since (always, when None), a warning otherwise."""
        copies: dict[str, list[dict]] = {}
        for r in self.closed_rows():
            copies.setdefault(r["id"], []).append(r)
        errors, warnings = [], []
        for row_id, found in sorted(copies.items()):
            if len(found) < 2:
                continue
            where = ", ".join(f"{r['_file']} ({r['closed']})" for r in found)
            old = self.proof_since is not None and all(
                (when := closed_time(r["closed"])) is not None and when < self.proof_since for r in found)
            (warnings if old else errors).append(f"closed archive repeats id {row_id} {len(found)} times: {where}")
        return errors, warnings

    def closed_proof_errors(self) -> list[str]:
        """Archived rows closed at or after proof_since (all, when None) must carry `kind: ref - text` proof."""
        errors = []
        for r in self.closed_rows():
            where = f"{r['_file']} {r['id']}"
            when = closed_time(r["closed"])
            if self.proof_since is not None and when is not None and when < self.proof_since:
                continue
            if when is None:
                errors.append(f"{where}: closed time must be YYYY-MM-DD HH:MMZ")
                continue
            proof = parse_proof(r["proof"])
            if proof is None:
                errors.append(f"{where}: proof must be `kind: ref - text` (close rows only through close_row)")
                continue
            problem = proof_problem(proof.kind, proof.ref, proof.text)
            if problem:
                errors.append(f"{where}: {problem}")
        return errors


def closed_time(cell: str) -> datetime | None:
    try:
        return datetime.strptime(cell, "%Y-%m-%d %H:%MZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def not_proof(value: str) -> bool:
    """True for a claim with nothing checkable in it: "verified", "ok", "done", "the worker said it works"."""
    words = re.findall(r"[a-z']+", (value or "").lower().replace("'s", ""))
    return not re.search(r"\d", value or "") and all(w in NOT_PROOF_WORDS for w in words)


def proof_problem(kind: str, ref: str, text: str) -> str | None:
    """None when (kind, ref, text) is structured proof, else why not. Static, so check() can re-run it on the
    archive. The ref carries the checkable token; text says what it proves.

    tests    counts ("Ran 88 tests ... OK", "64/64") or a CI run id
    ci       a run id or head SHA plus the conclusion "success"
    merged   the merge commit SHA, alone
    deployed a deploy or run id plus a live check status ("readyz 200", "healthy")
    config   the setting read back ("autoDeploy=false", "read back ...")
    message  the message, comment or review id
    owner    the owner's words in quotes, with the time
    folded   the open row id it was folded into (that row must mention this one; close_row checks)
    void     for a test or accidental row; the reason says which

    For tests, ci and deployed the outcome must be good, not only the shape: see red_outcome."""
    ref, text = " ".join(str(ref or "").split()), " ".join(str(text or "").split())
    if kind not in PROOF_KINDS:
        return f"{kind!r} is not a proof kind: {', '.join(PROOF_KINDS)}"
    if not ref:
        return f"a {kind} proof needs a ref"
    if not text:
        return f"a {kind} proof needs a text saying what the ref proves"
    if " - " in ref:
        return "the ref must not contain ' - ' (it separates ref from text in the archive)"
    if not_proof(ref) or not_proof(text):
        return f"{ref if not_proof(ref) else text!r} is a claim, not proof"
    red = red_outcome(kind, ref, text)
    if red:
        return red
    if kind == "tests" and not (COUNTS.search(ref) or RUN_ID.search(ref)):
        return "a tests proof needs the counts or a run id, not prose"
    if kind == "ci" and not ((RUN_ID.search(ref) or SHA.search(ref)) and CI_SUCCESS.search(ref)):
        return "a ci proof needs a run id or head SHA plus the conclusion (success)"
    if kind == "merged" and not re.fullmatch(r"[0-9a-f]{7,40}", ref):
        return "a merged proof is the merge commit SHA"
    if kind == "deployed":
        if not any(ID_TOKEN.search(ref[:m.start()] + " " + ref[m.end():]) for m in LIVE_STATUS.finditer(ref)):
            return "a deployed proof needs a deploy or run id plus a live check status"
    if kind == "config" and not READ_BACK.search(ref):
        return "a config proof needs the setting read back (name=value)"
    if kind == "message" and not MESSAGE_ID.search(ref):
        return "a message proof needs the message, comment or review id"
    if kind == "owner" and not (QUOTED.search(ref) and WHEN.search(ref)):
        return "an owner proof needs the owner's quoted words and the time"
    if kind == "folded" and not ROW_ID.match(ref):
        return "a folded proof's ref is the open row id it was folded into"
    if kind == "void" and not VOID_REASON.search(f"{ref} {text}"):
        return "void is only for a test or accidental row; say which"
    return None


def red_outcome(kind: str, ref: str, text: str) -> str | None:
    """Why a tests/ci/deployed proof reports a failed, cancelled, timed-out or empty run, or None (#22). The ref is
    the result, so any red word in it refuses, even beside "success": FAILED, a nonzero failure or error count,
    cancelled, timed out, "no success", a required check skipped, a nonzero exit, 63/64, nothing ran, a 4xx/5xx live
    status, unhealthy. The text describes, so only a nonzero failure count or a FAILED summary in it refuses."""
    if kind not in ("tests", "ci", "deployed"):
        return None
    ref_red = RED_OUTCOME.search(ZERO_RED.sub(" ", ref))
    if ref_red:
        return f"the {kind} proof reports {ref_red.group(0)!r}; a failed, cancelled or timed-out run is not done work"
    text_red = RED_COUNT.search(ZERO_RED.sub(" ", text))
    if text_red:
        return f"the {kind} proof's text reports {text_red.group(0)!r}; a failed run is not done work"
    if kind == "tests":
        code = EXIT_CODE.search(ref)
        if code and int(code.group(1)) != 0:
            return f"a tests proof needs exit 0, not exit {code.group(1)}"
        ratio = PASS_RATIO.search(ref)
        if ratio and int(ratio.group(1)) != int(ratio.group(2)):
            return f"a tests proof of {ratio.group(1)}/{ratio.group(2)} is not every test passing"
        if NOTHING_RAN.search(ref) or (re.search(r"\bskipped\b", ref, re.I) and not SOME_RAN.search(ref)):
            return "a tests proof where no test ran proves nothing"
    if kind == "deployed":
        bad = [m.group(0) for m in LIVE_STATUS.finditer(ref) if m.group(0).isdigit() and not 200 <= int(m.group(0)) < 400]
        if bad:
            return f"a deployed proof's live check returned {bad[0]}; only a 2xx/3xx status closes"
    return None


def parse_proof(cell: str) -> Proof | None:
    m = PROOF_CELL.match((cell or "").strip())
    return Proof(m["kind"], m["ref"], m["text"]) if m else None


def pr_refs(text: str) -> list[tuple[str, int]]:
    """Every GitHub PR named in text, as (owner/repo, number): owner/repo#N or a github.com pull URL. Host, owner
    and repo are case-insensitive on GitHub, so GITHUB.COM/Org/App and org/app name one PR, returned lower case (#22)."""
    out = []
    for m in PR_REF.finditer(text or ""):
        pr = (m[1].lower(), int(m[2])) if m[1] else (m[3].lower(), int(m[4]))
        if pr not in out:
            out.append(pr)
    return out


def compose_close_policies(*policies: Callable[[dict, Proof], str | None] | None) -> Callable[[dict, Proof], str | None]:
    """One can_close policy from several (for example merge SHA, then a clean review): the first refusal wins."""
    def can_close(row: dict, proof: Proof) -> str | None:
        for policy in policies:
            reason = policy(row, proof) if policy is not None else None
            if reason:
                return reason
        return None
    return can_close


def pr_merge_policy(lookup: Callable[[str, int], str | None]) -> Callable[[dict, Proof], str | None]:
    """A can_close policy: a row whose cells name a PR closes as `merged` only with that PR's merge SHA.

    lookup(repo, number) returns the merge commit SHA, or None when the PR is not merged; inject your VCS client
    (for GitHub: `gh pr view N -R repo --json state,mergeCommit`). A lookup error refuses the close."""
    def can_close(row: dict, proof: Proof) -> str | None:
        prs = pr_refs(" ".join(str(row.get(k, "")) for k in FIELDS))
        if proof.kind != "merged" or not prs:
            return None
        seen = []
        for repo, number in prs:
            try:
                sha = lookup(repo, number)
            except Exception as error:  # a broken lookup must not read as "allowed"
                return f"cannot read {repo}#{number} ({error})"
            if sha and sha.lower().startswith(proof.ref.lower()):
                return None
            seen.append(f"{repo}#{number} " + (f"merged {sha[:7]}" if sha else "not merged"))
        return f"{proof.ref} is not the merge SHA of the row's PR ({'; '.join(seen)})"
    return can_close


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
        try:
            ledger.close_row(a["id"], "tests", "verified", "the worker said so")
        except ProofRefused as refused:
            print("refused:", refused)
        print("closed to:", ledger.close_row(b["id"], "owner", '"A" 2026-01-16 14:02Z', "price A chosen"))
        print("next id:", ledger.next_free_id(), "(never reuses", b["id"] + ")")
        print("check:", ledger.check())
