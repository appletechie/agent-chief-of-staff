"""Valid JSON with bad fields (#18): every state file that gates behaviour fails safe, field by field.

test_defensive.py covers files that do not parse. These cover files that parse but carry a bad value: an
unparseable or timezone-naive timestamp, a wrong type, a missing id. The rule in every module: a bad field never
crashes the loop and never reads as quiet or allowed. The wake gate reads CHANGED; the timer tick skips and logs
that one record and keeps ticking the rest; the dispatch guard refuses that name; the lease raises LeaseError and a
scheduled job reads it as "not primary here".
"""
import json
import pathlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import dispatch_guard as dg
import lease as lz
import timers as tm
import wake_gate as wg

T0 = datetime(2026, 1, 16, 14, 0, tzinfo=timezone.utc)


class TempDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class WakeGateFields(TempDir):
    SOURCES = {"ledger": lambda: {"R-001": "same"}}

    def snapshot(self, **fields):
        good = {"parts": {"ledger": {"R-001": wg.digest("same")}}, "checked_at": T0.isoformat(),
                "last_changed_at": (T0 - timedelta(minutes=10)).isoformat()}
        return wg.evaluate(self.SOURCES, dict(good, **fields), T0)

    def test_normal_case_stays_unchanged(self):
        result = self.snapshot()
        self.assertFalse(result.changed, result.lines)

    def test_unparseable_last_changed_at_forces_a_pass(self):
        result = self.snapshot(last_changed_at="tomorrow morning")
        self.assertTrue(result.changed)
        self.assertIn("not an ISO 8601 time with a time zone", "\n".join(result.lines))

    def test_naive_last_changed_at_forces_a_pass(self):
        self.assertTrue(self.snapshot(last_changed_at="2026-01-16T13:50:00").changed)

    def test_wrong_typed_last_changed_at_forces_a_pass(self):
        self.assertTrue(self.snapshot(last_changed_at=1768571400).changed)

    def test_parts_as_a_list_forces_a_pass(self):
        result = self.snapshot(parts=[])
        self.assertTrue(result.changed)
        self.assertIn("parts is list", "\n".join(result.lines))

    def test_a_part_that_is_not_an_object_forces_a_pass(self):
        result = self.snapshot(parts={"ledger": ["R-001"]})
        self.assertTrue(result.changed)
        self.assertIn("ledger", "\n".join(result.lines))

    def test_the_next_pass_reads_the_saved_snapshot_as_clean(self):
        path = self.root / "wake-gate.json"
        path.write_text(json.dumps({"parts": [], "last_changed_at": "2026-01-16T12:00:00"}))
        self.assertTrue(wg.run(self.SOURCES, path, persist=True, now=T0).changed)
        self.assertFalse(wg.run(self.SOURCES, path, persist=True, now=T0 + timedelta(minutes=5)).changed)


class LeaseFields(TempDir):
    GOOD = {"primary": "a", "host": "h", "since": T0.isoformat(), "renewed_at": T0.isoformat(), "ttl_s": 7200,
            "reason": "init", "epoch": 3}

    def write(self, **fields):
        path = self.root / "lease.json"
        path.write_text(json.dumps({k: v for k, v in dict(self.GOOD, **fields).items() if v is not ...}))
        return lz.Lease(path)

    def test_bad_fields_raise_lease_error_from_every_read(self):
        for fields in ({"renewed_at": "yesterday"}, {"renewed_at": "2026-01-16T12:00:00"}, {"renewed_at": 0},
                       {"renewed_at": ...}, {"ttl_s": "two hours"}, {"ttl_s": -1}, {"ttl_s": True}, {"ttl_s": ...},
                       {"primary": None}, {"primary": ""}, {"epoch": "3"}, {"epoch": 0}, {"host": 5},
                       {"since": "2026-01-16T09:00:00"}):
            lease = self.write(**fields)
            for read in (lambda: lease.state(T0), lease.holder, lease.epoch, lambda: lease.renew("a", now=T0)):
                with self.assertRaises(lz.LeaseError, msg=fields) as error:
                    read()
                self.assertIn(str(lease.path), str(error.exception))
            with self.assertRaises(lz.LeaseError):
                with lease.fenced():
                    self.fail("a fenced write ran on a broken lease")

    def test_take_refuses_over_a_broken_lease(self):
        lease = self.write(ttl_s="two hours")
        before = lease.path.read_text()
        with self.assertRaises(lz.LeaseError):
            lease.take("b", "h2", "handover", now=T0 + timedelta(hours=3), force=True)
        self.assertEqual(lease.path.read_text(), before)

    def test_a_scheduled_job_skips_with_one_log_line(self):
        logged = []
        self.assertIsNone(lz.primary_gate(self.write(renewed_at="yesterday"), "timer tick", log=logged.append))
        self.assertEqual(len(logged), 1)
        self.assertIn("timer tick: not primary here; skipped", logged[0])
        self.assertEqual(lz.primary_gate(self.write(), "timer tick", log=logged.append), 3)
        self.assertEqual(len(logged), 1)

    def test_a_fenced_tick_over_a_broken_lease_sends_nothing(self):
        lease = self.write()
        store = tm.Timers(self.root / "timers.json", fence=lease.fenced)
        store.add("R-001", "+1m", "x", "p", now=T0, expect_epoch=3)
        lease.path.write_text(json.dumps(dict(self.GOOD, renewed_at="yesterday")))
        with self.assertRaises(lz.LeaseError):
            store.tick(lambda target, text: self.fail("sent under a broken lease"), {"R-001"}, now=T0 + timedelta(hours=1),
                       expect_epoch=3)

    def test_legacy_lease_without_an_epoch_still_works(self):
        lease = self.write(epoch=..., since=...)
        self.assertEqual(lease.epoch(), 0)
        self.assertEqual(lease.state(T0 + timedelta(minutes=30)), "held")
        lease.renew("a", now=T0 + timedelta(minutes=30))
        taken = lease.take("b", "h2", "handover", now=T0 + timedelta(hours=3))
        self.assertEqual((taken["epoch"], taken["previous"]["epoch"]), (1, None))

    def test_valid_lease_reads_as_before(self):
        lease = self.write()
        self.assertEqual((lease.state(T0), lease.holder(), lease.epoch()), ("held", "a", 3))


class TimerFields(TempDir):
    def store(self, *records):
        path = self.root / "timers.json"
        good = {"id": "t-good", "row": "R-001", "name": "check", "due": (T0 - timedelta(minutes=1)).isoformat(),
                "target": "coordinator", "owner": "coordinator", "prompt": "p", "state": "armed", "attempts": 0,
                "source": "cli", "note": None}
        path.write_text(json.dumps([dict(good, **r) if isinstance(r, dict) else r for r in records] + [good]))
        return tm.Timers(path)

    def tick(self, store):
        sent = []
        log = store.tick(lambda target, text: sent.append(text) or "queued", {"R-001"}, now=T0)
        return log, sent

    def test_bad_due_is_skipped_and_the_rest_still_tick(self):
        store = self.store({"id": "t-bad", "due": "not-a-time"})
        log, sent = self.tick(store)
        self.assertIn("t-bad invalid", log[0])
        self.assertIn("'due'", log[0])
        self.assertEqual(log[1:], ["t-good delivered to coordinator"])
        self.assertEqual(len(sent), 1)
        states = {t["id"]: t["state"] for t in store.load()}
        self.assertEqual(states, {"t-bad": "armed", "t-good": "delivered"})

    def test_naive_due_or_first_attempt_is_invalid(self):
        for fields in ({"due": "2026-01-16T13:00:00"}, {"first_attempt_at": "2026-01-16T12:00:00"}):
            log, sent = self.tick(self.store(dict(fields, id="t-naive")))
            self.assertIn("t-naive invalid", log[0], fields)
            self.assertEqual(len(sent), 1)

    def test_missing_id_wrong_types_and_non_objects_are_skipped(self):
        store = self.store("junk", {"id": None}, {"id": "t-target", "target": 5}, {"id": "t-state", "state": "later"},
                           {"id": "t-attempts", "attempts": "1"})
        log, sent = self.tick(store)
        self.assertEqual([line.split(" invalid")[0] for line in log[:5]],
                         ["record 0", "record 1", "t-target", "t-state", "t-attempts"])
        self.assertEqual(log[5:], ["t-good delivered to coordinator"])

    def test_finished_records_need_only_an_id_and_a_state(self):
        log, _ = self.tick(self.store({"id": "t-old", "state": "delivered", "due": "whenever"}))
        self.assertEqual(log, ["t-good delivered to coordinator"])

    def test_add_still_works_beside_a_bad_record(self):
        store = self.store({"id": None})
        timer = store.add("R-002", "+5m", "x", "p", now=T0)
        self.assertIn(timer["id"], [t.get("id") for t in store.load() if isinstance(t, dict)])

    def test_a_file_that_is_not_an_array_still_delivers_nothing(self):
        path = self.root / "timers.json"
        path.write_text('{"timers": []}')
        log = tm.Timers(path).tick(lambda t, m: self.fail("sent from a corrupt store"), {"R-001"}, now=T0)
        self.assertEqual(len(log), 1)
        self.assertIn("expected an array", log[0])


class ClaimFields(TempDir):
    SOURCES = dg.Sources(open_prs_naming=lambda k: [], assignee=lambda k: None, live_workers=lambda: [])

    def claims(self, data):
        path = self.root / "claims.json"
        path.write_text(json.dumps(data))
        return dg.Claims(path)

    def test_a_bad_timestamp_refuses_instead_of_escaping_the_guard(self):
        claims = self.claims({"ISSUE-1": {"session": "other", "at": "yesterday"}})
        verdict = dg.guard("ISSUE-1", "R-001", "me", self.SOURCES, claims, now=T0)
        self.assertFalse(verdict.allowed)
        self.assertIn("ISSUE-1 has an invalid claim", " ".join(verdict.refusals))

    def test_naive_at_missing_worker_or_non_object_refuses_even_for_this_session(self):
        for bad in ({"session": "me", "worker": "w1", "at": "2026-01-16T13:00:00"}, {"session": "me", "at": T0.isoformat()},
                    "R-001", None):
            verdict = dg.guard("ISSUE-1", "R-001", "me", self.SOURCES, self.claims({"R-001": bad}), now=T0)
            self.assertFalse(verdict.allowed, bad)

    def test_a_bad_claim_never_expires_and_is_held_until_overridden(self):
        claims = self.claims({"R-001": {"session": "other", "worker": "w1", "at": "last week"}})
        later = T0 + dg.CLAIM_TTL * 3
        self.assertFalse(dg.guard(None, "R-001", "me", self.SOURCES, claims, now=later).allowed)
        with self.assertRaises(dg.ClaimHeld):
            claims.claim(["R-001"], "me", "w2", later)
        claims.claim(["R-001"], "me", "w2", later, override="bad claim from a crashed session, checked by hand")
        self.assertEqual(claims.load()["R-001"]["session"], "me")

    def test_a_bad_claim_on_another_name_survives_a_claim(self):
        claims = self.claims({"R-009": {"session": "other", "at": "yesterday"}})
        claims.claim(["R-001"], "me", "w1", T0)
        self.assertEqual(claims.load()["R-009"], {"session": "other", "at": "yesterday"})

    def test_valid_claims_behave_as_before(self):
        claims = self.claims({})
        claims.claim(["R-001"], "other", "w1", T0)
        self.assertFalse(dg.guard(None, "R-001", "me", self.SOURCES, claims, now=T0).allowed)
        self.assertTrue(dg.guard(None, "R-001", "other", self.SOURCES, claims, now=T0).allowed)
        self.assertTrue(dg.guard(None, "R-001", "me", self.SOURCES, claims, now=T0 + dg.CLAIM_TTL).allowed)


class NaiveClock(unittest.TestCase):
    """A naive clock is refused before it can write a naive time (timers: karutoil's #18 follow-up; lease, wake gate
    and claims: Codex on #21)."""

    def test_add_tick_and_wait_refuse_a_naive_clock(self):
        with tempfile.TemporaryDirectory() as root:
            path = pathlib.Path(root) / "timers.json"
            with self.assertRaises(ValueError):
                tm.Timers(path).add("R-001", "+5m", "x", "p", now=datetime(2026, 1, 16, 14, 0))
            self.assertFalse(path.exists())
            store = tm.Timers(path)
            store.add("R-001", "+1m", "x", "p", now=T0)
            with self.assertRaises(ValueError):
                store.tick(lambda target, text: self.fail("sent on a naive clock"), {"R-001"}, now=datetime(2026, 1, 16, 15, 0))
            with self.assertRaises(ValueError):
                tm.parse_wait("WAIT ci org/app#88 +45m", datetime(2026, 1, 16, 14, 0))

    def test_lease_writes_refuse_a_naive_clock_and_the_lease_stays_valid(self):
        with tempfile.TemporaryDirectory() as root:
            lease = lz.Lease(pathlib.Path(root) / "lease.json")
            with self.assertRaises(ValueError):
                lease.init("a", "h", now=datetime(2026, 1, 16, 14, 0))
            self.assertFalse(lease.path.exists())
            lease.init("a", "h", now=T0)
            before = lease.path.read_text()
            with self.assertRaises(ValueError):
                lease.renew("a", now=datetime(2026, 1, 16, 15, 0))
            with self.assertRaises(ValueError):
                lease.take("b", "h2", "handover", now=datetime(2026, 1, 16, 19, 0), force=True)
            self.assertEqual(lease.path.read_text(), before)
            lease.renew("a", now=T0 + timedelta(minutes=5))
            self.assertEqual(lease.state(T0 + timedelta(minutes=10)), "held")

    def test_wake_gate_and_claims_refuse_a_naive_clock(self):
        with tempfile.TemporaryDirectory() as root:
            root = pathlib.Path(root)
            with self.assertRaises(ValueError):
                wg.run({}, root / "wake-gate.json", persist=True, now=datetime(2026, 1, 16, 14, 0))
            self.assertFalse((root / "wake-gate.json").exists())
            claims = dg.Claims(root / "claims.json")
            with self.assertRaises(ValueError):
                claims.claim(["R-001"], "me", "w1", datetime(2026, 1, 16, 14, 0))
            self.assertFalse(claims.path.exists())
            sources = dg.Sources(open_prs_naming=lambda k: [], assignee=lambda k: None, live_workers=lambda: [])
            with self.assertRaises(ValueError):
                dg.guard(None, "R-001", "me", sources, claims, now=datetime(2026, 1, 16, 14, 0))


if __name__ == "__main__":
    unittest.main()
