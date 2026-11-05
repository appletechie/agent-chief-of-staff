"""#25 B: a typed field that says red refuses the close, even beside a green word. First patch: karutoil (#26)."""
import pathlib
import tempfile
import unittest
from datetime import datetime, timezone

import ledger_store as ls

T0 = datetime(2026, 1, 16, 14, 0, tzinfo=timezone.utc)

# Every one of these closed at af2ea76 (karutoil's list in #25), plus the same shapes in other spellings.
RED = [
    ("ci", "run 123456 success exit 1"),
    ("ci", "run 123456 success=false"),
    ("ci", "run 123456 conclusion=no-success"),
    ("ci", "run 123456 conclusion=success; failure_count=1"),
    ("ci", "run 123456 success, status=neutral"),
    ("deployed", "deploy-12345 healthy exit 1"),
    ("deployed", "deploy-12345 healthy=false"),
    ("deployed", "deploy-12345 readyz:503"),
    ("deployed", "deploy-12345 healthz=500 healthy"),
    ("deployed", "deploy-12345 health_status=503 healthy"),
    ("tests", "run 123456 exit_code=1"),
    ("tests", "run 123456 exit-status: 2"),
    ("tests", "63 / 64"),
    ("tests", "0/0"),
    ("tests", "Ran 1 test OK (skipped=1)"),
    ("tests", "Ran 3 tests OK, 3 skipped"),
    ("tests", "12/12 passed; second retry 1/2 passed"),
    ("tests", "Ran 12 tests OK error_count=2"),
]
# Green evidence, including zero counts and explicit green fields, still closes.
GREEN = [
    ("tests", "Ran 162 tests OK (skipped=1)"),
    ("tests", "Ran 12 tests OK, 0 failures"),
    ("tests", "run 123456 exit_code=0, 12 passed"),
    ("tests", "12/12 passed; retry 2/2 passed"),
    ("tests", "Ran 12 tests OK failure_count=0"),
    ("tests", "64/64"),
    ("ci", "run 123456 success"),
    ("ci", "run 123456 conclusion=success failure_count=0"),
    ("ci", "run 123456 status=completed conclusion=success"),
    ("deployed", "deploy-12345 readyz 200"),
    ("deployed", "deploy-12345 readyz:200 healthy=true"),
    ("deployed", "deploy 41982839415 https://example.com:443/health 200"),
    ("deployed", "deploy-12345 https://status.example.com:443/health 200"),  # a port in a status host (Codex on #30)
    ("deployed", "deploy-12345 ready.example.com:8443/readyz 200"),
]


class TypedRedEvidence(unittest.TestCase):
    def test_typed_red_fields_refuse(self):
        for kind, ref in RED:
            with self.subTest(kind=kind, ref=ref):
                self.assertIsNotNone(ls.proof_problem(kind, ref, "on head abc1234"))

    def test_green_evidence_still_closes(self):
        for kind, ref in GREEN:
            with self.subTest(kind=kind, ref=ref):
                self.assertIsNone(ls.proof_problem(kind, ref, "on head abc1234"))

    def test_a_red_count_field_in_the_text_refuses(self):
        self.assertIsNotNone(ls.proof_problem("ci", "run 123456 success", "failure_count=1 on the head"))
        self.assertIsNone(ls.proof_problem("ci", "run 123456 success", "failure_count=0 on the head"))

    def test_other_kinds_do_not_read_typed_fields(self):
        self.assertIsNone(ls.proof_problem("config", "success=false read back", "flag off as asked"))

    def test_close_refuses_and_check_flags_a_typed_red_proof_in_the_archive(self):
        root = pathlib.Path(tempfile.mkdtemp())
        ledger = ls.Ledger(root)
        row = ledger.write_row(None, next="ship it")
        with self.assertRaises(ls.ProofRefused):
            ledger.close_row(row["id"], "ci", "run 123456 success=false", "on head abc1234", now=T0)
        self.assertEqual(ledger.closed_rows(), [])
        archive = root / "ledger" / "closed-2026-01.md"
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_text("| id | key | closed | proof | last next action |\n|---|---|---|---|---|\n"
                           "| R-009 | — | 2026-01-16 14:00Z | ci: run 123456 success=false - moved by hand | x |\n")
        self.assertTrue(any("R-009" in e and "success=false" in e for e in ledger.check()[0]))


if __name__ == "__main__":
    unittest.main()
