"""#28: what Markdown renders decides a review receipt and a hold, not its raw characters. First patch: karutoil (#29)."""
import unittest
from datetime import datetime, timedelta, timezone

import merge_gate as mg

T0 = datetime(2026, 1, 16, 14, 0, tzinfo=timezone.utc)
K = "b73a1636b1f7e591a7e3a731e2f9e521bb1bfbd6"


def decide(body="verdict: clean", title="fix", pr_body="", sha=K, now=T0):
    pr = mg.PR("org/app", 1, title, pr_body, K, False, True, {"ci": "success"}, [mg.Review(sha, body)], 0, T0)
    return mg.decide(pr, "", mg.Policy(frozenset()), now).action


class ReceiptMarkdown(unittest.TestCase):
    def test_an_indented_or_unclosed_example_is_not_the_verdict(self):
        for body in ("Example only:\n\n    verdict: clean", "Example only:\n\n\tverdict: clean",
                     "Example only:\n```text\n```not-a-close\nverdict: clean\n```",
                     "Example:\n\n    No major issues found."):
            with self.subTest(body=body):
                self.assertNotEqual(decide(body), "MERGE")

    def test_a_bare_closing_fence_still_closes(self):
        self.assertEqual(decide("```text\nan example receipt\n```  \nverdict: clean"), "MERGE")
        self.assertEqual(decide("~~~\nexample\n~~~~\nNo major issues found."), "MERGE")

    def test_p3_and_p4_findings_refuse_beside_a_clean_token(self):
        for body in ("- P3: remove the extra blank line\nverdict: clean", "P4 nit: rename x\nverdict: clean"):
            with self.subTest(body=body):
                self.assertEqual(decide(body), "REPORT")

    def test_an_empty_findings_list_does_not_override_clean(self):
        for body in ("## Review\n\n## Findings\nNone.\n\nverdict: clean", "Findings: none\nverdict: clean"):
            with self.subTest(body=body):
                self.assertEqual(decide(body), "MERGE")
        self.assertEqual(decide("## Findings\nNone.\n- P1: the retry loop never ends\nverdict: clean"), "REPORT")

    def test_conditional_bot_boilerplate_is_not_a_finding(self):
        boiler = "No major issues found.\n\nIf the bot has suggestions, it will comment; otherwise it reacts with a thumbs up."
        self.assertEqual(decide(boiler), "MERGE")
        self.assertEqual(decide("verdict: clean\nIf this has issues, fix the bug in parse() first."), "REPORT")


class ShortHeadSha(unittest.TestCase):
    def test_an_abbreviation_of_the_head_counts(self):
        for sha in (K[:7], K[:10], K[:10].upper()):
            with self.subTest(sha=sha):
                self.assertEqual(decide(sha=sha), "MERGE")

    def test_another_commit_or_a_too_short_prefix_never_counts(self):
        later = T0 + timedelta(hours=2)
        for sha in ("0ld5ha0", K[:6], "b73a163z", ""):
            with self.subTest(sha=sha):
                self.assertEqual(decide(sha=sha, now=later), "REQUEST_REVIEW")


class FormattedHolds(unittest.TestCase):
    def test_a_markdown_hold_is_the_same_hold(self):
        for title, body in (("Draft\\: rollback owner approval pending", ""), ("Rollback", "do **not** merge yet"),
                            ("Rollback", "do _not_ merge"), ("`WIP` rollback", ""), ("Rollback", "merge only *after* sign-off")):
            with self.subTest(title=title, body=body):
                self.assertEqual(decide(title=title, pr_body=body), "REPORT")

    def test_formatting_alone_is_not_a_hold(self):
        self.assertEqual(decide(title="Fix the **draft** parser", pr_body="renames snake_case_name"), "MERGE")


if __name__ == "__main__":
    unittest.main()
