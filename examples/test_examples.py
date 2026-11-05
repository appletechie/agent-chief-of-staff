"""Tests for the example sketches. Run from this directory: python3 -m unittest -v test_examples"""
import json
import tempfile
import subprocess
import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import dispatch_guard as dg
import ledger_store as ls
import lease as lz
import merge_gate as mg
import timers as tm
import wake_gate as wg

T0 = datetime(2026, 1, 16, 14, 0, tzinfo=timezone.utc)


class TempDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class LedgerTests(TempDir):
    def test_ids_are_never_reused_after_close(self):
        led = ls.Ledger(self.root)
        a = led.write_row(None, next="first")
        led.close_row(a["id"], proof="merged abc1234")
        self.assertEqual(led.write_row(None, next="second")["id"], "R-002")

    def test_close_requires_proof(self):
        led = ls.Ledger(self.root)
        a = led.write_row(None, next="first")
        with self.assertRaises(ValueError):
            led.close_row(a["id"], proof="  ")

    def test_long_rows_spill_to_notes(self):
        led = ls.Ledger(self.root)
        row = led.write_row(None, next="x" * 900)
        self.assertLessEqual(ls.row_chars(row), ls.ROW_BUDGET)
        self.assertTrue((self.root / "ledger" / "notes" / f"{row['id']}.md").exists())

    def test_concurrent_writers_do_not_lose_rows(self):
        led = ls.Ledger(self.root)
        led.write_row(None, next="seed")
        threads = [threading.Thread(target=lambda i=i: ls.Ledger(self.root).write_row(None, next=f"row {i}"))
                   for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        ids = [r["id"] for r in led.rows()]
        self.assertEqual(len(ids), 9)
        self.assertEqual(len(set(ids)), 9)

    def test_concurrent_processes_do_not_lose_rows(self):
        # Threads share one process; the lock must also hold across separate processes.
        led = ls.Ledger(self.root)
        led.write_row(None, next="seed")
        here = str(Path(__file__).resolve().parent)
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import ledger_store as ls\n"
                "for j in range(5): ls.Ledger(sys.argv[2]).write_row(None, next=f'p{sys.argv[3]} w{j}')")
        procs = [subprocess.Popen([sys.executable, "-c", code, here, str(self.root), str(i)]) for i in range(6)]
        self.assertEqual([p.wait(timeout=60) for p in procs], [0] * 6)
        ids = [r["id"] for r in led.rows()]
        self.assertEqual((len(ids), len(set(ids))), (31, 31))
        self.assertEqual(led.check()[0], [])

    def test_checker_flags_bad_rows(self):
        led = ls.Ledger(self.root)
        led.write_row(None, next="ok")
        with led.path.open("a") as fh:
            fh.write("| R-001 | bad key | nobody | — | — | tomorrow |\n")
        errors, _ = led.check()
        joined = "\n".join(errors)
        for needle in ("duplicate id", "key must be", "no next action", "owner must be", "due must be"):
            self.assertIn(needle, joined)

    def test_queues(self):
        cases = {"—": "ACTIVE", "worker; then owner": "ACTIVE", "owner: decision": "DECISION",
                 "owner: hands": "HANDS", "time": "TIME", "vendor contact": "PEOPLE"}
        for waits, queue in cases.items():
            self.assertEqual(ls.queue_of({"waits": waits}), queue, waits)

    def test_queue_reads_the_owner(self):
        # Issue #3, finding 5: a row the owner holds with nothing blocking it is not ready for a worker.
        cases = [("owner", "—", "DECISION"), ("owner", "", "DECISION"), ("worker-7f3a2c", "—", "ACTIVE"),
                 ("coordinator", "—", "ACTIVE"), ("coordinator (owner closed: 7f3a2c)", "—", "ACTIVE"),
                 ("owner", "owner: hands", "HANDS"), ("owner", "time", "TIME"), ("owner", "worker", "ACTIVE")]
        for owner, waits, queue in cases:
            self.assertEqual(ls.queue_of({"owner": owner, "waits": waits}), queue, (owner, waits))

    def test_missing_key_warns_where_policy_requires_one(self):
        # Issue #3, finding 6: the checker implements the documented key warning.
        led = ls.Ledger(self.root, require_key=lambda r: r["owner"].startswith("worker-"))
        led.write_row(None, owner="worker-7f3a2c", next="fix the loop")
        led.write_row(None, owner="owner", next="pick a price")
        led.write_row(None, key="ISSUE-2", owner="worker-7f3a2c", next="keyed")
        errors, warnings = led.check()
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("R-001", warnings[0])
        self.assertEqual(ls.Ledger(self.root).check(), ([], []))

    def test_spill_cuts_every_long_cell(self):
        # Issue #3, finding 6: a long waits-on is cut too, so the row stops warning on every check.
        led = ls.Ledger(self.root)
        for fields in ({"next": "short", "waits": "w" * 900}, {"next": "n" * 900, "waits": "w" * 900}):
            row = led.write_row(None, **fields)
            self.assertLessEqual(ls.row_chars(row), ls.ROW_BUDGET, fields.keys())
            note = (self.root / "ledger" / "notes" / f"{row['id']}.md").read_text()
            self.assertIn("w" * 900, note)
            self.assertIn(f"ledger/notes/{row['id']}.md", row["waits"])
        self.assertEqual(led.check(), ([], []))


class WakeGateTests(TempDir):
    def test_unchanged_then_changed(self):
        state = {"n": 1}
        sources = {"ledger": lambda: {"R-001": state["n"]}}
        path = self.root / "wg.json"
        self.assertTrue(wg.run(sources, path, persist=True, now=T0).changed)
        self.assertFalse(wg.run(sources, path, persist=True, now=T0 + timedelta(minutes=30)).changed)
        state["n"] = 2
        self.assertTrue(wg.run(sources, path, persist=True, now=T0 + timedelta(minutes=60)).changed)

    def test_unreadable_source_is_changed_and_keeps_snapshot(self):
        path = self.root / "wg.json"
        wg.run({"ledger": lambda: {"R-001": 1}}, path, persist=True, now=T0)

        def broken():
            raise OSError("disk")
        result = wg.run({"ledger": broken}, path, persist=True, now=T0 + timedelta(minutes=30))
        self.assertTrue(result.changed)
        self.assertIn("R-001", json.loads(path.read_text())["parts"]["ledger"])

    def test_max_quiet_forces_a_pass(self):
        path = self.root / "wg.json"
        sources = {"ledger": lambda: {"R-001": 1}}
        wg.run(sources, path, persist=True, now=T0)
        self.assertTrue(wg.run(sources, path, persist=True, now=T0 + timedelta(hours=5)).changed)

    def test_companion_does_not_consume_the_change(self):
        path = self.root / "wg.json"
        data = {"n": 1}
        sources = {"ledger": lambda: {"R-001": data["n"]}}
        wg.run(sources, path, persist=True, now=T0)
        data["n"] = 2
        self.assertTrue(wg.run(sources, path, persist=False, now=T0 + timedelta(minutes=1)).changed)
        self.assertTrue(wg.run(sources, path, persist=True, now=T0 + timedelta(minutes=2)).changed)

    def test_future_snapshot_forces_a_pass(self):
        # Issue #3, finding 6: a last-pass time ahead of the clock must not stretch the quiet period.
        path = self.root / "wg.json"
        sources = {"ledger": lambda: {"R-001": 1}}
        wg.run(sources, path, persist=True, now=T0 + timedelta(days=10))
        result = wg.run(sources, path, persist=True, now=T0 + timedelta(days=5))
        self.assertTrue(result.changed)
        self.assertIn("in the future", result.render())
        self.assertEqual(json.loads(path.read_text())["last_changed_at"], (T0 + timedelta(days=5)).isoformat())
        self.assertFalse(wg.run(sources, path, persist=True, now=T0 + timedelta(days=5, hours=1)).changed)

    def test_new_only_parts_ignore_resolved_items(self):
        path = self.root / "wg.json"
        found = {"f1": "fail", "f2": "warn"}
        kw = {"new_only": frozenset({"health"})}
        wg.run({"health": lambda: dict(found)}, path, persist=True, now=T0, **kw)
        found.pop("f1")
        self.assertFalse(wg.run({"health": lambda: dict(found)}, path, True, T0 + timedelta(minutes=1), **kw).changed)


class TimerTests(TempDir):
    def test_wait_line_parsing_is_lenient(self):
        self.assertEqual(tm.parse_wait("WAIT ci org/app#88 +45m", T0)["due"], T0 + timedelta(minutes=45))
        # Issue #1, finding 4: with the ref omitted, the time is still the check-by.
        no_ref = tm.parse_wait("WAIT ci +45m", T0)
        self.assertEqual((no_ref["ref"], no_ref["due"], no_ref["note"]), ("", T0 + timedelta(minutes=45), None))
        loose = tm.parse_wait("WAIT", T0)
        self.assertEqual((loose["kind"], loose["due"]), ("other", T0 + timedelta(minutes=60)))
        self.assertIsNotNone(tm.parse_wait("WAIT deploy d-42 sometime soon", T0)["note"])
        self.assertIsNone(tm.parse_wait("all done", T0))

    def test_crash_mid_send_is_never_resent(self):
        store = tm.Timers(self.root / "t.json")
        t = store.add("R-001", "+1m", "x", "p", now=T0)
        timers = store.load()
        timers[0]["state"] = "sending"
        store.save(timers)
        sent = []
        store.tick(lambda target, text: sent.append(text), {"R-001"}, now=T0 + timedelta(minutes=5))
        self.assertEqual(sent, [])
        self.assertEqual(store.load()[0]["state"], "failed")
        self.assertEqual(store.load()[0]["id"], t["id"])

    def test_closed_row_cancels(self):
        store = tm.Timers(self.root / "t.json")
        store.add("R-009", "+1m", "x", "p", now=T0)
        store.tick(lambda *a: "ok", set(), now=T0 + timedelta(minutes=5))
        self.assertEqual(store.load()[0]["state"], "cancelled")

    def test_send_errors_retry_then_fail(self):
        # Issue #3, finding 6: retries are bounded by time since the first attempt, not by a count of ticks.
        store = tm.Timers(self.root / "t.json")
        store.add("R-001", "+1m", "x", "p", now=T0)

        def boom(*_):
            raise ConnectionError
        first = T0 + timedelta(minutes=5)
        for i in range(10):
            store.tick(boom, {"R-001"}, now=first + timedelta(minutes=i))
        self.assertEqual((store.load()[0]["state"], store.load()[0]["attempts"]), ("armed", 10))
        sent = []
        store.tick(lambda target, text: sent.append(text) or "ok", {"R-001"}, now=first + timedelta(minutes=30))
        self.assertEqual((len(sent), store.load()[0]["state"]), (1, "delivered"))

        store = tm.Timers(self.root / "t2.json")
        store.add("R-001", "+1m", "x", "p", now=T0)
        store.tick(boom, {"R-001"}, now=first)
        store.tick(boom, {"R-001"}, now=first + tm.RETRY_WINDOW - timedelta(minutes=1))
        self.assertEqual(store.load()[0]["state"], "armed")
        log = store.tick(boom, {"R-001"}, now=first + tm.RETRY_WINDOW)
        self.assertEqual(store.load()[0]["state"], "failed")
        self.assertIn("retry window over", log[0])


class GuardTests(TempDir):
    def sources(self, prs=(), assignee=None, workers=()):
        return dg.Sources(open_prs_naming=lambda k: list(prs), assignee=lambda k: assignee,
                          live_workers=lambda: list(workers))

    def test_open_pr_by_anyone_refuses(self):
        v = dg.guard("ISSUE-1", "R-001", "s1", self.sources(prs=[{"repo": "o/r", "number": 1, "author": "teammate"}]),
                     dg.Claims(self.root / "c.json"), now=T0)
        self.assertFalse(v.allowed)

    def test_clear_allows(self):
        v = dg.guard("ISSUE-1", "R-001", "s1", self.sources(), dg.Claims(self.root / "c.json"), now=T0)
        self.assertTrue(v.allowed)

    def test_failed_lookup_is_a_warning(self):
        def boom(_):
            raise TimeoutError
        src = dg.Sources(open_prs_naming=boom, assignee=lambda k: None, live_workers=lambda: [])
        v = dg.guard("ISSUE-1", "R-001", "s1", src, dg.Claims(self.root / "c.json"), now=T0)
        self.assertTrue(v.warnings)

    def test_other_sessions_claim_refuses_until_ttl(self):
        claims = dg.Claims(self.root / "c.json")
        claims.claim(["R-001", "ISSUE-1"], "s2", "worker-aaaaaa", T0)
        self.assertFalse(dg.guard("ISSUE-1", "R-001", "s1", self.sources(), claims, now=T0).allowed)
        later = T0 + dg.CLAIM_TTL + timedelta(minutes=1)
        self.assertTrue(dg.guard("ISSUE-1", "R-001", "s1", self.sources(), claims, now=later).allowed)

    def test_override_needs_a_reason(self):
        with self.assertRaises(ValueError):
            dg.guard("ISSUE-1", "R-001", "s1", self.sources(), dg.Claims(self.root / "c.json"), allow_duplicate=" ")


class MergeGateTests(unittest.TestCase):
    def pr(self, **kw):
        base = dict(repo="org/app", number=1, title="ISSUE-1 fix", body="", head_sha="abc1234", is_draft=False,
                    mergeable=True, checks={"test": "success"}, reviews=[mg.Review("abc1234", "No major issues.")],
                    unresolved_threads=0, last_push_at=T0)
        base.update(kw)
        return mg.PR(**base)

    policy = mg.Policy(deploys_on_merge=frozenset({"org/site"}))

    def action(self, row="fix it", now=T0 + timedelta(hours=1), **kw):
        return mg.decide(self.pr(**kw), row, self.policy, now=now).action

    def test_green_merges(self):
        self.assertEqual(self.action(), "MERGE")

    def test_holds(self):
        self.assertEqual(self.action(row="HOLD: owner review"), "REPORT")
        self.assertEqual(self.action(body="Draft: merge only after owner sign-off"), "REPORT")
        self.assertEqual(self.action(is_draft=True), "REPORT")
        self.assertEqual(self.action(row="pinned and held at 1.0 for now"), "MERGE")

    def test_review_on_old_sha_does_not_count(self):
        old = [mg.Review("0ld5ha0", "No major issues.")]
        self.assertEqual(self.action(reviews=old), "REQUEST_REVIEW")
        self.assertEqual(self.action(reviews=old, review_requested_for={"abc1234"}), "WAIT")
        self.assertEqual(self.action(reviews=old, now=T0 + timedelta(minutes=10)), "WAIT")

    def test_unknown_review_format_is_not_clean(self):
        self.assertEqual(self.action(reviews=[mg.Review("abc1234", "Summary: refactors the loop.")]), "REPORT")

    def test_checks(self):
        self.assertEqual(self.action(checks={"test": "in_progress"}), "WAIT")
        self.assertEqual(self.action(checks={"test": "cancelled"}), "REPORT")
        self.assertEqual(self.action(checks={}), "WAIT")

    def test_clean_phrase_does_not_hide_findings(self):
        # Issue #1, finding 1: a clean phrase and a P1 in the same receipt is not clean.
        mixed = [mg.Review("abc1234", "Looks good to merge. One P1 bug in the retry loop.")]
        self.assertEqual(self.action(reviews=mixed), "REPORT")
        self.assertEqual(self.action(reviews=[mg.Review("abc1234", "No major issues.")]), "MERGE")

    def test_explicit_verdict_line_wins(self):
        self.assertEqual(self.action(reviews=[mg.Review("abc1234", "Notes on the loop.\nverdict: clean\n")]), "MERGE")
        self.assertEqual(self.action(reviews=[mg.Review("abc1234", "No major issues.\nVerdict: findings")]), "REPORT")

    def test_required_checks(self):
        # Issue #1, findings 2 and 3: a required check must report and must pass; unlisted checks may skip.
        policy = mg.Policy(deploys_on_merge=frozenset(), required_checks={"org/app": frozenset({"test", "e2e"})})
        decide = lambda now=T0 + timedelta(hours=1), **kw: mg.decide(self.pr(**kw), "fix it", policy, now=now).action
        self.assertEqual(decide(checks={"test": "success"}, now=T0 + timedelta(minutes=10)), "WAIT")
        self.assertEqual(decide(checks={"test": "success", "e2e": "skipped"}), "REPORT")
        self.assertEqual(decide(checks={"test": "success", "e2e": "success", "lint": "skipped"}), "MERGE")
        # Issue #3, finding 4: neutral on a required check passes (as the VCS host's own gate treats it);
        # skipped on a required check stays red.
        self.assertEqual(decide(checks={"test": "success", "e2e": "neutral"}), "MERGE")
        self.assertEqual(decide(checks={"test": "neutral", "e2e": "skipped"}), "REPORT")

    def test_required_check_that_never_reports_is_reported(self):
        # Issue #3, finding 4: a missing required check waits for check_wait after the last push, then reports.
        policy = mg.Policy(deploys_on_merge=frozenset(), required_checks={"org/app": frozenset({"test", "e2e"})})
        decide = lambda after: mg.decide(self.pr(checks={"test": "success"}), "fix it", policy, now=T0 + after)
        self.assertEqual(decide(policy.check_wait - timedelta(minutes=1)).action, "WAIT")
        for after in (policy.check_wait, timedelta(days=1), timedelta(days=30)):
            got = decide(after)
            self.assertEqual((got.action, got.reason), ("REPORT", "required checks never reported: e2e"))

    def test_deploy_on_merge_repo_is_never_auto_merged(self):
        self.assertEqual(self.action(repo="org/site"), "REPORT")

    def test_threads_block(self):
        self.assertEqual(self.action(unresolved_threads=2), "REPORT")


class LeaseTests(TempDir):
    def test_expired_lease_is_not_taken_automatically(self):
        lease = lz.Lease(self.root / "lease.json")
        lease.init("a", "host-a", now=T0)
        self.assertEqual(lease.state(T0 + timedelta(hours=3)), "expired")
        self.assertEqual(lease.holder(), "a")
        with self.assertRaises(lz.LeaseError):
            lease.renew("b", now=T0)
        with self.assertRaises(lz.LeaseError):
            lease.take("b", "host-b", reason="")
        lease.take("b", "host-b", reason="old primary silent past its TTL", now=T0 + timedelta(hours=3))
        self.assertTrue(lease.is_primary("b"))

    def test_take_needs_expiry_or_force_and_bumps_the_epoch(self):
        # Issue #3, finding 3: a held lease is not taken by accident; every take moves the fencing token.
        lease = lz.Lease(self.root / "lease.json")
        lease.init("a", "host-a", now=T0)
        self.assertEqual(lease.epoch(), 1)
        with self.assertRaises(lz.LeaseError):
            lease.take("b", "host-b", reason="planned rollover", now=T0 + timedelta(minutes=10))
        self.assertEqual((lease.holder(), lease.epoch()), ("a", 1))
        lease.take("b", "host-b", reason="planned rollover", now=T0 + timedelta(minutes=10), force=True)
        self.assertEqual((lease.holder(), lease.epoch()), ("b", 2))
        prev = lease.read()["previous"]
        self.assertEqual((prev["primary"], prev["epoch"], prev["renewed_at"], prev["state_at_take"]),
                         ("a", 1, T0.isoformat(), "held"))
        self.assertEqual(lease.renew("b", now=T0 + timedelta(minutes=20))["epoch"], 2)


if __name__ == "__main__":
    unittest.main()
