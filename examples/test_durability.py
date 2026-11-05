"""Concurrency tests for the durable state files. Run from examples/: python3 -m unittest -v test_durability

Each of these used to lose or duplicate data. A lost timer is a missed wake; a lost claim is a
duplicate dispatch; a second delivery of one timer breaks the at-most-once promise in docs/waking.md.
"""
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import dispatch_guard as dg
import timers as tm

T0 = datetime(2026, 1, 16, 14, 0, tzinfo=timezone.utc)


def run_together(count, body):
    """Start `count` threads that all enter `body` at the same moment."""
    barrier = threading.Barrier(count)

    def go(i):
        barrier.wait()
        body(i)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


class TempDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class TimerDurabilityTests(TempDir):
    def test_concurrent_adds_do_not_lose_timers(self):
        count = 20

        def add(i):
            tm.Timers(self.root / "timers.json").add("R-001", "+5m", f"t{i}", f"prompt {i}", now=T0)

        run_together(count, add)
        self.assertEqual(len(tm.Timers(self.root / "timers.json").load()), count)

    def test_overlapping_ticks_deliver_once(self):
        tm.Timers(self.root / "timers.json").add("R-001", "+1m", "soak", "check the soak", now=T0)
        sent = []

        def send(target, text):
            time.sleep(0.03)          # a send that outlives the next tick, which is the busy-session case
            sent.append(text)
            return "ok"

        run_together(3, lambda i: tm.Timers(self.root / "timers.json").tick(send, {"R-001"}, T0 + timedelta(minutes=5)))
        self.assertEqual(len(sent), 1, f"one timer delivered {len(sent)} times")

    def test_an_overlapping_tick_does_not_fail_a_live_send(self):
        tm.Timers(self.root / "timers.json").add("R-001", "+1m", "soak", "check the soak", now=T0)

        def send(target, text):
            time.sleep(0.03)
            return "ok"

        run_together(3, lambda i: tm.Timers(self.root / "timers.json").tick(send, {"R-001"}, T0 + timedelta(minutes=5)))
        states = {t["state"] for t in tm.Timers(self.root / "timers.json").load()}
        self.assertEqual(states, {"delivered"})


class ClaimDurabilityTests(TempDir):
    def test_concurrent_claims_are_all_recorded(self):
        count = 20

        def claim(i):
            dg.Claims(self.root / "claims.json").claim(
                [f"ISSUE-{i}"], f"session-{i}", f"worker-{i:06x}", T0)

        run_together(count, claim)
        self.assertEqual(len(dg.Claims(self.root / "claims.json").load()), count)

    def test_a_lost_claim_would_have_allowed_a_duplicate_dispatch(self):
        """Two sessions racing on the same key: the second must see the first's claim."""
        claims = dg.Claims(self.root / "claims.json")
        dg.Claims(self.root / "claims.json").claim(["ISSUE-900"], "coordinator-a1", "worker-aaaaaa", T0)
        sources = dg.Sources(open_prs_naming=lambda k: [], assignee=lambda k: None, live_workers=lambda: [])
        verdict = dg.guard("ISSUE-900", "R-001", "coordinator-b2", sources, claims, now=T0)
        self.assertFalse(verdict.allowed)
        self.assertIn("claimed by session coordinator-a1", " ".join(verdict.refusals))


if __name__ == "__main__":
    unittest.main()
