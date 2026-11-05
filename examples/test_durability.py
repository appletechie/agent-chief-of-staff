"""Concurrency and fencing tests for the sketches (issues #3 and #6). Run from this directory:
python3 -m unittest -v test_durability"""
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import dispatch_guard as dg
import ledger_store as ls
import lease as lz
import timers as tm
import wake_gate as wg

T0 = datetime(2026, 1, 16, 14, 0, tzinfo=timezone.utc)
HERE = str(Path(__file__).resolve().parent)


def together(n, fn):
    """Run fn(i) in n threads released at the same instant. Returns the exceptions raised, by index."""
    barrier, errors = threading.Barrier(n), {}

    def run(i):
        barrier.wait()
        try:
            fn(i)
        except Exception as error:
            errors[i] = error
    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return errors


def processes(code, *args, n=6):
    procs = [subprocess.Popen([sys.executable, "-c", code, HERE, *map(str, args), str(i)]) for i in range(n)]
    return [p.wait(timeout=60) for p in procs]


class TempDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class TimerConcurrencyTests(TempDir):
    def test_concurrent_adds_all_survive(self):
        store = tm.Timers(self.root / "t.json")
        self.assertEqual(together(20, lambda i: store.add("R-001", "+1m", f"n{i}", "p", now=T0)), {})
        self.assertEqual(len({t["id"] for t in store.load()}), 20)

    def test_concurrent_adds_across_processes(self):
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import timers as tm\n"
                "for j in range(5): tm.Timers(sys.argv[2]).add('R-001', '+1m', f'p{sys.argv[3]} {j}', 'p')")
        self.assertEqual(processes(code, self.root / "t.json"), [0] * 6)
        self.assertEqual(len(tm.Timers(self.root / "t.json").load()), 30)

    def test_overlapping_ticks_deliver_once(self):
        store = tm.Timers(self.root / "t.json")
        store.add("R-001", "+1m", "x", "p", now=T0)
        sent = []

        def slow(target, text):  # a send that outlives the next tick: the designed case, not a rare one
            sent.append(text)
            time.sleep(0.3)
            return "ok"
        logs = {}
        errors = together(5, lambda i: logs.__setitem__(i, store.tick(slow, {"R-001"}, now=T0 + timedelta(minutes=5))))
        self.assertEqual(errors, {})
        self.assertEqual(len(sent), 1)
        delivered = [log for log in logs.values() if any("delivered to" in line for line in log)]
        self.assertEqual(len(delivered), 1)
        others = [log for log in logs.values() if log not in delivered]
        self.assertTrue(all(log in ([], ["another tick is running; delivered nothing"]) for log in others), others)
        self.assertEqual(store.load()[0]["state"], "delivered")

    def test_tick_during_a_send_does_not_fail_the_timer(self):
        # The second-order bug: an overlapping tick saw `sending` and marked a live send failed.
        store = tm.Timers(self.root / "t.json")
        store.add("R-001", "+1m", "x", "p", now=T0)
        started, go = threading.Event(), threading.Event()

        def blocking(target, text):
            started.set()
            go.wait(5)
            return "ok"
        first = threading.Thread(target=lambda: store.tick(blocking, {"R-001"}, now=T0 + timedelta(minutes=5)))
        first.start()
        self.assertTrue(started.wait(5))
        second = store.tick(lambda *a: "ok", {"R-001"}, now=T0 + timedelta(minutes=6))
        self.assertEqual(second, ["another tick is running; delivered nothing"])
        self.assertEqual(store.load()[0]["state"], "sending")
        go.set()
        first.join(5)
        self.assertEqual(store.load()[0]["state"], "delivered")

    def test_add_during_a_slow_tick_waits_and_lands(self):
        store = tm.Timers(self.root / "t.json")
        store.add("R-001", "+1m", "x", "p", now=T0)
        started, go = threading.Event(), threading.Event()

        def blocking(target, text):
            started.set()
            go.wait(5)
            return "ok"
        tick = threading.Thread(target=lambda: store.tick(blocking, {"R-001"}, now=T0 + timedelta(minutes=5)))
        tick.start()
        self.assertTrue(started.wait(5))
        add = threading.Thread(target=lambda: store.add("R-002", "+1m", "y", "p", now=T0))
        add.start()
        add.join(0.3)
        self.assertTrue(add.is_alive(), "add should wait for the tick's lock")
        go.set()
        tick.join(5)
        add.join(5)
        self.assertEqual(sorted((t["row"], t["state"]) for t in store.load()),
                         [("R-001", "delivered"), ("R-002", "armed")])

    def test_overlapping_ticks_across_processes_deliver_once(self):
        store = tm.Timers(self.root / "t.json")
        store.add("R-001", "+1m", "x", "p", now=T0)
        out = self.root / "sent.txt"
        code = ("import sys, time; sys.path.insert(0, sys.argv[1]); import timers as tm\n"
                "def send(target, text):\n"
                "    open(sys.argv[3], 'a').write(text + '\\n'); time.sleep(1.0); return 'ok'\n"
                "tm.Timers(sys.argv[2]).tick(send, {'R-001'})")
        self.assertEqual(processes(code, self.root / "t.json", out, n=4), [0] * 4)
        self.assertEqual(len(out.read_text().splitlines()), 1)
        self.assertEqual(store.load()[0]["state"], "delivered")


class ClaimConcurrencyTests(TempDir):
    def test_concurrent_claims_all_survive(self):
        claims = dg.Claims(self.root / "c.json")
        self.assertEqual(together(20, lambda i: claims.claim([f"R-{i:03d}"], f"s{i}", "worker-aaaaaa", T0)), {})
        self.assertEqual(len(claims.load()), 20)

    def test_concurrent_claims_across_processes(self):
        code = ("import sys; from datetime import datetime, timezone; sys.path.insert(0, sys.argv[1])\n"
                "import dispatch_guard as dg\n"
                "dg.Claims(sys.argv[2]).claim([f'R-{sys.argv[3]}'], f's{sys.argv[3]}', 'worker-aaaaaa',"
                " datetime.now(timezone.utc))")
        self.assertEqual(processes(code, self.root / "c.json"), [0] * 6)
        self.assertEqual(len(dg.Claims(self.root / "c.json").load()), 6)

    def test_two_sessions_claiming_one_row_get_one_claim(self):
        claims = dg.Claims(self.root / "c.json")
        errors = together(2, lambda i: claims.claim(["R-050", "ISSUE-50"], f"s{i}", "worker-aaaaaa", T0))
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(next(iter(errors.values())), dg.ClaimHeld)
        winner = claims.load()["R-050"]["session"]
        self.assertEqual(claims.load()["ISSUE-50"]["session"], winner)
        loser = "s1" if winner == "s0" else "s0"
        claims.claim(["R-050"], loser, "worker-bbbbbb", T0, override="owner asked for a second attempt")
        self.assertEqual(claims.load()["R-050"]["session"], loser)


class LeaseFenceTests(TempDir):
    """A primary that slept past its TTL wakes after a takeover: every write it attempts is refused."""

    def setUp(self):
        super().setUp()
        self.lease = lz.Lease(self.root / "state" / "lease.json")
        self.lease.init("coordinator-a1", "host-a", now=T0)
        self.old = self.lease.renew("coordinator-a1", now=T0)["epoch"]  # what the old primary holds
        # The old primary's laptop sleeps; after the TTL another session takes over.
        self.lease.take("coordinator-b2", "host-b", "a1 silent past TTL", now=T0 + timedelta(hours=3))
        self.new = self.lease.epoch()

    def test_epochs(self):
        self.assertEqual((self.old, self.new), (1, 2))
        self.assertFalse(self.lease.is_primary("coordinator-a1"))
        with self.assertRaises(lz.LeaseError):
            self.lease.renew("coordinator-a1")

    def test_ledger_refuses_the_stale_primary(self):
        led = ls.Ledger(self.root, fence=self.lease.fenced)
        row = led.write_row(None, next="new primary's row", expect_epoch=self.new)
        before = led.path.read_text()
        for attempt in (lambda: led.write_row(None, next="stale primary writes anyway", expect_epoch=self.old),
                        lambda: led.write_row(row["id"], next="stale edit", expect_epoch=self.old),
                        lambda: led.close_row(row["id"], "merged", "abc1234", "stale close", expect_epoch=self.old),
                        lambda: led.write_row(None, next="no epoch at all")):
            with self.assertRaises(ls.StaleEpoch):
                attempt()
        self.assertEqual(led.path.read_text(), before)
        self.assertEqual(list((self.root / "ledger").glob("closed-*.md")), [])

    def test_timers_refuse_the_stale_primary(self):
        store = tm.Timers(self.root / "state" / "timers.json", fence=self.lease.fenced)
        store.add("R-001", "+1m", "x", "p", now=T0, expect_epoch=self.new)
        with self.assertRaises(tm.StaleEpoch):
            store.add("R-001", "+1m", "stale", "p", now=T0, expect_epoch=self.old)
        sent = []
        with self.assertRaises(tm.StaleEpoch):
            store.tick(lambda target, text: sent.append(text), {"R-001"}, now=T0 + timedelta(hours=4),
                       expect_epoch=self.old)
        self.assertEqual((sent, [t["state"] for t in store.load()]), ([], ["armed"]))
        store.tick(lambda target, text: sent.append(text) or "ok", {"R-001"}, now=T0 + timedelta(hours=4),
                   expect_epoch=self.new)
        self.assertEqual((len(sent), store.load()[0]["state"]), (1, "delivered"))

    def test_takeover_during_a_send_records_nothing_and_stops_the_tick(self):
        # Issue #6: the replaced tick used to mark the send delivered after the takeover. The fence is released
        # for the send (so the take does not wait on it), and re-checked before the outcome is recorded.
        store = tm.Timers(self.root / "state" / "timers.json", fence=self.lease.fenced)
        store.add("R-001", "+1m", "a", "p", now=T0, expect_epoch=self.new)
        store.add("R-002", "+1m", "b", "p", now=T0, expect_epoch=self.new)
        sent = []

        def send_then_lose_the_lease(target, text):
            sent.append(text)
            self.lease.take("coordinator-c3", "host-c", "test", force=True)
            return "ok"
        with self.assertRaises(tm.StaleEpoch):
            store.tick(send_then_lose_the_lease, {"R-001", "R-002"}, now=T0 + timedelta(hours=4),
                       expect_epoch=self.new)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sorted(t["state"] for t in store.load()), ["armed", "sending"])
        # The new primary's tick surfaces the interrupted send as failed (never resent) and delivers the other.
        log = store.tick(lambda target, text: sent.append(text) or "ok", {"R-001", "R-002"},
                         now=T0 + timedelta(hours=4), expect_epoch=self.lease.epoch())
        self.assertEqual(len(sent), 2)
        self.assertEqual(sorted(t["state"] for t in store.load()), ["delivered", "failed"])
        self.assertTrue(any("crashed mid-send" in line for line in log), log)

    def test_wake_gate_refuses_a_stale_save(self):
        path = self.root / "state" / "wake-gate.json"
        sources = {"ledger": lambda: {"R-001": 1}}
        wg.run(sources, path, persist=True, now=T0, fence=self.lease.fenced, expect_epoch=self.new)
        before = path.read_text()
        with self.assertRaises(wg.StaleEpoch):
            wg.run({"ledger": lambda: {"R-001": 2}}, path, persist=True, now=T0 + timedelta(hours=4),
                   fence=self.lease.fenced, expect_epoch=self.old)
        self.assertEqual(path.read_text(), before)
        # A companion's look (persist=False) needs no epoch and saves nothing.
        self.assertTrue(wg.run({"ledger": lambda: {"R-001": 2}}, path, persist=False,
                               fence=self.lease.fenced).changed)
        self.assertEqual(json.loads(path.read_text()), json.loads(before))

    def test_fenced_caller_cannot_take_or_renew_from_inside_its_write(self):
        with self.lease.fenced():
            with self.assertRaises(lz.LeaseError):
                self.lease.renew("coordinator-b2")
        self.lease.renew("coordinator-b2")

    def test_simultaneous_takes_of_an_expired_lease_have_one_winner(self):
        later = T0 + timedelta(hours=8)
        errors = together(10, lambda i: self.lease.take(f"coordinator-x{i}", "host-x", "race", now=later))
        self.assertEqual(len(errors), 9)
        self.assertTrue(all(isinstance(e, lz.LeaseError) for e in errors.values()))
        self.assertEqual(self.lease.epoch(), self.new + 1)


class FenceInFlightTests(TempDir):
    """Issue #6, finding 1: a write that has passed the epoch check must finish before a take can move the epoch."""

    def setUp(self):
        super().setUp()
        self.lease = lz.Lease(self.root / "state" / "lease.json")
        self.lease.init("laptop", "host-a", now=T0)
        self.e1 = self.lease.epoch()

    def race(self, write, hold):
        """Start write() in a thread, stop it at hold (already past the fence), start a take, then release.

        Returns (take finished while the write was held, the write's result or exception)."""
        at_write, release, out = threading.Event(), threading.Event(), {}

        def gate():
            at_write.set()
            release.wait(10)
        hold(gate)

        def run():
            try:
                out["write"] = write()
            except Exception as error:
                out["write"] = error
        writer = threading.Thread(target=run)
        writer.start()
        self.assertTrue(at_write.wait(5))
        taker = threading.Thread(target=lambda: self.lease.take("new-primary", "host-b", "slept past the TTL",
                                                                force=True))
        taker.start()
        taker.join(0.5)
        took_early = not taker.is_alive()
        release.set()
        writer.join(10)
        taker.join(30)
        self.assertEqual(self.lease.epoch(), self.e1 + 1)
        return took_early, out["write"]

    def test_take_waits_for_a_ledger_write_in_flight(self):
        led = ls.Ledger(self.root, fence=self.lease.fenced)
        real = ls.atomic_write

        def hold(gate):
            def gated(path, text):
                gate()
                return real(path, text)
            ls.atomic_write = gated
        try:
            took_early, result = self.race(
                lambda: led.write_row(None, expect_epoch=self.e1, next="the sleeping laptop writes"), hold)
        finally:
            ls.atomic_write = real
        self.assertFalse(took_early, "take() moved the epoch while a fenced write was in flight")
        self.assertEqual(result["id"], "R-001")  # ordered before the take, so the new primary's first read has it
        with self.assertRaises(ls.StaleEpoch):
            led.write_row(None, expect_epoch=self.e1, next="after the take")
        self.assertEqual([r["id"] for r in led.rows()], ["R-001"])

    def test_take_waits_for_a_timer_add_in_flight(self):
        store = tm.Timers(self.root / "state" / "timers.json", fence=self.lease.fenced)
        real = store.save

        def hold(gate):
            def gated(timers):
                gate()
                return real(timers)
            store.save = gated
        took_early, result = self.race(lambda: store.add("R-001", "+1m", "a", "p", now=T0, expect_epoch=self.e1),
                                       hold)
        store.save = real
        self.assertFalse(took_early)
        self.assertEqual([t["id"] for t in store.load()], [result["id"]])
        with self.assertRaises(tm.StaleEpoch):
            store.add("R-001", "+1m", "b", "p", now=T0, expect_epoch=self.e1)

    def test_take_waits_for_a_wake_gate_save_in_flight(self):
        path = self.root / "state" / "wake-gate.json"
        sources = {"ledger": lambda: {"R-001": 1}}
        hold_box = {}

        def write():
            real = wg.tempfile.mkstemp

            def gated(*a, **k):  # stops inside the fenced save, after the epoch check
                hold_box["gate"]()
                return real(*a, **k)
            wg.tempfile = type("T", (), {"mkstemp": staticmethod(gated)})
            try:
                return wg.run(sources, path, persist=True, now=T0, fence=self.lease.fenced, expect_epoch=self.e1)
            finally:
                wg.tempfile = tempfile
        took_early, result = self.race(write, lambda gate: hold_box.__setitem__("gate", gate))
        self.assertFalse(took_early)
        self.assertTrue(result.changed and path.exists())
        with self.assertRaises(wg.StaleEpoch):
            wg.run(sources, path, persist=True, now=T0, fence=self.lease.fenced, expect_epoch=self.e1)

    def test_take_does_not_wait_for_a_slow_send(self):
        store = tm.Timers(self.root / "state" / "timers.json", fence=self.lease.fenced)
        store.add("R-001", "+1m", "a", "p", now=T0, expect_epoch=self.e1)

        def hold(gate):
            self.send = lambda target, text: gate() or "ok"
        took_early, result = self.race(
            lambda: store.tick(lambda t, x: self.send(t, x), {"R-001"}, now=T0 + timedelta(minutes=5),
                               expect_epoch=self.e1), hold)
        self.assertTrue(took_early, "a takeover must not wait on a send")
        self.assertIsInstance(result, tm.StaleEpoch)
        self.assertEqual(store.load()[0]["state"], "sending")

    def test_take_waits_for_a_fence_held_by_another_process(self):
        held, release = self.root / "held", self.root / "release"
        code = ("import sys, time, pathlib; sys.path.insert(0, sys.argv[1]); import lease as lz\n"
                "root = pathlib.Path(sys.argv[2])\n"
                "with lz.Lease(root / 'state/lease.json').fenced() as epoch:\n"
                "    (root / 'held').write_text(str(epoch))\n"
                "    while not (root / 'release').exists(): time.sleep(0.02)\n")
        proc = subprocess.Popen([sys.executable, "-c", code, HERE, str(self.root)])
        try:
            deadline = time.monotonic() + 10
            while not held.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(held.read_text(), str(self.e1))
            taker = threading.Thread(target=lambda: self.lease.take("new-primary", "host-b", "test", force=True))
            taker.start()
            taker.join(0.5)
            self.assertTrue(taker.is_alive(), "take() did not wait for another process's fenced write")
            self.assertEqual(self.lease.epoch(), self.e1)
            release.write_text("")
            taker.join(30)
            self.assertEqual(self.lease.epoch(), self.e1 + 1)
        finally:
            release.write_text("")
            self.assertEqual(proc.wait(timeout=30), 0)


class FenceSmallFindingsTests(TempDir):
    def test_expect_epoch_without_a_fence_is_an_error(self):
        # Issue #6, finding 2: it used to be ignored silently.
        led = ls.Ledger(self.root)
        with self.assertRaises(ValueError):
            led.write_row(None, expect_epoch=999, next="caller thinks it is fenced")
        with self.assertRaises(ValueError):
            led.close_row("R-001", "merged", "abc1234", "caller thinks it is fenced", expect_epoch=999)
        self.assertEqual(led.rows(), [])
        store = tm.Timers(self.root / "t.json")
        with self.assertRaises(ValueError):
            store.add("R-001", "+1m", "a", "p", expect_epoch=999)
        with self.assertRaises(ValueError):
            store.tick(lambda *a: "ok", set(), expect_epoch=999)
        with self.assertRaises(ValueError):
            wg.run({}, self.root / "wg.json", persist=True, expect_epoch=999)
        self.assertFalse((self.root / "wg.json").exists())
        # Unfenced single-session use still works without an epoch.
        self.assertEqual(led.write_row(None, next="ok")["id"], "R-001")

    def test_timer_ids_never_collide(self):
        # Issue #6, finding 3: ids are now drawn under the lock against the ids already in the file.
        store = tm.Timers(self.root / "t.json")
        real, draws = tm.secrets.token_hex, iter(["abcdef", "abcdef", "abcdef", "123456"])
        tm.secrets.token_hex = lambda n: next(draws)
        try:
            store.add("R-1", "+1m", "a", "p", now=T0)
            store.add("R-2", "+1m", "b", "q", now=T0)
        finally:
            tm.secrets.token_hex = real
        self.assertEqual([t["id"] for t in store.load()], ["t-abcdef", "t-123456"])


if __name__ == "__main__":
    unittest.main()
