import unittest
from datetime import datetime, timezone
import merge_gate as mg
T0 = datetime(2026, 1, 16, 14, 0, tzinfo=timezone.utc)
K = "b73a1636b1f7e591a7e3a731e2f9e521bb1bfbd6"


class ReviewSemanticTests(unittest.TestCase):
    def test_a_markdown_hold_is_the_same_hold(self):
        for title,body in [("Draft\\: rollback owner approval pending",""),("Rollback progress","do **not** merge yet")]:
            pr=mg.PR("org/app",1,title,body,K,False,True,{"ci":"success"},[mg.Review(K,"verdict: clean")],0,T0)
            self.assertEqual(mg.decide(pr,"",mg.Policy(frozenset()),T0).action,"REPORT",title)

    def test_explicit_p3_finding_still_refuses_merge(self):
        pr=mg.PR("org/app",1,"fix","",K,False,True,{"ci":"success"},
                 [mg.Review(K,"- P3: remove a blank line\nverdict: clean")],0,T0)
        self.assertEqual(mg.decide(pr,"",mg.Policy(frozenset()),T0).action,"REPORT")

    def test_actual_empty_findings_do_not_override_clean(self):
        pr=mg.PR("org/app",1,"fix","",K,False,True,{"ci":"success"},
                 [mg.Review(K,"## Review\n\n## Findings\nNone.\n\nverdict: clean")],0,T0)
        self.assertEqual(mg.decide(pr,"",mg.Policy(frozenset()),T0).action,"MERGE")

    def test_markdown_example_cannot_be_an_actual_verdict(self):
        for body in ["Example only:\n\n    verdict: clean", "Example only:\n```text\n```not-a-close\nverdict: clean\n```"]:
            pr=mg.PR("org/app",1,"fix","",K,False,True,{"ci":"success"},[mg.Review(K,body)],0,T0)
            self.assertNotEqual(mg.decide(pr,"",mg.Policy(frozenset()),T0).action,"MERGE",body)

    def test_actual_commit_abbreviation_does_not_force_a_redundant_review(self):
        pr=mg.PR("org/app",1,"fix","",K,False,True,{"ci":"success"},
                 [mg.Review("b73a1636b1","verdict: clean")],0,T0)
        self.assertEqual(mg.decide(pr,"",mg.Policy(frozenset()),T0).action,"MERGE")

    def test_a_genuinely_other_commit_never_counts(self):
        pr=mg.PR("org/app",1,"fix","",K,False,True,{"ci":"success"},
                 [mg.Review("0ld5ha0","verdict: clean")],0,T0)
        self.assertNotEqual(mg.decide(pr,"",mg.Policy(frozenset()), T0+__import__('datetime').timedelta(hours=2)).action,"MERGE")


if __name__=="__main__":
    unittest.main()
