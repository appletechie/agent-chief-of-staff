"""#34: each probe invokes the current API with the exact invalid field data, rather than relying on prose tests."""
import json
import pathlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
import dispatch_guard as dg
import lease as lz
import timers as tm
import wake_gate as wg

T0=datetime(2026,1,16,14,0,tzinfo=timezone.utc)


class BoundaryChecks(unittest.TestCase):
    def test_null_timestamp_is_a_pass_not_a_quiet_wake(self):
        path=pathlib.Path(tempfile.mkdtemp())/"wake-gate.json"
        path.write_text('{"last_changed_at":null,"parts":{}}')
        self.assertTrue(wg.run({},path,persist=False,now=T0).changed)

    def test_missing_host_cannot_be_written_then_refused_on_read(self):
        path=pathlib.Path(tempfile.mkdtemp())/"lease.json"
        lease=lz.Lease(path)
        lease.init("first","host-a",now=T0)
        with self.assertRaises(lz.LeaseError):
            lease.take("second",None,"deliberate handover",now=T0+timedelta(hours=3),force=True)
        self.assertEqual(lease.holder(),"first")

    def test_huge_lease_ttl_has_a_scoped_error(self):
        path=pathlib.Path(tempfile.mkdtemp())/"lease.json"
        path.write_text(json.dumps({"primary":"primary","host":"host-a","renewed_at":T0.isoformat(),"ttl_s":10**20}))

        with self.assertRaises(lz.LeaseError): lz.Lease(path).read()

    def test_counter_overflow_does_not_abort_a_neighbour_timer(self):
        path=pathlib.Path(tempfile.mkdtemp())/"timers.json"
        giant=dict(id="t-huge",row="R-001",name="giant",prompt="run",target="coordinator",
                   state="armed",due=T0.isoformat(),attempts=int("9"*4300))
        fine=dict(giant,id="t-next",row="R-002",attempts=0)
        path.write_text(json.dumps([giant,fine]))
        log=tm.Timers(path).tick(lambda a,b: (_ for _ in ()).throw(OSError('temporary destination failure')),
                                 {"R-001","R-002"},now=T0)

        self.assertTrue(any("unencodable counter" in line for line in log) or any("overflow" in line.lower() for line in log))
        self.assertTrue(any(failure["state"] == "failed" for failure in tm.Timers(path).load() if failure["id"] == "t-huge"))

    def test_the_write_of_an_unencodable_epoch_does_not_dispose_the_old_value(self):
        path=pathlib.Path(tempfile.mkdtemp())/"lease.json"
        lease=lz.Lease(path)
        lease.init("original","host-a",now=T0)
        raw=lease.read(); raw["epoch"]=int("9"*4300)
        path.write_text(json.dumps(raw))
        with self.assertRaises(lz.LeaseError):
            lease.take("replacement","host-b","not silently lose takeover state",now=T0,force=True)
        self.assertEqual(lz.Lease(path).holder(),"original")


if __name__=="__main__":
    unittest.main()
