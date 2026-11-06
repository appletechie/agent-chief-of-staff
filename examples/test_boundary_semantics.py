"""#33: these four tests use shapes which survive doc-token parsing but must not alter meaning."""
import pathlib
import tempfile
import unittest
from datetime import datetime, datetime as dt, timedelta, timezone
import ledger_store as ls
import merge_gate as mg
import timers as tm

T0=datetime(2026,1,16,14,0,tzinfo=timezone.utc)


class BoundarySemantics(unittest.TestCase):
    def test_unicode_separators_cannot_drop_a_ledger_row(self):
        root=pathlib.Path(tempfile.mkdtemp()); led=ls.Ledger(root)
        first=led.write_row(None,next="prefix \u2028suffix")
        second=led.write_row(None,next="next valid action")
        self.assertEqual((first["id"],second["id"]),("R-001","R-002"))

    def test_any_missing_merged_pr_blocks_a_row_with_several(self):
        root=pathlib.Path(tempfile.mkdtemp())
        calls=[]
        def lookup(repo,num):
            calls.append(num)
            return "abcdef1" if num==1 else None
        led=ls.Ledger(root,can_close=ls.pr_merge_policy(lookup))
        row=led.write_row(None,next="Merge org/app#1 and org/app#2, then finish")
        with self.assertRaises(ls.CloseRefused): led.close_row(row["id"],"merged","abcdef1","attempt one merge only")
        self.assertGreaterEqual(calls.count(2), 1)  # auditing the second PR must never be skipped because the first matched

    def test_only_then_clauses_still_fit_and_have_a_note_reference(self):
        root=pathlib.Path(tempfile.mkdtemp()); led=ls.Ledger(root)
        row=led.write_row(None,next="short",waits="time; then owner: "+"x"*1200)
        self.assertLessEqual(ls.row_chars(row),ls.ROW_BUDGET)
        self.assertTrue("(detail:" in row["waits"])
        self.assertTrue((root/"ledger"/"notes"/"R-001.md").exists())

    def test_wait_without_a_named_kind_honors_an_explicit_delay(self):
        parsed=tm.parse_wait("WAIT +45m",T0)
        self.assertEqual(parsed["due"],T0+timedelta(minutes=45))


if __name__=="__main__":
    unittest.main()
