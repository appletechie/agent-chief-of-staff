"""#25 A: a crash mid-close cannot tear the archive or the note, and a torn line left by an older writer stops closes.

Before #25 the archive row and the proof note were appends. A process killed mid-append left part of a row on disk;
the retried close then spliced its row onto that fragment, closed_rows() could not see it, check() stayed clean and
the next new row was handed the same id. Now every write in close_row replaces the whole file atomically, and an
archive line that is not one whole row refuses every close until a person repairs it. First patch: karutoil (#26).
"""
import pathlib
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone

import ledger_store as ls

T0 = datetime(2026, 1, 16, 14, 0, tzinfo=timezone.utc)
HERE = pathlib.Path(__file__).resolve().parent
HEADER = "| id | key | closed | proof | last next action |\n|---|---|---|---|---|\n"
ROW = "| R-001 | — | 2026-01-16 14:00Z | tests: Ran 12 tests OK - on head abc1234 | close with evidence |\n"

# The child opens R-001, then dies inside atomic_write for one file: half the text is in the temporary file and the
# rename never happens. That is what a SIGKILL (or power cut) mid-write leaves behind.
CRASH = r"""
import os, sys, tempfile
from datetime import datetime, timezone
sys.path.insert(0, sys.argv[3])
import ledger_store as ls
root, step = sys.argv[1], sys.argv[2]
L = ls.Ledger(root)
L.write_row(None, next="close with evidence")
real = ls.atomic_write
def crash(path, text):
    if (step == "note" and "notes" in str(path)) or (step == "archive" and path.name.startswith("closed-")) \
            or (step == "ledger" and path.name == "LEDGER.md"):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        os.write(fd, text[:len(text) // 2].encode())
        os.kill(os.getpid(), 9)
    return real(path, text)
ls.atomic_write = crash
L.close_row("R-001", "tests", "Ran 12 tests OK", "on head abc1234", now=datetime(2026, 1, 16, 14, 0, tzinfo=timezone.utc))
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.ledger = ls.Ledger(self.root)

    def close(self, ledger=None):
        return (ledger or self.ledger).close_row("R-001", "tests", "Ran 12 tests OK", "on head abc1234", now=T0)

    def archive(self):
        return self.root / "ledger" / "closed-2026-01.md"


class AtomicClose(Base):
    def test_a_close_archives_once_and_the_next_row_gets_a_new_id(self):
        self.ledger.write_row(None, next="close with evidence")
        self.close()
        self.assertEqual(sorted(self.ledger.closed_ids()), ["R-001"])
        self.assertEqual(self.ledger.check()[0], [])
        self.assertEqual(self.ledger.write_row(None, next="really new work")["id"], "R-002")

    def test_the_note_holds_the_proof_once(self):
        self.ledger.write_row(None, next="close with evidence")
        self.close()
        self.assertEqual((self.root / "ledger" / "notes" / "R-001.md").read_text().count("## proof"), 1)

    def test_a_real_sigkill_mid_write_leaves_every_file_whole_and_the_retry_finishes(self):
        for step in ("note", "archive", "ledger"):
            with self.subTest(step=step):
                root = pathlib.Path(tempfile.mkdtemp())
                child = subprocess.run([sys.executable, "-I", "-c", CRASH, str(root), step, str(HERE)],
                                       capture_output=True, text=True, timeout=60)
                self.assertEqual(child.returncode, -9, child.stderr[-300:])
                ledger = ls.Ledger(root)
                self.assertEqual(ledger.archive_problems(), [])
                self.assertEqual([r["id"] for r in ledger.rows()], ["R-001"])
                self.close(ledger)
                self.assertEqual([r["id"] for r in ledger.closed_rows()], ["R-001"])
                self.assertEqual(ledger.rows(), [])
                self.assertEqual(ledger.check()[0], [])
                self.assertEqual((root / "ledger" / "notes" / "R-001.md").read_text().count("## proof"), 1)
                self.assertEqual(ledger.write_row(None, next="new work")["id"], "R-002")

    def test_a_spill_note_is_a_whole_file_write(self):
        row = self.ledger.write_row(None, next="word " * 200)
        notes = list((self.root / "ledger" / "notes").glob("*"))
        self.assertEqual([p.name for p in notes], [f"{row['id']}.md"])
        self.assertIn("next action: word word", notes[0].read_text())


class TornArchive(Base):
    """An archive torn by an append (an older version of this store, or any other writer) is refused, not read past."""

    def test_every_prefix_of_a_torn_append_refuses_the_close_and_keeps_the_id(self):
        line = ROW.rstrip("\n")
        for cut in (1, 3, 9, 20, 60, len(line) - 1):
            with self.subTest(cut=cut):
                self.setUp()
                self.ledger.write_row(None, next="close with evidence")
                self.archive().parent.mkdir(parents=True, exist_ok=True)
                self.archive().write_text(HEADER + line[:cut])
                with self.assertRaises(ls.ArchiveError):
                    self.close()
                self.assertEqual([r["id"] for r in self.ledger.rows()], ["R-001"])
                self.assertTrue(any("closed-2026-01.md line 3" in e for e in self.ledger.check()[0]))
                self.assertEqual(self.ledger.write_row(None, next="new work")["id"], "R-002")

    def test_spliced_rows_and_an_id_cell_after_text_are_reported(self):
        self.archive().parent.mkdir(parents=True)
        cases = {
            ROW.rstrip("\n") + ROW: "rows spliced",
            "|| R-001 | — | 2026-01-16 14:00Z | tests: Ran 12 tests OK - on head abc1234 | x |\n": "after other text",
            "x | R-001 | — | 2026-01-16 14:00Z | tests: Ran 12 tests OK - on head abc1234 | x |\n": "after other text",
            "| R-001 | — |\n": "2 cells",
        }
        for body, problem in cases.items():
            with self.subTest(body=body):
                self.archive().write_text(HEADER + body)
                found = self.ledger.archive_problems()
                self.assertEqual(len(found), 1)
                self.assertIn(problem, found[0]["problem"])
                self.assertEqual(found[0]["ids"], ["R-001"])
                self.assertEqual(self.ledger.next_free_id(), "R-002")

    def test_a_next_action_that_is_only_a_row_id_is_not_a_second_row(self):
        # Codex on #30: a cell holding just "R-002" is a value, not a row boundary.
        self.ledger.write_row(None, next="R-002")
        self.close()
        self.assertEqual(self.ledger.archive_problems(), [])
        self.ledger.write_row(None, next="close this too")
        self.ledger.close_row("R-002", "tests", "Ran 3 tests OK", "on head abc1234", now=T0)
        self.assertEqual(sorted(self.ledger.closed_ids()), ["R-001", "R-002"])

    def test_whole_rows_stay_clean(self):
        self.archive().parent.mkdir(parents=True)
        self.archive().write_text(HEADER + ROW + "| R-002 | — | 2026-01-16 14:00Z | folded: R-003 - carried into R-003 | x |\n")
        self.assertEqual(self.ledger.archive_problems(), [])
        self.assertEqual(self.ledger.check()[0], [])

    def test_a_partial_note_from_an_older_append_gets_the_full_proof_once(self):
        self.ledger.write_row(None, next="close with evidence")
        note = self.root / "ledger" / "notes" / "R-001.md"
        note.parent.mkdir(parents=True)
        note.write_text("\n## proof\nkind: tests\nref: Ran 12 te")
        self.close()
        self.assertEqual(note.read_text().count("ref: Ran 12 tests OK\non head abc1234\n"), 1)
        self.assertEqual(self.ledger.rows(), [])


if __name__ == "__main__":
    unittest.main()
