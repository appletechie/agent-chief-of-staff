"""#22: evidence must be good, not just well formed; a retried close archives once; edge sequences fail closed.

Most cases are karutoil's, from #22 and #23, reproduced on 4923709 before the fix.
"""
import json
import pathlib
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

import dispatch_guard as dg
import lease as lz
import ledger_store as ls
import merge_gate as mg
import timers as tm
import wake_gate as wg

T0 = datetime(2026, 1, 16, 14, 0, tzinfo=timezone.utc)


class Temp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def sub(self, name: str) -> pathlib.Path:
        path = self.root / name
        path.mkdir()
        return path


class EvidenceOutcome(unittest.TestCase):
    def test_failed_cancelled_and_red_evidence_is_refused(self):
        bad = [
            ("tests", "Ran 2 tests in 0.0s FAILED (failures=2)", "the suite has two failures"),
            ("tests", "run 123456: FAILED", "31 failures"),
            ("tests", "12 passed, 1 failed", "suite on head"),
            ("tests", "12 passed, 2 errors", "suite on head"),
            ("tests", "exit 1: 12 passed", "suite on head"),
            ("tests", "63/64 passing", "suite completed"),
            ("tests", "1 skipped", "all tests skipped"),
            ("tests", "Ran 0 tests OK", "nothing collected"),
            ("tests", "Ran 12 tests OK", "the run reported 2 failed"),
            ("ci", "run 123456: no success; cancelled", "CI cancelled on the head"),
            ("ci", "run 123456: no success", "head run completed"),
            ("ci", "abc1234 success and 1 failed", "CI is not finished"),
            ("ci", "run 123456: success; 1 failure", "CI report"),
            ("ci", "run 123456: success; canceled", "CI report"),
            ("ci", "run 123456: success; integration failure", "CI report on head"),
            ("ci", "run 123456 timed out after success on lint", "CI report"),
            ("ci", "run 123456 conclusion=startup_failure, success on abc1234", "CI report"),
            ("ci", "run 123456 success, required check e2e skipped", "CI report"),
            ("ci", "run 123456: conclusion=success2", "run failed before success"),
            ("ci", "run 123456 conclusion=timed_out, success on lint", "CI report"),
            ("tests", "pytest tests/test_x.py: 1 failed", "suite on head"),
            ("deployed", "deploy-12345 status 500", "live route returned an error"),
            ("deployed", "deploy-12345 status 503", "GET /readyz returned HTTP 503"),
            ("deployed", "deploy-12345 not healthy", "flow observed unavailable"),
        ]
        for kind, ref, text in bad:
            with self.subTest(kind=kind, ref=ref, text=text):
                self.assertIsNotNone(ls.proof_problem(kind, ref, text))

    def test_good_evidence_with_zero_counts_still_closes(self):
        good = [
            ("tests", "Ran 88 tests ... OK", "0 failures"),
            ("tests", "Ran 139 tests OK (skipped=1)", "suite on head"),
            ("tests", "Ran 5 tests: 5 passed, 0 failed", "five passed"),
            ("tests", "exit 0, 64/64 passed", "suite on head"),
            ("ci", "run 123456 success", "CI passed on the head"),
            ("ci", "run 123456 SUCCESS", "no errors; no failures"),
            ("ci", "run 123456 success on abc1234", "fixes the cancelled-timer bug"),
            ("deployed", "deploy-12345 status 200", "health and live flow passed"),
            ("deployed", "deploy-12345 status 301", "redirect to the new host"),
            ("tests", "pytest tests/test_errors.py: 12 passed", "suite on head"),
            ("tests", "go test ./errors/handler_test.go: 12 passed", "covers tests/test_cancelled.py"),
            ("tests", "test_failures 12 passed", "suite on head"),
        ]
        for kind, ref, text in good:
            with self.subTest(kind=kind, ref=ref):
                self.assertIsNone(ls.proof_problem(kind, ref, text))

    def test_other_kinds_are_not_read_for_red_words(self):
        self.assertIsNone(ls.proof_problem("message", "comment 12860", "replied about the failed run"))

    def test_close_refuses_and_check_flags_a_red_proof_in_the_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            ledger = ls.Ledger(root)
            row = ledger.write_row(None, next="ship it")
            with self.assertRaises(ls.ProofRefused):
                ledger.close_row(row["id"], "ci", "run 123456: no success; cancelled", "CI cancelled", now=T0)
            self.assertEqual(ledger.closed_rows(), [])
            (root / "ledger").mkdir()
            (root / "ledger" / "closed-2026-01.md").write_text(
                "| R-009 | — | 2026-01-16 14:00Z | ci: run 123456: no success; cancelled - CI cancelled | x |\n")
            self.assertTrue(any("R-009" in e and "no success" in e for e in ledger.check()[0]))


class ArchiveReplay(Temp):
    """A close that crashed after the archive append is retried as docs/ledger.md says: it must archive once."""

    def crash_after_archive(self, ledger):
        real = ls.atomic_write

        def write(path, text):
            if pathlib.Path(path) == ledger.path:
                raise OSError("crash after the archive append")
            real(path, text)
        return mock.patch.object(ls, "atomic_write", write)

    def test_retry_after_the_crash_does_not_append_a_second_proof(self):
        for ref in ("abcdef1", "run 123456 | tail -1"):
            with self.subTest(ref=ref):
                ledger = ls.Ledger(self.sub(f"r{len(ref)}"))
                row = ledger.write_row(None, next="merge once")
                kind = "merged" if ref == "abcdef1" else "tests"
                with self.crash_after_archive(ledger), self.assertRaises(OSError):
                    ledger.close_row(row["id"], kind, ref, "proven on abc1234", now=T0)
                self.assertEqual(ledger.check()[0], [f"line 7 {row['id']}: id also appears in a closed archive"])
                ledger.close_row(row["id"], kind, ref, "proven on abc1234", now=datetime(2026, 2, 1, tzinfo=timezone.utc))
                self.assertEqual(len(ledger.closed_rows()), 1)
                self.assertEqual(ledger.rows(), [])
                self.assertEqual(ledger.check(), ([], []))
                self.assertFalse((ledger.root / "ledger" / "closed-2026-02.md").exists())
                self.assertEqual((ledger.notes / f"{row['id']}.md").read_text().count("## proof"), 1)

    def test_note_written_before_a_crash_is_not_repeated(self):
        ledger = ls.Ledger(self.root)
        row = ledger.write_row(None, next="no duplicate note")
        ledger.notes.mkdir(parents=True)
        (ledger.notes / f"{row['id']}.md").write_text("\n## proof\nkind: tests\nref: run 123456\n12 passed\n")
        ledger.close_row(row["id"], "tests", "run 123456", "12 passed", now=T0)
        self.assertEqual((ledger.notes / f"{row['id']}.md").read_text().count("## proof"), 1)
        self.assertEqual(len(ledger.closed_rows()), 1)

    def test_the_note_keeps_the_proof_verbatim(self):
        ledger = ls.Ledger(self.root)
        row = ledger.write_row(None, next="set the banner")
        ref, text = 'banner="hello  world" read back', "output:\n  line one\n  line two"
        ledger.close_row(row["id"], "config", ref, text, now=T0)
        self.assertIn(f"ref: {ref}\n{text}\n", (ledger.notes / f"{row['id']}.md").read_text())

    def test_an_archived_id_with_other_proof_is_refused(self):
        ledger = ls.Ledger(self.root)
        row = ledger.write_row(None, next="close once")
        with self.crash_after_archive(ledger), self.assertRaises(OSError):
            ledger.close_row(row["id"], "tests", "Ran 5 tests OK", "suite on abc1234", now=T0)
        with self.assertRaises(ls.CloseRefused):
            ledger.close_row(row["id"], "merged", "abcdef1", "merged", now=T0)
        self.assertEqual(len(ledger.closed_rows()), 1)
        self.assertEqual([r["id"] for r in ledger.rows()], [row["id"]])

    def test_a_duplicate_archive_fails_check_after_the_open_row_is_gone(self):
        ledger = ls.Ledger(self.root)
        line = "| R-001 | ISSUE-1 | 2026-01-16 14:00Z | merged: abcdef1 - merged again | work |\n"
        (self.root / "ledger").mkdir()
        (self.root / "ledger" / "closed-2026-01.md").write_text(line * 2)
        self.assertIn("repeats id R-001 2 times", "\n".join(ledger.check()[0]))

    def test_duplicates_older_than_proof_since_only_warn(self):
        ledger = ls.Ledger(self.root, proof_since=datetime(2026, 1, 10, tzinfo=timezone.utc))
        (self.root / "ledger").mkdir()
        old = "| R-001 | — | 2026-01-02 09:00Z | done | work |\n"
        new = "| R-002 | — | 2026-01-16 14:00Z | merged: abcdef1 - merged | work |\n"
        (self.root / "ledger" / "closed-2026-01.md").write_text(old * 2 + new * 2)
        errors, warnings = ledger.check()
        self.assertEqual([e.split()[4] for e in errors], ["R-002"])
        self.assertEqual([w.split()[4] for w in warnings], ["R-001"])


class HostNameMatching(Temp):
    def test_host_owner_and_repo_case_do_not_opt_out_of_the_lookup(self):
        self.assertEqual(ls.pr_refs("merge https://GITHUB.COM/Org/App/pull/42, org/app#42 and ORG/APP#42"),
                         [("org/app", 42)])
        seen = []

        def lookup(repo, number):
            seen.append((repo, number))
            return "89f09d6c0ffee1234567890abcdef1234567890"
        ledger = ls.Ledger(self.root, can_close=ls.pr_merge_policy(lookup))
        row = ledger.write_row(None, next="Merge https://GITHUB.COM/org/app/pull/42 then close")
        with self.assertRaises(ls.CloseRefused):
            ledger.close_row(row["id"], "merged", "deadbeef", "merged", now=T0)
        self.assertEqual(seen, [("org/app", 42)])
        ledger.close_row(row["id"], "merged", "89f09d6", "merged", now=T0)


class ReviewOutcomes(unittest.TestCase):
    def decide(self, body):
        pr = mg.PR("o/a", 1, "t", "", "a" * 40, False, True, {"ci": "success"}, [mg.Review("a" * 40, body)], 0, T0)
        return mg.decide(pr, "", mg.Policy(frozenset()), T0).action

    def test_p0_and_fenced_text_do_not_read_as_clean(self):
        for body in ["No major issues.\n- P0: data loss",
                     "Example only:\n~~~\nverdict: clean\n~~~",
                     "Example only:\n```\nlooks good to merge\n```",
                     "~~~\n```\nverdict: clean",
                     "verdict: findings\n\nP0: drops messages\n\nverdict: clean"]:
            with self.subTest(body=body):
                self.assertNotEqual(self.decide(body), "MERGE")

    def test_a_clean_verdict_outside_fences_still_merges(self):
        self.assertEqual(self.decide("verdict: clean\nNo issues found."), "MERGE")
        self.assertEqual(self.decide("~~~\nexample\n~~~\nverdict: clean"), "MERGE")


class DispatchRecords(Temp):
    def guard(self, sources, key="K", row="R-001"):
        return dg.guard(key, row, "mine", sources, dg.Claims(self.root / "claims.json"), now=T0)

    def test_an_invalid_pr_before_or_after_a_valid_one_cannot_hide_it(self):
        pr = {"repo": "o/r", "number": 7, "author": "other"}
        for records in ([{}, pr], [pr, {}], [None, pr]):
            with self.subTest(records=records):
                out = self.guard(dg.Sources(open_prs_naming=lambda k, r=records: r, assignee=lambda k: None,
                                            live_workers=lambda: []))
                self.assertFalse(out.allowed)
                self.assertTrue(any("o/r#7" in reason for reason in out.refusals))
                self.assertTrue(any("malformed" in reason for reason in out.refusals))

    def test_a_dead_pr_search_refuses(self):
        def down(key):
            raise OSError("HTTP 502")
        out = self.guard(dg.Sources(open_prs_naming=down, assignee=lambda k: None, live_workers=lambda: []))
        self.assertFalse(out.allowed)
        self.assertIn("PR search failed (OSError); duplicate PRs not ruled out", out.refusals)

    def test_a_naive_history_record_cannot_hide_a_valid_recent_dispatch(self):
        entries = [{"key": "ISSUE-1", "session": "other-old", "at": "2026-01-16T13:00:00"},
                   {"key": "ISSUE-1", "session": "other-live", "at": "2026-01-16T13:55:00+00:00"}]
        out = self.guard(dg.Sources(open_prs_naming=lambda key: (), assignee=lambda key: None,
                                    live_workers=lambda: (), recent_dispatches=lambda key: entries), key="ISSUE-1")
        self.assertFalse(out.allowed)
        self.assertTrue(any("other-live" in s for s in out.refusals))
        self.assertTrue(any("no time zone" in w for w in out.warnings))

    def test_an_invalid_worker_record_is_a_safe_stop(self):
        for workers in ([None], [None, {"row": "R-001"}]):
            with self.subTest(workers=workers):
                out = self.guard(dg.Sources(open_prs_naming=lambda k: (), assignee=lambda k: None,
                                            live_workers=lambda w=workers: w), key=None)
                self.assertFalse(out.allowed)


class StateFiles(Temp):
    def test_non_utf8_or_directory_state_gets_the_contract_response(self):
        bad = self.root / "state.json"
        bad.write_bytes(b"\xff\xfe")
        directory = self.sub("state-dir")
        for path in (bad, directory):
            with self.subTest(path=path.name):
                self.assertTrue(wg.run({}, path, persist=False, now=T0).changed)
                with self.assertRaises(lz.LeaseError):
                    lz.Lease(path).epoch()
                self.assertTrue(any("corrupt" in s for s in tm.Timers(path).tick(lambda t, m: "ok", set(), T0)))
                out = dg.guard("K", "R", "s", dg.Sources(open_prs_naming=lambda k: (), assignee=lambda k: None,
                                                         live_workers=lambda: ()), dg.Claims(path), now=T0)
                self.assertFalse(out.allowed)


class TimerEdges(Temp):
    def test_add_does_not_create_self_corrupt_records(self):
        store = tm.Timers(self.root / "timers.json")
        for field in ["row", "name", "target", "prompt"]:
            kwargs = {"row": "R-001", "name": "n", "prompt": "p", "target": "coordinator"}
            kwargs[field] = ""
            with self.subTest(field=field), self.assertRaises(ValueError):
                store.add(**kwargs, due="+1m", now=T0)
            self.assertFalse(store.path.exists())

    def test_falsey_first_attempt_is_invalid_not_a_later_type_error(self):
        path = self.root / "timers.json"
        record = {"id": "t-1", "row": "R-001", "state": "armed", "due": T0.isoformat(), "target": "x",
                  "name": "name", "prompt": "prompt", "attempts": 0, "first_attempt_at": None}
        path.write_text(json.dumps([record]))
        log = tm.Timers(path).tick(lambda t, m: (_ for _ in ()).throw(OSError("down")), {"R-001"}, T0)
        self.assertTrue(any("invalid" in line for line in log))
        self.assertEqual(json.loads(path.read_text())[0]["state"], "armed")

    def test_two_records_with_the_same_id_cannot_both_deliver(self):
        path = self.root / "timers.json"
        one = dict(id="t-same", row="R-1", state="armed", due=T0.isoformat(),
                   target="coordinator", name="check", prompt="check", attempts=0)
        other = dict(one, id="t-other")
        path.write_text(json.dumps([one, dict(one, prompt="again the same id"), other]))
        sent = []
        log = tm.Timers(path).tick(lambda t, m: sent.append(m) or "ok", {"R-1"}, T0)
        self.assertEqual(len([line for line in log if "appears 2 times" in line]), 2)
        self.assertEqual(len(sent), 1)

    def test_extremely_large_relative_due_gets_the_fallback(self):
        for due in ["+999999999999999m", "+999999999999999h"]:
            with self.subTest(due=due):
                _, note = tm.parse_due(due, T0)
                self.assertIsNotNone(note)

    def test_a_foreign_record_id_does_not_break_add(self):
        store = tm.Timers(self.root / "timers.json")
        store.path.write_text(json.dumps([{"id": []}]))
        self.assertTrue(store.add("R", "+1m", "n", "p", now=T0)["id"].startswith("t-"))


if __name__ == "__main__":
    unittest.main()
