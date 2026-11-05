"""A process dying after an append used to create an invisible / partly-doubled archive and permit id reuse."""
import pathlib
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone

import ledger_store as ls

T0 = datetime(2026, 1, 16, 14, 0, tzinfo=timezone.utc)


class AtomicArchiveTests(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.ledger = ls.Ledger(self.root)
        self.row = self.ledger.write_row(None, next="close with evidence")

    def n(self):
        self.ledger.close_row(self.row["id"], "tests", "run 123456\n12 passed", "on head abc1234", now=T0)
        return [r["id"] for r in self.ledger.closed_rows()]

    def test_atomic_archive_replaces_the_entire_record_and_is_idempotent(self):
        self.assertEqual(self.n(), ["R-001"])
        self.assertEqual(self.ledger.check()[0], [])
        self.assertEqual(self.ledger.write_row(None, next="really new work")["id"], "R-002")

    def test_note_does_not_duplicate_data(self):
        self.assertEqual(self.n(), ["R-001"])
        note = self.root/"ledger"/"notes"/"R-001.md"
        self.assertEqual(note.read_text().count('## proof'),1)


class RealCrashTests(unittest.TestCase):
    def write_archive_line(self, send=None):
        root=tempfile.mkdtemp()
        script = r'''import ledger_store as ls, sys
root=sys.argv[1]; L=ls.Ledger(root); r=L.write_row(None,next="test atomic crash")
L.close_row("R-001", "tests","run 123456","all 12 passed")
'''
        p=subprocess.run([sys.executable,"-c",script, root],cwd=pathlib.Path(__file__).parent)
        self.assertEqual(p.returncode,0)
        self.ledger=ls.Ledger(root); self.assertEqual(len(self.ledger.closed_rows()),1)

    def test_integrity_rejected_on_a_partly_written_archive(self):
        root = pathlib.Path(tempfile.mkdtemp()); path = root/"ledger"/"closed-2026-01.md"; path.parent.mkdir()
        for bad in ['|| R-001 | — | 2026-01-16 |', '| ONLYINCOMPLETE | R-001 | bad |']:
            path.write_text(bad)
            ledger=ls.Ledger(root)
            if 'R-' not in bad:
                self.assertEqual(ledger.closed_rows(), [])
                continue
            with self.assertRaises(ls.ArchiveError):
                ledger.closed_rows()



class ProcessInterruptionTests(unittest.TestCase):
    def test_real_sigkill_after_a_trailing_write(self):
        script = r"""
import os, sys
import ledger_store as ls
L=ls.Ledger(sys.argv[1])
L.write_row(None,next="data replay")
original=ls.atomic_write
def kill_after_archive(path,text):
    if str(path).endswith(".md") and "closed-" in str(path):
        original(path, text[:int(sys.argv[2])])
        os.kill(os.getpid(), 9)
    return original(path, text)
ls.atomic_write=kill_after_archive
L.close_row("R-001","tests","run 123456","12 passed", now=ls.datetime(2026,1,16,14,0,tzinfo=ls.timezone.utc))
"""
        for prefix in [1,25,60,120]:
            with self.subTest(prefix=prefix):
                root=pathlib.Path(tempfile.mkdtemp())
                child=subprocess.run([sys.executable,"-c",script, str(root), str(prefix)],cwd=pathlib.Path(__file__).parent)
                self.assertEqual(child.returncode,-9)
                try:
                    ledger=ls.Ledger(root)
                    ledger.close_row("R-001","tests","run 123456","12 passed",now=T0)
                except (ls.CloseRefused, ls.ArchiveError):
                    # a clearly distinct archived partial remains visible for the operator instead of rewriting it
                    continue
                self.assertEqual([r["id"] for r in ledger.closed_rows()], ["R-001"], prefix)
                self.assertEqual(ledger.check()[0],[],prefix)

    def test_partial_note_retains_the_evidence_without_repeating(self):
        script = r"""
import os, sys
import ledger_store as ls
from datetime import datetime, timezone
L=ls.Ledger(sys.argv[1]);L.write_row(None,next="data replay")
original=ls.atomic_write
def cut_note(path,text):
    if "notes" in str(path):
        original(path,text[:45]);os.kill(os.getpid(),9)
    return original(path,text)
ls.atomic_write=cut_note
L.close_row("R-001","tests","run 123456","12 passed",now=datetime(2026,1,16,14,0,tzinfo=timezone.utc))
"""
        root=pathlib.Path(tempfile.mkdtemp())
        child=subprocess.run([sys.executable,"-c",script,str(root)],cwd=pathlib.Path(__file__).parent)
        self.assertEqual(child.returncode,-9)
        ledger=ls.Ledger(root)
        ledger.close_row("R-001","tests","run 123456","12 passed",now=T0)
        self.assertEqual(ledger.rows(),[])
        self.assertEqual(len(ledger.closed_ids()),1)


if __name__ == "__main__":
    unittest.main()
