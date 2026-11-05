"""Evidence and session-processing invariants found after #21: false success and replay safety."""
import json
import pathlib
import tempfile
import unittest
from datetime import datetime, timezone

import ledger_store as ls
import merge_gate as mg
import timers as tm
import wake_gate as wg
import lease as lz

T0 = datetime(2026, 1, 16, 14, 0, tzinfo=timezone.utc)


class TestOutputOutcomes(unittest.TestCase):
    def test_negative_evidence_is_not_proof(self):
        for kind, ref, text in [
            ("tests", "Ran 2 tests in 0.0s FAILED (failures=2)", "the suite has two failures"),
            ("tests", "run 123456: FAILED", "31 failures"),
            ("ci", "run 123456: no success; cancelled", "CI cancelled on the head"),
            ("ci", "abc1234 success and 1 failed", "CI is not finished"),
            ("deployed", "deploy-12345 status 500", "live route returned an error"),
        ]:
            self.assertIsNotNone(ls.proof_problem(kind, ref, text), (kind, ref, text))

    def test_documented_good_evidence_still_works(self):
        self.assertIsNone(ls.proof_problem("tests", "Ran 88 tests ... OK", "0 failures"))
        self.assertIsNone(ls.proof_problem("ci", "run 123456 success", "CI passed on the head"))
        self.assertIsNone(ls.proof_problem("deployed", "deploy-12345 status 200", "real flow returned OK"))


class ReviewOutcomes(unittest.TestCase):
    def test_p0_and_tilde_fenced_text_do_not_read_as_clean(self):
        for body in ["No major issues.\n- P0: data loss",
                     "Example only:\n~~~\nverdict: clean\n~~~",
                     "verdict: findings\n\nP0: drops messages\n\nverdict: clean"]:
            pr = mg.PR("o/a", 1, "t", "", "a"*40, False, True,
                       {"ci": "success"}, [mg.Review("a"*40, body)], 0, T0)
            self.assertNotEqual(mg.decide(pr, "", mg.Policy(frozenset()), T0).action, "MERGE")


class ArchiveReplay(unittest.TestCase):
    def test_retry_after_the_crash_does_not_append_a_second_proof(self):
        root = pathlib.Path(tempfile.mkdtemp())
        ledger = ls.Ledger(root)
        row = ledger.write_row(None, next="merge once")
        real_write = ls.atomic_write

        def fail_ledger(path, text):
            if pathlib.Path(path) == ledger.path:
                raise OSError("post-archive crash")
            real_write(path, text)

        ls.atomic_write = fail_ledger
        try:
            ledger.close_row(row["id"], "merged", "abcdef1", "merged on the head", now=T0)
        except OSError:
            pass
        ls.atomic_write = real_write
        ledger.close_row(row["id"], "merged", "abcdef1", "merged on the head", now=T0)
        self.assertEqual(len(ledger.closed_ids()), 1)
        self.assertEqual(ledger.check()[0], [])

    def test_manual_duplicate_archive_is_detected_even_when_open_row_is_gone(self):
        root = pathlib.Path(tempfile.mkdtemp()); ledger = ls.Ledger(root)
        line = '| R-001 | ISSUE-1 | 2026-01-16 14:00Z | merged: abcdef1 - merged again | work |\n'
        (root/"ledger").mkdir()
        (root/"ledger"/"closed-2026-01.md").write_text(line*2)
        self.assertIn("repeats id R-001", "\n".join(ledger.check()[0]))


class HostNameMatching(unittest.TestCase):
    def test_host_case_does_not_opt_out_of_the_vcs_lookup(self):
        root = pathlib.Path(tempfile.mkdtemp())
        row = ls.Ledger(root).write_row(None, next="merge https://GITHUB.COM/org/app/pull/42 then close")
        self.assertEqual(ls.pr_refs(row["next"]), [("org/app", 42)])


class InputValidation(unittest.TestCase):
    def test_add_does_not_create_self_corrupt_records(self):
        store = tm.Timers(pathlib.Path(tempfile.mkdtemp())/"timers.json")
        for field in ["row", "name", "target"]:
            kwargs = {"row": "R-001", "name": "n", "prompt": "p", "target": "coordinator"}
            kwargs[field] = ""
            with self.assertRaises(ValueError):
                store.add(**kwargs, due="+1m", now=T0)
            self.assertFalse(store.path.exists())

    def test_falsey_first_attempt_is_not_a_later_parse_typeerror(self):
        root = pathlib.Path(tempfile.mkdtemp()); path = root/"timers.json"
        record = {"id":"t-1", "row":"R-001", "state":"armed", "due":T0.isoformat(), "target":"x",
                  "name":"name", "prompt":"prompt", "attempts":0, "first_attempt_at":None}
        path.write_text(json.dumps([record]))
        log = tm.Timers(path).tick(lambda t, m: (_ for _ in ()).throw(OSError("down")), {"R-001"}, T0)
        self.assertIsInstance(log,list)


class DuplicateRecordTests(unittest.TestCase):
    def test_two_records_with_the_same_id_cannot_both_deliver(self):
        path =pathlib.Path(tempfile.mkdtemp())/"timers.json"
        one = dict(id="t-same", row="R-1", state="armed", due=T0.isoformat(),
                   target="coordinator", name="check", prompt="check", attempts=0)
        path.write_text(json.dumps([one, dict(one, prompt="again the same id")]))
        log = tm.Timers(path).tick(lambda t, m: self.fail("duplicates must not cause sends"), {"R-1"}, T0)
        self.assertTrue(any("duplicate" in line for line in log))

    def test_extremely_large_relative_due_gets_a_fallback(self):
        for due in ["+999999999999999m", "+999999999999999h"]:
            _, note = tm.parse_due(due, T0)
            self.assertIsNotNone(note)


if __name__ == "__main__":
    unittest.main()

class MoreDefects(unittest.TestCase):
    def test_non_utf8_inputs_get_the_contract_response(self):
        for name, fn in [("wake", lambda p: wg.run({}, p, persist=False, now=T0).changed),
                         ("lease", lambda p: lz.Lease(p).epoch()),
                         ("timer", lambda p: tm.Timers(p).tick(lambda t,m: "ok", set(), T0))]:
            path = pathlib.Path(tempfile.mkdtemp())/"state.json"
            path.write_bytes(b"\xff\xfe")
            if name == "wake": self.assertTrue(fn(path))
            if name == "lease":
                with self.assertRaises(lz.LeaseError): fn(path)
            if name == "timer": self.assertTrue(any("corrupt" in s for s in fn(path)))

    def test_invalid_pr_before_a_valid_one_cannot_hide_it(self):
        import dispatch_guard as dg
        pr={"repo":"o/r", "number":7, "author":"other"}
        source=dg.Sources(open_prs_naming=lambda k:[{},pr], assignee=lambda k:None, live_workers=lambda:[])
        out=dg.guard("K", "R-001", "mine", source,
                     dg.Claims(pathlib.Path(tempfile.mkdtemp())/"claims.json"), now=T0)
        self.assertFalse(out.allowed)
        self.assertTrue(any("o/r#7" in reason for reason in out.refusals))

    def test_directory_as_a_state_path_does_not_crash_the_wake(self):
        path=pathlib.Path(tempfile.mkdtemp())
        self.assertTrue(wg.run({}, path, persist=False, now=T0).changed)
