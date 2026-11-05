"""The close consults the review on the PR's head, not only the merge (#19, after karutoil's #20)."""
import pathlib
import tempfile
import unittest
from datetime import datetime, timezone

import ledger_store as ls
import merge_gate as mg

T0 = datetime(2026, 1, 16, 14, 0, tzinfo=timezone.utc)
SHA = "cafebabe1234567890cafebabe1234567890"


class CloseReviewPolicy(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.pr = mg.PR(repo="org/app", number=42, title="fix", body="", head_sha=SHA, is_draft=False,
                        mergeable=True, checks={"ci": "success"}, reviews=[mg.Review(SHA, "verdict: findings")],
                        unresolved_threads=1, last_push_at=T0)

    def tearDown(self):
        self._tmp.cleanup()

    def clean(self):
        self.pr.reviews, self.pr.unresolved_threads = [mg.Review(SHA, "verdict: clean")], 0

    def fresh(self, policy):
        ledger = ls.Ledger(self.root, can_close=policy)
        return ledger, ledger.write_row(None, key="ISSUE-1", next="merge org/app#42 then close")

    def test_findings_on_the_head_refuse_the_close(self):
        ledger, row = self.fresh(mg.pr_review_policy(lambda repo, number: self.pr))
        with self.assertRaises(ls.CloseRefused) as error:
            ledger.close_row(row["id"], "merged", SHA, "merged the PR")
        self.assertIn("review on head has findings", str(error.exception))
        self.assertEqual(len(ledger.rows()), 1)

    def test_unresolved_threads_or_no_receipt_on_the_head_refuse(self):
        for reviews, threads in (([mg.Review(SHA, "verdict: clean")], 2), ([mg.Review("0ldhead", "verdict: clean")], 0)):
            self.pr.reviews, self.pr.unresolved_threads = reviews, threads
            ledger, row = self.fresh(mg.pr_review_policy(lambda repo, number: self.pr))
            with self.assertRaises(ls.CloseRefused):
                ledger.close_row(row["id"], "merged", SHA, "merged the PR")

    def test_a_clean_review_allows_the_close(self):
        self.clean()
        ledger, row = self.fresh(mg.pr_review_policy(lambda repo, number: self.pr))
        ledger.close_row(row["id"], "merged", SHA, "merged, review clean")
        self.assertEqual(ledger.rows(), [])

    def test_merge_time_stops_do_not_block_a_close_after_the_merge(self):
        # A merged PR is no longer mergeable and may still carry the hold marker the owner merged past.
        self.clean()
        self.pr.mergeable, self.pr.body = None, "Draft: merge only after the owner signs off"
        ledger, row = self.fresh(mg.pr_review_policy(lambda repo, number: self.pr))
        ledger.close_row(row["id"], "merged", SHA, "owner merged it")
        self.assertEqual(ledger.rows(), [])

    def test_a_missing_pr_or_a_broken_lookup_refuses(self):
        def down(repo, number):
            raise OSError("api down")
        for lookup in (down, lambda repo, number: None):
            ledger, row = self.fresh(mg.pr_review_policy(lookup))
            with self.assertRaises(ls.CloseRefused):
                ledger.close_row(row["id"], "merged", SHA, "merged the PR")

    def test_other_proof_kinds_are_not_review_gated(self):
        ledger, row = self.fresh(mg.pr_review_policy(lambda repo, number: self.pr))
        other = ledger.write_row(None, key="ISSUE-2", next=f"carry on {row['id']}'s open items")
        ledger.close_row(row["id"], "folded", other["id"], f"folded into {other['id']}")
        self.assertEqual([r["id"] for r in ledger.rows()], [other["id"]])

    def test_composed_merge_and_review_policies(self):
        merged = {"sha": None}
        policy = ls.compose_close_policies(ls.pr_merge_policy(lambda repo, number: merged["sha"]),
                                           mg.pr_review_policy(lambda repo, number: self.pr))
        ledger, row = self.fresh(policy)
        with self.assertRaises(ls.CloseRefused) as error:
            ledger.close_row(row["id"], "merged", SHA, "not merged yet")
        self.assertIn("not merged", str(error.exception))
        merged["sha"] = SHA
        with self.assertRaises(ls.CloseRefused) as error:
            ledger.close_row(row["id"], "merged", SHA, "merged with findings")
        self.assertIn("review on head", str(error.exception))
        self.clean()
        ledger.close_row(row["id"], "merged", SHA, "merged, review clean")
        self.assertEqual(ledger.rows(), [])

    def test_decide_still_reports_the_same_review_reasons(self):
        policy = mg.Policy(frozenset())
        self.assertEqual(mg.decide(self.pr, "", policy, now=T0).reason, "review on head has findings")
        self.pr.reviews = [mg.Review(SHA, "verdict: clean")]
        self.assertEqual(mg.decide(self.pr, "", policy, now=T0).reason, "1 unresolved review threads")
        self.pr.unresolved_threads = 0
        self.assertEqual(mg.decide(self.pr, "", policy, now=T0).action, "MERGE")


if __name__ == "__main__":
    unittest.main()
