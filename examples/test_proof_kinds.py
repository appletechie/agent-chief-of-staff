"""Issue #8 section 1: close_row takes structured proof, so a claim ("verified", "ok") can never close a row."""
import pathlib
import tempfile
import unittest
from datetime import datetime, timezone

import ledger_store as ls

T0 = datetime(2026, 1, 16, 14, 0, tzinfo=timezone.utc)
SHA = "89f09d6c0ffee1234567890abcdef1234567890"


class ProofKindsBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.lookups = []

    def tearDown(self):
        self._tmp.cleanup()

    def ledger(self, **kw):
        led = ls.Ledger(self.root, **kw)
        led.write_row(None, key="ISSUE-1", next="Fix the retry loop")
        led.write_row(None, next="Merge acme/widgets#16, then reply on the issue")
        led.write_row(None, next="Build row; carries the follow-ups of folded R-001")
        return led

    def untouched(self, led, before):
        self.assertEqual(led.path.read_text(), before)
        self.assertEqual(list((self.root / "ledger").glob("closed-*.md")), [])
        self.assertFalse((self.root / "ledger" / "notes").exists())


class RefusedProof(ProofKindsBase):
    def test_the_issue_examples_no_longer_close(self):
        led = self.ledger()
        before = led.path.read_text()
        for ref in ("verified", "ok", "the worker said it works", "Done.", "LGTM", "works"):
            for kind in ("tests", "ci", "merged", "deployed"):
                with self.subTest(kind=kind, ref=ref), self.assertRaises(ls.ProofRefused):
                    led.close_row("R-001", kind, ref, "claimed by the worker")
        with self.assertRaises(ls.ProofRefused):
            led.close_row("R-001", "tests", "Ran 88 tests OK", "verified")
        self.untouched(led, before)

    def test_unknown_kind_and_empty_ref_or_text(self):
        led = self.ledger()
        before = led.path.read_text()
        for kind, ref, text in [("proof", SHA, "fix merged"), ("merged", "", "fix merged"),
                                ("merged", "   ", "fix merged"), ("merged", SHA, " ")]:
            with self.subTest(kind=kind, ref=ref, text=text), self.assertRaises(ls.ProofRefused):
                led.close_row("R-001", kind, ref, text)
        self.untouched(led, before)

    def test_prose_refs_are_refused_per_kind(self):
        prose = {
            "tests": "the suite passes locally",
            "ci": "run 4198283941 finished",
            "merged": "merged PR 16",
            "deployed": "deploy 4198283941 rolled out",
            "config": "changed the setting",
            "message": "replied on the issue",
            "owner": "the owner said to close it",
            "folded": "into the build row",
            "void": "not needed any more",
        }
        for kind, ref in prose.items():
            with self.subTest(kind=kind):
                self.assertIsNotNone(ls.proof_problem(kind, ref, "what it proves"))

    def test_ref_must_not_hold_the_separator(self):
        self.assertIsNotNone(ls.proof_problem("owner", '"close it - now" 14:02Z', "owner decided"))


class AcceptedProof(ProofKindsBase):
    def test_each_kind_accepts_its_evidence(self):
        for kind, ref in [("tests", "Ran 88 tests in 6.1s OK"), ("tests", "64/64 passing"), ("tests", "run 4198283941"),
                          ("ci", "run 4198283941 completed/success on 89f09d6"), ("merged", "89f09d6"),
                          ("deployed", "deploy dep-4417, readyz 200"), ("config", "autoDeploy=false read back"),
                          ("message", "issuecomment-3456789"), ("owner", '"ship it" 2026-01-16 14:02Z'),
                          ("folded", "R-003"), ("void", "created by a unit test")]:
            with self.subTest(kind=kind, ref=ref):
                self.assertIsNone(ls.proof_problem(kind, ref, "what it proves"))

    def test_archive_line_is_kind_ref_text_and_note_is_verbatim(self):
        led = self.ledger()
        led.close_row("R-001", "tests", "exit 0: `pytest | tail -1` -> 12 passed", "suite on the head commit", now=T0)
        archived = led.closed_rows()
        self.assertEqual([r["id"] for r in archived], ["R-001"])
        self.assertEqual(archived[0]["closed"], "2026-01-16 14:00Z")
        proof = ls.parse_proof(archived[0]["proof"])
        self.assertEqual((proof.kind, proof.text), ("tests", "suite on the head commit"))
        note = (self.root / "ledger" / "notes" / "R-001.md").read_text()
        self.assertIn("kind: tests\nref: exit 0: `pytest | tail -1` -> 12 passed\n", note)
        self.assertEqual(led.check(), ([], []))
        self.assertEqual(led.next_free_id(), "R-004")


class Folded(ProofKindsBase):
    def test_target_must_be_open_and_mention_the_row(self):
        led = self.ledger()
        before = led.path.read_text()
        for row_id, target in [("R-002", "R-003"), ("R-001", "R-077"), ("R-001", "R-001")]:
            with self.subTest(row=row_id, target=target), self.assertRaises(ls.CloseRefused):
                led.close_row(row_id, "folded", target, "same work")
        self.untouched(led, before)
        led.close_row("R-001", "folded", "R-003", "same work; R-003 carries the follow-ups")
        self.assertEqual(ls.parse_proof(led.closed_rows()[0]["proof"]).kind, "folded")


class MergePolicy(ProofKindsBase):
    def lookup(self, sha=SHA, error=None):
        def fake(repo, number):
            self.lookups.append((repo, number))
            if error:
                raise RuntimeError(error)
            return sha
        return fake

    def test_row_naming_a_pr_needs_its_merge_sha(self):
        led = self.ledger(can_close=ls.pr_merge_policy(self.lookup()))
        before = led.path.read_text()
        with self.assertRaises(ls.CloseRefused) as caught:
            led.close_row("R-002", "merged", "abcdef1", "merged")
        self.assertIn("acme/widgets#16 merged 89f09d6", str(caught.exception))
        self.untouched(led, before)
        led.close_row("R-002", "merged", "89f09d6", "squash merge of #16")
        self.assertEqual(self.lookups, [("acme/widgets", 16)] * 2)

    def test_unmerged_or_unreadable_pr_refuses(self):
        for lookup, words in [(self.lookup(sha=None), "not merged"), (self.lookup(error="HTTP 502"), "cannot read")]:
            led = self.ledger(can_close=ls.pr_merge_policy(lookup))
            with self.subTest(words=words), self.assertRaises(ls.CloseRefused) as caught:
                led.close_row("R-002", "merged", "89f09d6", "squash merge of #16")
            self.assertIn(words, str(caught.exception))
            self._tmp.cleanup()
            self._tmp = tempfile.TemporaryDirectory()
            self.root = pathlib.Path(self._tmp.name)

    def test_other_kinds_and_rows_without_a_pr_skip_the_lookup(self):
        led = self.ledger(can_close=ls.pr_merge_policy(self.lookup()))
        led.close_row("R-002", "message", "issuecomment-3456789", "replied on the issue")
        led.close_row("R-001", "merged", "abcdef1", "fix merged")
        self.assertEqual(self.lookups, [])

    def test_pr_refs(self):
        self.assertEqual(ls.pr_refs("acme/widgets#16, https://github.com/acme/api/pull/9, docs/notes#3x, acme/widgets#16"),
                         [("acme/widgets", 16), ("acme/api", 9)])


class CheckArchive(ProofKindsBase):
    def archive(self, *lines):
        path = self.root / "ledger" / "closed-2026-01.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("| id | key | closed | proof | last next action |\n|---|---|---|---|---|\n" + "".join(l + "\n" for l in lines))

    def test_free_text_archive_rows_fail_the_check(self):
        led = ls.Ledger(self.root)
        self.archive("| R-001 | — | 2026-01-16 14:00Z | verified | x |",
                     "| R-002 | — | 2026-01-16 14:00Z | shipped: 89f09d6 - merged | x |",
                     "| R-003 | — | 2026-01-16 14:00Z | merged: the PR - merged | x |",
                     "| R-004 | — | yesterday | merged: 89f09d6 - merged | x |",
                     "| R-005 | — | 2026-01-16 14:00Z | merged: 89f09d6 - merged | x |")
        errors, _ = led.check()
        self.assertEqual([e.split()[1].rstrip(":") for e in errors], ["R-001", "R-002", "R-003", "R-004"])

    def test_rows_closed_before_proof_since_are_grandfathered(self):
        self.archive("| R-001 | — | 2026-01-16 14:00Z | verified | x |",
                     "| R-002 | — | 2026-01-17 09:00Z | verified | x |")
        led = ls.Ledger(self.root, proof_since=datetime(2026, 1, 17, tzinfo=timezone.utc))
        errors, _ = led.check()
        self.assertEqual([e.split()[1].rstrip(":") for e in errors], ["R-002"])


if __name__ == "__main__":
    unittest.main()
