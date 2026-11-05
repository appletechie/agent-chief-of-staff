"""State-file handling used to turn a corrupt snapshot into a raw Python traceback.

Adds one check per documented failure mode, and proves where recovery is possible (the wake gate always
self-heals by writing a fresh snapshot). The others must fail closed with a useful message, not traceback.
"""
import json
import pathlib
import tempfile
import unittest
from datetime import datetime, timezone

import dispatch_guard as dg
import lease as lz
import timers as tm
import wake_gate as wg

T0 = datetime(2026, 1, 16, 14, 0, tzinfo=timezone.utc)


class TempDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)


class WakeGateTests(TempDir):
    def test_corrupt_snapshot_is_a_pass_not_a_traceback(self):
        path = self.root / "wake-gate.json"
        path.write_text("{not json")
        result = wg.run({"ledger": lambda: {"R-001": "a"}}, path, persist=True, now=T0)
        self.assertTrue(result.changed)
        self.assertIn("previous snapshot unreadable", "\n".join(result.lines))
        # a fresh snapshot was written over the corrupt one, so the next tick is valid
        self.assertEqual(json.loads(path.read_text())["last_changed_at"], T0.isoformat())

    def test_wrong_shape_is_also_a_pass(self):
        path = self.root / "wake-gate.json"
        path.write_text("[]")
        self.assertTrue(wg.run({}, path, persist=True, now=T0).changed)

    def test_missing_snapshot_works(self):
        path = self.root / "wake-gate.json"
        self.assertTrue(wg.run({}, path, persist=False, now=T0).changed)
        self.assertFalse(path.exists())


class LeaseTests(TempDir):
    def test_corrupt_snapshot_fails_closed(self):
        path = self.root / "lease.json"
        for content in ["{not json", "", "[1]", "null"]:
            path.write_text(content)
            with self.assertRaises(lz.LeaseError) as ctx:
                lz.Lease(path).epoch()
            self.assertIsInstance(ctx.exception, lz.LeaseError)
            self.assertNotIsInstance(ctx.exception, json.JSONDecodeError)

    def test_corrupt_state_cannot_lead_to_a_quiet_writer(self):
        path = self.root / "lease.json"
        lease = lz.Lease(path)
        lease.init("primary", "h", now=T0)
        path.write_text("hmm")
        with self.assertRaises(lz.LeaseError):
            lease.renew("primary", now=T0)

    def test_normal_sequence_unchanged(self):
        path = self.root / "lease.json"
        lease = lz.Lease(path)
        value = lease.init("a", "h", now=T0)
        self.assertEqual(value["epoch"], 1)
        lease.renew("a", now=T0)
        self.assertEqual(lease.is_primary("a"), True)


class TimerTests(TempDir):
    def test_corrupt_timers_store_explains_and_delivers_nothing(self):
        path = self.root / "timers.json"
        path.write_text("{not json")
        store = tm.Timers(path)
        log = store.tick(lambda t, m: self.fail("no messages should be delivered"), {"R-001"}, now=T0)
        self.assertEqual(len(log), 1)
        self.assertIn("corrupt", log[0])

    def test_wrong_shape_is_defects_not_tracebacks(self):
        path = self.root / "timers.json"
        path.write_text('{"id": "t-123"}')
        with self.assertRaises(tm.TimersCorrupt):
            tm.Timers(path).load()

    def test_empty_store_is_valid(self):
        tm.Timers(self.root / "timers.json").add("R-001", "+1m", "x", "p", now=T0)
        self.assertEqual(len(tm.Timers(self.root / "timers.json").load()), 1)


class ClaimTests(TempDir):
    def test_corrupt_claims_are_a_refusal_not_a_silent_pass(self):
        claims_path = self.root / "claims.json"
        claims_path.write_text("{not json")
        sources = dg.Sources(open_prs_naming=lambda k: [], assignee=lambda k: None, live_workers=lambda: [])
        result = dg.guard("ISSUE-1", "R-001", "a", sources, dg.Claims(claims_path), now=T0)
        self.assertFalse(result.allowed)
        self.assertIn("corrupt", " ".join(result.refusals))

    def test_good_claims_work(self):
        claims = dg.Claims(self.root / "claims.json")
        claims.claim(["ISSUE-1"], "a", "w1", T0)
        self.assertEqual(claims.load()["ISSUE-1"]["session"], "a")


if __name__ == "__main__":
    unittest.main()
