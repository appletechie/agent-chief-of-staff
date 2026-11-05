"""The close must consult review outcome too, not merely detect that a PR was merged (#19)."""
import pathlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import ledger_store as ls
import merge_gate as mg

T0 = datetime(2026, 1, 16, 14, 0, tzinfo=timezone.utc)
SHA = "cafebabe1234567890cafebabe1234567890"


class TestCloseReviewPolicy(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.pr = mg.PR(repo="org/app", number=42, title="fix", body="", head_sha=SHA, is_draft=False,
                        mergeable=True, checks={"ci": "success"}, reviews=[mg.Review(SHA, "verdict: findings")],
                        unresolved_threads=1, last_push_at=T0)

    def fresh(self, policy):
        ledger = ls.Ledger(self.root, can_close=policy)
        row = ledger.write_row(None, key="ISSUE-1", next="merge org/app#42 then close")
        return ledger, row

    def test_failing_review_blocks_the_close(self):
        ledger, row = self.fresh(mg.pr_review_policy(lambda repo, num: self.pr))
        with self.assertRaises(ls.CloseRefused):
            ledger.close_row(row["id"], "merged", SHA, "merged the PR")

    def test_clean_review_allows_a_close(self):
        self.pr.reviews = [mg.Review(SHA, "verdict: clean")]
        self.pr.unresolved_threads = 0
        ledger, row = self.fresh(mg.pr_review_policy(lambda repo, num: self.pr))
        ledger.close_row(row["id"], "merged", SHA, "an actual merge SHA")
        self.assertEqual(ledger.rows(), [])

    def test_a_broken_lookup_fails_closed(self):
        def bad(repo, n): raise OSError("down")
        ledger, row = self.fresh(mg.pr_review_policy(bad))
        with self.assertRaises(ls.CloseRefused):
            ledger.close_row(row["id"], "merged", SHA, "merged the PR")

    def test_composed_constraints_short_circuit(self):
        # order: PR merged condition first, then clean review; both need to succeed for closing to happen
        policy = ls.compose_close_policies(ls.pr_merge_policy(lambda r, n: SHA),
                                           mg.pr_review_policy(lambda r, n: self.pr))
        ledger, row = self.fresh(policy)
        with self.assertRaises(ls.CloseRefused):
            ledger.close_row(row["id"], "merged", SHA, "merged but with findings")
        self.pr.reviews = [mg.Review(SHA, "verdict: clean")]
        self.pr.unresolved_threads = 0
        ledger.close_row(row["id"], "merged", SHA, "now right")


if __name__ == "__main__":
    unittest.main()
