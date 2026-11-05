"""Colliding ids must not lock the whole timer-store loop; redraw is bounded by a constant, not the store size."""
import pathlib
import tempfile
import unittest
from datetime import datetime, timezone

import timers as tm

T0 = datetime(2026, 1, 16, 14, 0, tzinfo=timezone.utc)


class IdRedrawTests(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.original = tm.secrets.token_hex

    def tearDown(self):
        tm.secrets.token_hex = self.original

    def test_collision_gets_a_second_attempt(self):
        ids = iter(["same", "same", "different"])
        tm.secrets.token_hex = lambda n: next(ids)
        store = tm.Timers(self.root / "timers.json")
        store.add("R-001", "+1m", "x", "p", now=T0)
        second = store.add("R-002", "+1m", "y", "q", now=T0)
        self.assertEqual(second["id"], "t-different")
        self.assertEqual(len(store.load()), 2)

    def test_constant_generator_raises_rather_than_spinning(self):
        tm.secrets.token_hex = lambda n: "always"
        store = tm.Timers(self.root / "timers.json")
        store.add("R-001", "+1m", "x", "p", now=T0)
        with self.assertRaises(RuntimeError) as error:
            store.add("R-002", "+1m", "y", "q", now=T0)
        self.assertIn("10 attempts", str(error.exception))
        # the failed add did not touch the store
        self.assertEqual(len(store.load()), 1)


if __name__ == "__main__":
    unittest.main()
