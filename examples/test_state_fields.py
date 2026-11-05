"""A valid JSON state file can still be invalid; protect every field parsed later, not only the file as a whole."""
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
        self.root = pathlib.Path(tempfile.mkdtemp())


class WakeGateSchemaTests(TempDir):
    def snapshot(self, value):
        path = self.root / "wake-gate.json"
        path.write_text(json.dumps(value))
        return wg.run({"ledger": lambda: {"R-001": "same"}}, path, persist=True, now=T0)

    def test_a_bad_timestamp_forces_a_pass(self):
        self.assertTrue(self.snapshot({"last_changed_at": "tomorrow", "parts": {}}).changed)
        self.assertTrue(self.snapshot({"last_changed_at": "2026-01-16T12:00:00", "parts": {}}).changed)

    def test_a_wrong_part_type_forces_a_pass(self):
        self.assertTrue(self.snapshot({"last_changed_at": T0.isoformat(), "parts": []}).changed)

    def test_normal_case_stays_unchanged(self):
        path = self.root / "wake-gate.json"
        path.write_text(json.dumps({"last_changed_at": T0.isoformat(), "parts": {"ledger": {"R-001": wg.digest("same")}}}))
        result = wg.run({"ledger": lambda: {"R-001": "same"}}, path, persist=False, now=T0)
        self.assertFalse(result.changed)


class LeaseSchemaTests(TempDir):
    def test_bad_renewed_at_or_ttl_is_Refused(self):
        path = self.root / "lease.json"
        for changes in [
            {"renewed_at": "yesterday"},
            {"renewed_at": "2026-01-16T12:00:00"},
            {"renewed_at": 0},
            {"ttl_s": "two hours"},
            {"ttl_s": -1},
            {"primary": None},
        ]:
            path.write_text(json.dumps({"primary": "a", "renewed_at": T0.isoformat(), "ttl_s": 7200} | changes))
            with self.assertRaises(lz.LeaseError) as error:
                lz.Lease(path).state()
            self.assertIn(str(path), str(error.exception))

    def test_old_lease_without_an_epoch_still_updates(self):
        path = self.root / "lease.json"
        lease = lz.Lease(path)
        lease.init("a", "h", now=T0)
        lease.renew("a", now=T0 + __import__('datetime').timedelta(minutes=30))
        self.assertEqual(lz.Lease(path).state(T0), "held")


class TimerSchemaTests(TempDir):
    def test_bad_due_date_is_a_surfaced_error_no_send(self):
        path = self.root / "timers.json"
        path.write_text(json.dumps([{"id": "t-1", "row": "R-001", "state": "armed", "due": "yesterday"}]))
        record = tm.Timers(path)
        log = record.tick(lambda target, text: self.fail("invalid timers must not deliver"), {"R-001"}, now=T0)
        self.assertTrue(any("invalid timestamp" in message for message in log))

    def test_naive_due_date_and_first_attempt_rejected(self):
        for field in ["due", "first_attempt_at"]:
            path = self.root / "timers.json"
            path.write_text(json.dumps([{"id": "t-1", "row": "R-001", "state": "armed", "due": T0.isoformat(), field: "2026-01-16T12:00:00"}]))
            with self.assertRaises(tm.TimersCorrupt):
                tm.Timers(path).load()


class ClaimSchemaTests(TempDir):
    def test_a_timestamp_on_a_claim_cannot_abort_the_guard(self):
        claims_path = self.root / "claims.json"
        claims_path.write_text(json.dumps({"ISSUE-1": {"session": "other", "at": "yesterday"}}))
        sources = dg.Sources(open_prs_naming=lambda k: [], assignee=lambda k: None, live_workers=lambda: [])
        verdict = dg.guard("ISSUE-1", "R-001", "me", sources, dg.Claims(claims_path), now=T0)
        self.assertFalse(verdict.allowed)
        self.assertTrue(any("invalid timestamp" in reason for reason in verdict.refusals))


if __name__ == "__main__":
    unittest.main()
