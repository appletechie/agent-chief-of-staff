"""#25 C: no blank identity is written, no lease is not primary, and state readers keep their error contract.

A blank primary, session or worker used to be written and then rejected by the next read, turning a working lease
or claim into a broken one. A missing lease gave primary_gate() epoch 0, so a fenced tick delivered as if a primary
held it. A 4301-digit number or an unreadable parent directory escaped as a bare ValueError, or read as "no file".
datetime.max broke the never-raise fallback of parse_due. First patch: karutoil (#27).
"""
import os
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import dispatch_guard as dg
import lease as lz
import timers as tm

T0 = datetime(2026, 1, 16, 14, 0, tzinfo=timezone.utc)


def tmp() -> pathlib.Path:
    return pathlib.Path(tempfile.mkdtemp())


class BlankIdentities(unittest.TestCase):
    def test_a_blank_primary_never_creates_or_takes_the_lease(self):
        path = tmp() / "lease.json"
        lease = lz.Lease(path)
        for blank in ("", "   ", None):
            with self.subTest(blank=blank), self.assertRaises(lz.LeaseError):
                lease.init(blank, "h", now=T0)
        self.assertFalse(path.exists())
        lease.init("coordinator-a", "h", now=T0)
        before = path.read_text()
        for blank in ("", "  ", None):
            with self.subTest(blank=blank), self.assertRaises(lz.LeaseError):
                lease.take(blank, "h2", "handover", now=T0, force=True)
        self.assertEqual(path.read_text(), before)
        self.assertTrue(lease.is_primary("coordinator-a"))

    def test_a_blank_claim_name_session_or_worker_is_never_written(self):
        path = tmp() / "claims.json"
        for names, session, worker in ((["R-001"], "", "worker-aaaaaa"), (["R-001"], "session-x", " "),
                                       ([""], "session-x", "worker-aaaaaa"), ([], "session-x", "worker-aaaaaa"),
                                       (["R-001"], None, "worker-aaaaaa")):
            with self.subTest(names=names, session=session, worker=worker), self.assertRaises(ValueError):
                dg.Claims(path).claim(names, session, worker, now=T0)
        self.assertFalse(path.exists())
        dg.Claims(path).claim(["R-001"], "session-x", "worker-aaaaaa", now=T0)
        self.assertIsNone(dg.claim_problem(dg.Claims(path).load()["R-001"]))


class NoLease(unittest.TestCase):
    def test_no_lease_record_means_the_scheduled_job_does_nothing(self):
        root = tmp()
        lease = lz.Lease(root / "lease.json")
        logged = []
        self.assertIsNone(lz.primary_gate(lease, "coordinator pass", log=logged.append))
        self.assertEqual(logged, ["coordinator pass: no lease; skipped (nobody is primary until the lease is initialised)"])
        self.assertEqual(lease.epoch(), 0)  # the raw reading is unchanged; the gate and the fence decide

    def test_a_fenced_store_refuses_to_write_or_deliver_without_a_lease(self):
        root = tmp()
        lease = lz.Lease(root / "lease.json")
        store = tm.Timers(root / "timers.json", fence=lease.fenced)
        with self.assertRaises(lz.LeaseError):
            store.add("R-001", "+1m", "check", "look", now=T0, expect_epoch=0)
        self.assertFalse((root / "timers.json").exists())
        lease.init("coordinator-a", "h", now=T0)
        store.add("R-001", "+1m", "check", "look", now=T0, expect_epoch=1)
        (root / "lease.json").unlink()
        sent = []
        with self.assertRaises(lz.LeaseError):
            store.tick(lambda target, text: sent.append(text) or "ok", {"R-001"}, now=T0 + timedelta(hours=1), expect_epoch=0)
        self.assertEqual(sent, [])
        self.assertEqual(store.load()[0]["state"], "armed")

    def test_an_epoch_less_lease_still_acts_at_epoch_0(self):
        path = tmp() / "lease.json"
        path.write_text('{"primary": "coordinator-a", "renewed_at": "2026-01-16T14:00:00+00:00", "ttl_s": 7200}')
        lease = lz.Lease(path)
        self.assertEqual(lz.primary_gate(lease, "tick", log=lambda m: None), 0)
        with lease.fenced() as epoch:
            self.assertEqual(epoch, 0)


class StateReaders(unittest.TestCase):
    @unittest.skipUnless(hasattr(sys, "get_int_max_str_digits"), "no int digit limit before Python 3.11")
    def test_a_4301_digit_number_is_the_contract_error_not_a_bare_value_error(self):
        root = tmp()
        big = "1" * 4301
        (root / "lease.json").write_text('{"primary": "p", "renewed_at": "2026-01-16T14:00:00+00:00", "ttl_s": ' + big + "}")
        (root / "claims.json").write_text('{"R-001": ' + big + "}")
        (root / "timers.json").write_text("[" + big + "]")
        with self.assertRaises(lz.LeaseError):
            lz.Lease(root / "lease.json").epoch()
        self.assertIsNone(lz.primary_gate(lz.Lease(root / "lease.json"), "tick", log=lambda m: None))
        with self.assertRaises(dg.ClaimsCorrupt):
            dg.Claims(root / "claims.json").load()
        with self.assertRaises(tm.TimersCorrupt):
            tm.Timers(root / "timers.json").load()

    def test_an_unreadable_parent_is_not_a_missing_file(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root reads through mode 000")
        state = tmp() / "state"
        state.mkdir()
        for name in ("lease.json", "claims.json", "timers.json"):
            (state / name).write_text("{}")
        os.chmod(state, 0)
        try:
            with self.assertRaises(lz.LeaseError):
                lz.Lease(state / "lease.json").read()
            logged = []
            self.assertIsNone(lz.primary_gate(lz.Lease(state / "lease.json"), "tick", log=logged.append))
            self.assertIn("not primary here", logged[0])
            with self.assertRaises(dg.ClaimsCorrupt):
                dg.Claims(state / "claims.json").load()
            with self.assertRaises(tm.TimersCorrupt):
                tm.Timers(state / "timers.json").load()
        finally:
            os.chmod(state, 0o755)


class ExtremeClocks(unittest.TestCase):
    def test_parse_due_never_raises_near_datetime_max(self):
        top = datetime.max.replace(tzinfo=timezone.utc)
        for text in ("+1m", "+2h", "23:59Z", "soon", None):
            with self.subTest(text=text):
                due, note = tm.parse_due(text, top)
                self.assertLessEqual(due, top)
        due, note = tm.parse_due("+1m", top)
        self.assertEqual(due, top)
        self.assertIn("due now", note)
        self.assertEqual(tm.parse_wait("WAIT ci org/app#1 +1m", top)["due"], top)


if __name__ == "__main__":
    unittest.main()
