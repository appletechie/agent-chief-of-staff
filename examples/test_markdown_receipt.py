"""#28: what Markdown renders decides a review receipt and a hold, not its raw characters. First patch: karutoil (#29)."""
import unittest
from datetime import datetime, timedelta, timezone

import merge_gate as mg

T0 = datetime(2026, 1, 16, 14, 0, tzinfo=timezone.utc)
K = "b73a1636b1f7e591a7e3a731e2f9e521bb1bfbd6"


def decide(body="verdict: clean", title="fix", pr_body="", sha=K, now=T0, at=None, extra=()):
    reviews = [*extra, mg.Review(sha, body, at)]
    pr = mg.PR("org/app", 1, title, pr_body, K, False, True, {"ci": "success"}, reviews, 0, T0)
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

    def test_a_severity_marker_is_a_token_not_part_of_a_word(self):
        # Codex on #30: MP3, MP4 and HTTP3 are not P3/P4 findings; a bare P2 still is.
        self.assertEqual(decide("No major issues found. Validated MP3 and MP4 playback over HTTP3."), "MERGE")
        self.assertEqual(decide("verdict: clean\nP2 retry loop never ends"), "REPORT")
        self.assertEqual(decide("verdict: clean\n(p3) blank line"), "REPORT")

    def test_an_empty_findings_list_does_not_override_clean(self):
        for body in ("## Review\n\n## Findings\nNone.\n\nverdict: clean", "Findings: none\nverdict: clean"):
            with self.subTest(body=body):
                self.assertEqual(decide(body), "MERGE")
        self.assertEqual(decide("## Findings\nNone.\n- P1: the retry loop never ends\nverdict: clean"), "REPORT")

    def test_conditional_bot_boilerplate_is_not_a_finding(self):
        boiler = "No major issues found.\n\nIf the bot has suggestions, it will comment; otherwise it reacts with a thumbs up."
        self.assertEqual(decide(boiler), "MERGE")
        self.assertEqual(decide("verdict: clean\nIf this has issues, fix the bug in parse() first."), "REPORT")
        # Codex on #30: a conditional about the code, not the bot, is a finding.
        self.assertEqual(decide("No major issues found.\n\nIf the connection has issues, reconnect() never resets "
                                "the retry counter."), "REPORT")
        self.assertEqual(decide("No major issues found.\n\nIf Codex has suggestions, it will comment."), "MERGE")


class ShortHeadSha(unittest.TestCase):
    """A short receipt SHA binds only when it is 10+ hex, a prefix of the head, posted at or after the last push,
    and not also a prefix of a full SHA another receipt names. A 7-hex prefix (~28 bits) can be crafted."""
    LATER = T0 + timedelta(hours=2)
    AFTER_PUSH = T0 + timedelta(minutes=5)

    def test_a_full_sha_receipt_binds(self):
        self.assertEqual(decide(sha=K), "MERGE")
        self.assertEqual(decide(sha=K.upper()), "MERGE")

    def test_a_10_hex_abbreviation_posted_after_the_push_binds(self):
        for sha in (K[:10], K[:12].upper()):
            with self.subTest(sha=sha):
                self.assertEqual(decide(sha=sha, at=self.AFTER_PUSH), "MERGE")

    def test_a_crafted_shared_7_hex_prefix_is_refused(self):
        for sha in (K[:7], K[:9]):
            with self.subTest(sha=sha):
                self.assertEqual(decide(sha=sha, at=self.AFTER_PUSH, now=self.LATER), "REQUEST_REVIEW")

    def test_a_short_receipt_older_than_the_last_push_or_undated_is_refused(self):
        for at in (T0 - timedelta(minutes=1), None):
            with self.subTest(at=at):
                self.assertEqual(decide(sha=K[:10], at=at, now=self.LATER), "REQUEST_REVIEW")

    def test_a_short_receipt_never_stands_in_for_a_full_receipt_of_another_commit(self):
        other = K[:10] + "f" * 30
        findings = mg.Review(other, "verdict: findings", self.AFTER_PUSH)
        self.assertEqual(decide(sha=K[:10], at=self.AFTER_PUSH, now=self.LATER, extra=[findings]), "REQUEST_REVIEW")

    def test_a_full_head_receipt_outranks_a_short_one(self):
        full = mg.Review(K, "verdict: findings", self.AFTER_PUSH)
        self.assertEqual(decide(sha=K[:10], at=self.AFTER_PUSH, extra=[full]), "REPORT")

    def test_another_commit_or_a_non_hex_prefix_never_counts(self):
        for sha in ("0ld5ha0", "0ld5ha0b1c", "b73a163z", ""):
            with self.subTest(sha=sha):
                self.assertEqual(decide(sha=sha, at=self.AFTER_PUSH, now=self.LATER), "REQUEST_REVIEW")


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
