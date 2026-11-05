"""Evidence proof must survive close_row (a pipeline is valid command text, not a column separator)."""
import pathlib
import tempfile
import unittest
from datetime import datetime, timezone

import ledger_store as ls

T0 = datetime(2026, 1, 16, 14, 0, tzinfo=timezone.utc)


class ProofPreservation(unittest.TestCase):
    def test_full_proof_goes_to_notes(self):
        root = pathlib.Path(tempfile.mkdtemp())
        led = ls.Ledger(root)
        row = led.write_row(None, next="fix")
        proof = "exit 0: `cargo test | tail -1` -> 1171 passed"
        led.close_row(row["id"], "tests", proof, "suite run on the head commit", now=T0)
        self.assertIn(proof, (root / "ledger" / "notes" / f"{row['id']}.md").read_text())


if __name__ == "__main__":
    unittest.main()
