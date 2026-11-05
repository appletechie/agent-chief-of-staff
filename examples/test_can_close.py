"""The close hook ties row/proof to outside facts (PR state, review receipt) instead of free text always closing."""
import pathlib
import tempfile
import unittest
from datetime import datetime, timezone

import ledger_store as ls

T0 = datetime(2026, 1, 16, 14, 0, tzinfo=timezone.utc)


class CanCloseHookTests(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())

    def test_policy_can_refuse(self):
        self.state = {"merged": False, "review_clean": True, "last_push": 1}

        def policy(row, proof):
            if not self.state["merged"]:
                return f"PR for {row['key']} is not merged"
            return None

        led = ls.Ledger(self.root, can_close=policy)
        row = led.write_row(None, key="ISSUE-1", next="fix it")
        with self.assertRaises(ls.CloseRefused):
            led.close_row(row["id"], "ci", "run 4198283941 completed/success", "CI green")
        self.assertEqual(len(led.rows()), 1)
        self.assertFalse(list(self.root.glob("ledger/closed-*.md")))
        self.state["merged"] = True
        led.close_row(row["id"], "ci", "run 4198283941 completed/success", "CI green")
        self.assertEqual(len(led.rows()), 0)

    def test_no_policy_still_needs_structured_proof(self):
        led = ls.Ledger(self.root)
        row = led.write_row(None, next="fix it")
        with self.assertRaises(ls.ProofRefused):
            led.close_row(row["id"], "tests", "verified", "the worker said it works")
        self.assertEqual(len(led.rows()), 1)
        led.close_row(row["id"], "merged", "89f09d6", "fix merged")
        self.assertEqual(len(led.rows()), 0)


if __name__ == "__main__":
    unittest.main()
