"""Coverage after #30: whole rendered meaning, full record shape, and the required-check deadline."""
import pathlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import ledger_store as ls
import merge_gate as mg

T0=datetime(2026,1,16,14,0,tzinfo=timezone.utc)
SHA="a"*40


class RenderedReceiptTests(unittest.TestCase):
    def test_visible_holds_match_in_uninterrupted_text(self):
        for body in ["Rollback: do [not](https://example.test) merge",
                     "Rollback: do <span>not</span> merge"]:
            pr=mg.PR("org/app",1,"t",body,SHA,False,True,{"ci":"success"},[mg.Review(SHA,"verdict: clean")],0,T0)
            self.assertEqual(mg.decide(pr,"",mg.Policy(frozenset()),T0).action,"REPORT")

    def test_a_wrapped_link_name_is_not_itself_a_hidden_hold(self):
        pr=mg.PR("org/app",1,"t","[Rollback runbook](https://example.test/WIP)",SHA,
                 False,True,{"ci":"success"},[mg.Review(SHA,"verdict: clean")],0,T0)
        self.assertEqual(mg.decide(pr,"",mg.Policy(frozenset()),T0).action,"MERGE")

    def test_formatted_severitiy_is_not_an_unknown_finding(self):
        pr=mg.PR("org/app",1,"t","",SHA,False,True,{"ci":"success"},
                 [mg.Review(SHA,"verdict: clean\n- P**3**: an unnecessary blank line")],0,T0)
        self.assertEqual(mg.decide(pr,"",mg.Policy(frozenset()),T0).action,"REPORT")

    def test_four_space_closer_cannot_let_an_example_verdict_approve(self):
        pr=mg.PR("org/app",1,"t","",SHA,False,True,{"ci":"success"},
                 [mg.Review(SHA,"Example:\n```text\n    ```\nverdict: clean\n```")],0,T0)
        self.assertNotEqual(mg.decide(pr,"",mg.Policy(frozenset()),T0).action,"MERGE")

    def test_zero_checks_can_trigger_a_late_required_report(self):
        pr=mg.PR("org/app",1,"t","",SHA,False,True,{},
                 [mg.Review(SHA,"verdict: clean")],0,T0)
        p=mg.Policy(frozenset(), required_checks={"org/app":frozenset({"ci"})})
        self.assertEqual(mg.decide(pr,"",p,T0+timedelta(days=30)).action,"REPORT")


class ArchiveCompletenessTests(unittest.TestCase):
    def test_a_row_lost_its_last_cell_is_not_a_whole_archive_record(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        root=pathlib.Path(tmp.name); path=root/"ledger"/"closed-2026-01.md";path.parent.mkdir()
        path.write_text('| R-001 | ISSUE-1 | 2026-01-16 14:00Z | merged: abcdef1 - merged once |\n')
        ledger=ls.Ledger(root)
        self.assertTrue(ledger.archive_problems())
        self.assertIn("4 cells", "\n".join(ledger.check()[0]))


if __name__=="__main__":
    unittest.main()
