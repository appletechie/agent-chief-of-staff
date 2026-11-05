"""The verdict token can coexist with findings and examples, so neither is mistaken for a clean review."""
import tempfile
import unittest
from datetime import datetime, timezone

import merge_gate as mg

T0 = datetime(2026, 1, 16, 14, 0, tzinfo=timezone.utc)


class ReviewVerdictTests(unittest.TestCase):
    def should(self, body):
        policy = mg.Policy(deploys_on_merge=frozenset())
        pr = mg.PR(repo="o/a", number=1, title="t", body="", head_sha="a" * 40, is_draft=False, mergeable=True,
                   checks={"ci": "success"}, reviews=[mg.Review("a" * 40, body)],
                   unresolved_threads=0, last_push_at=T0)
        return mg.review_verdict(pr, policy)

    def test_explicit_clean_verdict(self):
        self.assertEqual(self.should("verdict: clean"), "clean")

    def test_findings_under_a_clean_token(self):
        body = "## Review\nverdict: clean\n\n## Findings\n- P1: the retry loop drops messages"
        self.assertEqual(self.should(body), "findings")

    def test_example_of_the_convention_is_ignored(self):
        body = "End every review with a line:\n\n```\nverdict: clean\n```\n\nor `verdict: findings`."
        self.assertNotEqual(self.should(body), "clean")

    def test_last_verdict_out_of_a_fence_wins(self):
        body = "```\nverdict: clean\n```\nverdict: findings"
        self.assertEqual(self.should(body), "findings")

    def test_a_finder_line_wins_over_the_last_token(self):
        body = "verdict: findings\n\nFindings\n- a bug\nverdict: clean"
        self.assertEqual(self.should(body), "findings")

    def test_no_receipt_stays_unknown(self):
        self.assertIsNone(self.should_no_receipts())

    def should_no_receipts(self):
        policy = mg.Policy(deploys_on_merge=frozenset())
        pr = mg.PR(repo="o/a", number=1, title="t", body="", head_sha="a" * 40, is_draft=False, mergeable=True,
                   checks={"ci": "success"}, reviews=[mg.Review("b" * 40, "verdict: clean")],
                   unresolved_threads=0, last_push_at=T0)
        return mg.review_verdict(pr, policy)


if __name__ == "__main__":
    unittest.main()
