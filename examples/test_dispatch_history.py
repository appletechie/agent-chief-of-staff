"""dispatch.json catches up on a live guard when claims expired or never existed."""
import pathlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import dispatch_guard as dg

T0 = datetime(2026, 1, 16, 14, 0, tzinfo=timezone.utc)


class TestDispatchHistory(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.claims = dg.Claims(self.root / "claims.json")

    def sources(self, dispatches):
        return dg.Sources(open_prs_naming=lambda k: [], assignee=lambda k: None, live_workers=lambda: [],
                          recent_dispatches=lambda key: dispatches)

    def test_recent_dispatch_from_another_session_refuses(self):
        rec = [{"key": "ISSUE-1", "session": "other", "at": (T0 - timedelta(hours=1)).isoformat()}]
        v = dg.guard("ISSUE-1", "R-001", "me", self.sources(rec), self.claims, now=T0)
        self.assertFalse(v.allowed)
        self.assertIn("recent dispatch", " ".join(v.refusals))

    def test_my_recent_dispatch_never_blocks_me(self):
        rec = [{"key": "ISSUE-1", "session": "me", "at": (T0 - timedelta(hours=1)).isoformat()}]
        v = dg.guard("ISSUE-1", "R-001", "me", self.sources(rec), self.claims, now=T0)
        self.assertTrue(v.allowed)

    def test_expired_dispatch_no_longer_blocks(self):
        rec = [{"key": "ISSUE-1", "session": "other", "at": (T0 - timedelta(hours=7)).isoformat()}]
        v = dg.guard("ISSUE-1", "R-001", "me", self.sources(rec), self.claims, now=T0)
        self.assertTrue(v.allowed)

    def test_dispatch_history_error_is_a_warning(self):
        def bad(key): raise RuntimeError("dispatch gone")
        sources = dg.Sources(open_prs_naming=lambda k: [], assignee=lambda k: None, live_workers=lambda: [],
                             recent_dispatches=bad)
        v = dg.guard("ISSUE-1", "R-001", "me", sources, self.claims, now=T0)
        self.assertTrue(v.allowed)
        self.assertTrue(any("recent dispatch" in w for w in v.warnings))


if __name__ == "__main__":
    unittest.main()
