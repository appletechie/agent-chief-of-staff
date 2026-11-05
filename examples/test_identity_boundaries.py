"""Critically: empty identities cannot be validly persisted, and there is no implicit primary at epoch zero."""
import json
import pathlib
import tempfile
import unittest
from datetime import datetime, timezone

import dispatch_guard as dg
import lease as lz
import timers as tm

T0=datetime(2026,1,16,14,0,tzinfo=timezone.utc)


class IdentityValidation(unittest.TestCase):
    def test_blank_primary_never_creates_or_steals_the_lease(self):
        path = pathlib.Path(tempfile.mkdtemp())/"lease.json"
        lease = lz.Lease(path)
        with self.assertRaises(lz.LeaseError): lease.init("", "h", now=T0)
        self.assertFalse(path.exists())
        lease.init("a", "h", now=T0)
        with self.assertRaises(lz.LeaseError): lease.take("  ", "h2", "handover", now=T0,force=True)
        self.assertFalse(path.exists() and not lease.is_primary("a"))

    def test_blank_session_or_worker_cannot_be_written(self):
        path = pathlib.Path(tempfile.mkdtemp())/"claim.json"
        for args in [("", "w-aaaaa"), ("session-x", " ")]:
            with self.assertRaises(ValueError): dg.Claims(path).claim(["R-001"], *args, now=T0)
        self.assertFalse(path.exists())

    def test_no_lease_record_means_the_scheduled_job_does_nothing(self):
        root = pathlib.Path(tempfile.mkdtemp()); lease = lz.Lease(root/"lease.json")
        logged=[]; result=lz.primary_gate(lease,"coordinator pass",log=logged.append)
        self.assertIsNone(result)
        store = tm.Timers(root/"timers.json", fence=lease.fenced)
        # The scheduled pass, not the raw fencing primitive, establishes whether to run: there is no lease to own here.
        self.assertIsNotNone(lease.epoch())  # a legacy lease lacking epoch still means 0; the absence itself gates jobs
        self.assertEqual(logged, ["coordinator pass: no primary lease is recorded; skipped (initialize the lease first)"])

    def test_numerical_codec_overload_maps_to_corruption(self):
        root = pathlib.Path(tempfile.mkdtemp()); path=root/"lease.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"primary":"p","renewed_at":"2026-01-16T14:00:00+00:00","ttl_s":' + "1"*4301 + '}')
        with self.assertRaises(lz.LeaseError):
            lz.Lease(path).epoch()
