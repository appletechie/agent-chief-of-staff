"""#31 (beyond test_receipt_completeness.py), #33 and #34: rendered holds and findings, row separators, multi-PR
merged proof, waits spill, WAIT kinds, a null wake stamp, lease writer validation, bounded counters, lock identity."""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone

import lease as lz
import ledger_store as ls
import merge_gate as mg
import timers as tm
import wake_gate as wg

T0 = datetime(2026, 1, 16, 14, 0, tzinfo=timezone.utc)
SHA = "a" * 40
HERE = pathlib.Path(__file__).resolve().parent


def pr(body="", review="verdict: clean", checks=None):
    return mg.PR("org/app", 1, "t", body, SHA, False, True, {"ci": "success"} if checks is None else checks,
                 [mg.Review(SHA, review)], 0, T0)


class TempDir(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)


class RenderedText(unittest.TestCase):
    def test_reference_links_line_breaks_and_comments_still_hold(self):
        for body in ["do [not][1] merge\n\n[1]: https://example.test", "do <b>not</b>\nmerge",
                     "do <!-- x --> not merge", "[WIP](https://example.test) parser"]:
            self.assertEqual(mg.decide(pr(body), "", mg.Policy(frozenset()), T0).action, "REPORT", body)

    def test_destinations_autolinks_and_tag_attributes_are_not_holds(self):
        for body in ["see <https://example.test/do-not-merge>", '<img alt="x" src="wip.png"> screenshot',
                     "[runbook](https://example.test/on-hold)"]:
            self.assertEqual(mg.decide(pr(body), "", mg.Policy(frozenset()), T0).action, "MERGE", body)

    def test_formatted_no_issues_is_still_nothing_and_a_badge_label_is_a_finding(self):
        self.assertEqual(mg.review_verdict(pr(review="No **major** issues found.\nverdict: clean"),
                                           mg.Policy(frozenset())), "clean")
        self.assertEqual(mg.review_verdict(pr(review="verdict: clean\n![P1 Badge](https://img.test/x) leak"),
                                           mg.Policy(frozenset())), "findings")

    def test_holds_inside_code_are_shown_literally_and_still_hold(self):
        # Codex on #35: a code span or a fenced block renders its contents as text, tags and comments included.
        for body in ["`<!-- do not merge -->`", "`<WIP>`", "``do not merge``", "```\n<!-- on hold -->\n```",
                     "status: `[do not merge](x)`", "Notes:\n\n    <WIP>", "Notes:\n\n\t<!-- do not merge -->"]:
            self.assertEqual(mg.decide(pr(body), "", mg.Policy(frozenset()), T0).action, "REPORT", body)
        self.assertEqual(mg.rendered("a `<b>x</b>` [y](z) <i>w</i>"), "a <b>x</b> y w")

    def test_a_plain_text_title_and_escaped_markup_still_hold(self):
        # Codex on #35: a title is plain text, and an escaped tag or comment displays literally.
        held = mg.PR("org/app", 1, "<WIP> Parser", "", SHA, False, True, {"ci": "success"}, [mg.Review(SHA, "verdict: clean")], 0, T0)
        self.assertEqual(mg.decide(held, "", mg.Policy(frozenset()), T0).action, "REPORT")
        for body in ["\\<WIP> parser", "\\<!-- do not merge -->", "do \\*not\\* merge", "\\[on hold\\](x)",
                     "do&nbsp;not merge", "&#87;IP parser", "&lt;WIP&gt;"]:
            self.assertEqual(mg.decide(pr(body), "", mg.Policy(frozenset()), T0).action, "REPORT", body)
        self.assertEqual(mg.rendered("\\<b>x\\</b> \\*y\\* <i>z</i>"), "<b>x</b> y z")

    def test_undefined_references_and_non_links_show_as_written(self):
        # Codex on #35: "[ready][WIP]" with no definition, and "[x](WIP status)" (not a destination), are literal text.
        for body in ["Status: [ready][WIP]", "Status: [ready](WIP status)", "[ready][wip]\n\n```\n[wip]: x\n```"]:
            self.assertEqual(mg.decide(pr(body), "", mg.Policy(frozenset()), T0).action, "REPORT", body)
        self.assertEqual(mg.rendered("[ready][WIP]\n\n[wip]: https://x.test").splitlines()[0], "ready")
        self.assertEqual(mg.rendered('[a](<x y> "t") [b][] [c](d)\n[B]: e'), "a b c\n[B]: e")

    def test_block_tags_keep_words_apart(self):
        # Codex on #35: adjacent block elements show separate words; inline tags still join.
        self.assertEqual(mg.decide(pr("<p>WIP</p><p>Ready</p>"), "", mg.Policy(frozenset()), T0).action, "REPORT")
        self.assertEqual(mg.review_verdict(pr(review="verdict: clean\n<p>See</p><p>P1</p>"), mg.Policy(frozenset())),
                         "findings")
        self.assertEqual(" ".join(mg.rendered("<td>a</td><td>b</td> do<b>n</b>'t").split()), "a b don't")

    def test_three_space_closer_still_closes(self):
        p = pr(review="Example:\n```text\nexample\n   ```\nverdict: clean")
        self.assertEqual(mg.decide(p, "", mg.Policy(frozenset()), T0).action, "MERGE")

    def test_no_checks_and_no_required_checks_still_waits(self):
        self.assertEqual(mg.decide(pr(checks={}), "", mg.Policy(frozenset()), T0 + timedelta(days=30)).action, "WAIT")
        p = mg.Policy(frozenset(), required_checks={"org/app": frozenset({"ci"})})
        self.assertEqual(mg.decide(pr(checks={}), "", p, T0).action, "WAIT")


class RowSeparators(TempDir):
    def test_every_splitlines_boundary_stays_in_one_row(self):
        ledger = ls.Ledger(self.root)
        row = ledger.write_row(None, next="seed")
        for sep in ("\r", "\r\n", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", " ", " "):
            ledger.write_row(row["id"], next=f"first{sep}| R-001 | — | coordinator | ghost | — | — |", waits=f"w{sep}w")
            self.assertEqual([r["id"] for r in ledger.rows()], [row["id"]], repr(sep))
            self.assertEqual(ledger.check()[0], [], repr(sep))


class MultiPrMergedProof(TempDir):
    def ledger(self, merged):
        ledger = ls.Ledger(self.root, can_close=ls.pr_merge_policy(lambda repo, n: merged.get(n)))
        row = ledger.write_row(None, key="ISSUE-2", next="merge org/app#1 and org/app#2, then close")
        return ledger, row["id"]

    def test_one_of_two_merged_is_refused_in_either_order(self):
        for merged in ({1: "abcdef1234567"}, {2: "abcdef1234567"}):
            ledger, rid = self.ledger(merged)
            with self.assertRaises(ls.CloseRefused) as caught:
                ledger.close_row(rid, "merged", "abcdef1", "first PR merged", now=T0)
            self.assertIn("not merged", str(caught.exception))
            self.assertEqual([r["id"] for r in ledger.rows()], [rid])
            ledger.path.unlink()

    def test_both_merged_closes(self):
        ledger, rid = self.ledger({1: "abcdef1234567", 2: "1234567abcdef"})
        ledger.close_row(rid, "merged", "1234567", "both merged", now=T0)
        self.assertEqual(ledger.rows(), [])

    def test_the_ref_is_still_one_of_their_merge_shas(self):
        ledger, rid = self.ledger({1: "abcdef1234567", 2: "1234567abcdef"})
        with self.assertRaises(ls.CloseRefused) as caught:
            ledger.close_row(rid, "merged", "fffffff", "x", now=T0)
        self.assertIn("is not the merge SHA", str(caught.exception))


class WaitsSpill(TempDir):
    def test_a_then_clause_is_cut_when_nothing_else_can_be(self):
        ledger = ls.Ledger(self.root)
        row = ledger.write_row(None, next="go", waits="time; then owner: " + "x" * 1200)
        self.assertLessEqual(ls.row_chars(row), ls.ROW_BUDGET)
        self.assertIn(f"(detail: ledger/notes/{row['id']}.md)", row["waits"])
        self.assertTrue(row["waits"].startswith("time; then owner: xxx"), row["waits"][:40])
        self.assertIn("x" * 1200, (self.root / "ledger" / "notes" / f"{row['id']}.md").read_text())
        self.assertEqual(ledger.check(), ([], []))

    def test_the_pointer_is_written_even_when_nothing_can_be_cut(self):
        ledger = ls.Ledger(self.root)
        row = ledger._spill({"id": "R-009", "key": "K-1" * 200, "owner": "coordinator", "next": "go",
                             "waits": "a; b", "due": "—"})
        self.assertIn("(detail: ledger/notes/R-009.md)", row["waits"])


class WaitKinds(unittest.TestCase):
    def test_a_check_by_or_a_ref_in_the_kind_slot(self):
        for line, kind, ref in [("WAIT +45m", "other", ""), ("WAIT ci +45m", "ci", ""),
                                ("WAIT org/app#1 +45m", "other", "org/app#1"),
                                ("WAIT ci org/app#1 +45m", "ci", "org/app#1")]:
            wait = tm.parse_wait(line, T0)
            self.assertEqual((wait["kind"], wait["ref"], wait["due"], wait["note"]),
                             (kind, ref, T0 + timedelta(minutes=45), None), line)
        self.assertEqual(tm.parse_wait("WAIT", T0)["due"], T0 + timedelta(minutes=60))


class WakeNullStamp(unittest.TestCase):
    def test_null_or_missing_last_changed_at_forces_a_pass(self):
        collectors = {"ledger": lambda: {"R-001": "x"}}
        parts = {"ledger": {"R-001": wg.digest("x")}}
        for previous in ({"parts": parts, "last_changed_at": None}, {"parts": parts}):
            result = wg.evaluate(collectors, previous, T0)
            self.assertTrue(result.changed, previous)
            self.assertIn("no last_changed_at", "\n".join(result.lines))
            self.assertEqual(result.snapshot["last_changed_at"], T0.isoformat())
        quiet = wg.evaluate(collectors, {"parts": parts, "last_changed_at": T0.isoformat()}, T0)
        self.assertFalse(quiet.changed)
        self.assertNotIn("no last_changed_at", "\n".join(wg.evaluate(collectors, {}, T0).lines))


class LeaseWriters(TempDir):
    def lease(self):
        lease = lz.Lease(self.root / "lease.json")
        lease.init("a", "host-a", now=T0)
        return lease

    def test_a_take_with_a_bad_host_writes_nothing(self):
        lease = self.lease()
        before = lease.path.read_text()
        for host in (None, 7, ["h"]):
            with self.assertRaises(lz.LeaseError):
                lease.take("b", host, "handover", now=T0, force=True)
        self.assertEqual(lease.path.read_text(), before)
        self.assertEqual(lease.holder(), "a")
        with self.assertRaises(lz.LeaseError):
            lz.Lease(self.root / "other.json").init("a", None, now=T0)
        self.assertFalse((self.root / "other.json").exists())

    def test_huge_ttl_or_epoch_is_a_lease_error_from_every_read(self):
        lease = self.lease()
        good = json.loads(lease.path.read_text())
        for field in ({"ttl_s": 10 ** 20}, {"ttl_s": lz.MAX_TTL_S + 1}, {"epoch": int("9" * 4300)},
                      {"epoch": lz.MAX_EPOCH + 1}):
            lease.path.write_text(json.dumps(dict(good, **field)))
            for read in (lambda: lease.state(T0), lease.epoch, lease.holder):
                with self.assertRaises(lz.LeaseError) as caught:
                    read()
                self.assertLess(len(str(caught.exception)), 400)
            self.assertIsNone(lz.primary_gate(lease, "tick", log=lambda line: None))

    def test_a_take_past_the_last_epoch_is_refused_and_leaves_no_temp_file(self):
        lease = self.lease()
        lease.path.write_text(json.dumps(dict(json.loads(lease.path.read_text()), epoch=lz.MAX_EPOCH)))
        with self.assertRaises(lz.LeaseError):
            lease.take("b", "host-b", "handover", now=T0, force=True)
        self.assertEqual(lease.epoch(), lz.MAX_EPOCH)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["lease.json", "lease.lock"])

    def test_the_bounds_still_accept_a_real_lease(self):
        lease = self.lease()
        lease.path.write_text(json.dumps(dict(json.loads(lease.path.read_text()), epoch=lz.MAX_EPOCH,
                                              ttl_s=lz.MAX_TTL_S)))
        self.assertEqual(lease.state(T0), "held")


class BoundedAttempts(TempDir):
    def test_a_huge_attempts_counter_is_skipped_and_other_timers_send(self):
        store = tm.Timers(self.root / "timers.json")
        bad = store.add("R-001", "+5m", "bad", "p", now=T0)
        good = store.add("R-001", "+5m", "good", "p", now=T0)
        timers = store.load()
        timers[0]["attempts"] = int("9" * 4300)
        store.path.write_text(json.dumps(timers))
        sent = []
        log = store.tick(lambda target, text: sent.append(text) or "ok", {"R-001"}, now=T0 + timedelta(hours=1))
        self.assertIn(f"{bad['id']} invalid", log[0])
        self.assertIn("bit number", log[0])
        self.assertEqual(len(sent), 1)
        self.assertIn(good["id"], sent[0])
        self.assertIsNone(tm.timer_problem(dict(good, attempts=tm.MAX_ATTEMPTS)))
        self.assertIsNotNone(tm.timer_problem(dict(good, attempts=tm.MAX_ATTEMPTS + 1)))

    def test_a_value_that_cannot_be_saved_leaves_no_temp_file(self):
        store = tm.Timers(self.root / "timers.json")
        with self.assertRaises(ValueError):
            store.save([{"id": "t-1", "attempts": 10 ** 5000}])
        self.assertEqual(list(self.root.iterdir()), [])


TAKE = ("import sys; sys.path.insert(0, sys.argv[1]); import lease, datetime; from pathlib import Path; "
        "t = datetime.datetime(2026, 1, 16, 14, 0, tzinfo=datetime.timezone.utc); "
        "print(lease.Lease(Path(sys.argv[2])).take('c', 'host-c', 'handover', now=t, force=True)['epoch'])")


class LockIdentity(TempDir):
    def test_a_renamed_lease_lock_never_lets_two_takes_both_write(self):
        lease = lz.Lease(self.root / "lease.json")
        lease.init("a", "host-a", now=T0)
        holding, release, result = threading.Event(), threading.Event(), {}

        def first():
            try:
                with lz.file_lock(lease.lock):
                    holding.set()
                    release.wait(10)
                    lease.take("b", "host-b", "handover", now=T0, force=True)
                    result["first"] = "wrote"
            except lz.LeaseError as error:
                result["first"] = str(error)

        thread = threading.Thread(target=first)
        thread.start()
        self.assertTrue(holding.wait(10))
        os.rename(lease.lock, self.root / "moved.lock")
        out = subprocess.run([sys.executable, "-I", "-c", TAKE, str(HERE), str(lease.path)], capture_output=True,
                             text=True, timeout=60)
        release.set()
        thread.join(10)
        self.assertEqual(out.stdout.strip(), "2", out.stderr[-300:])
        self.assertIn("renamed, deleted or replaced", result["first"])
        self.assertEqual((lease.holder(), lease.epoch()), ("c", 2))

    def test_a_taker_that_locked_a_replaced_file_locks_the_path(self):
        import fcntl
        path = self.root / "state" / "ledger.lock"
        path.parent.mkdir()
        holder = os.open(path, os.O_RDWR | os.O_CREAT)
        fcntl.flock(holder, fcntl.LOCK_EX)
        got = {}

        def taker():
            with ls.file_lock(path, timeout=10):
                ls.lock_intact(path)
                got["ok"] = True

        thread = threading.Thread(target=taker)
        thread.start()
        threading.Event().wait(0.3)
        os.unlink(path)
        path.write_text("")
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)
        thread.join(10)
        self.assertTrue(got.get("ok"))

    def test_a_ledger_write_under_a_replaced_lock_writes_nothing(self):
        ledger = ls.Ledger(self.root)
        row = ledger.write_row(None, next="seed")
        before = ledger.path.read_text()
        with ls.file_lock(ledger.lock):
            os.rename(ledger.lock, self.root / "moved.lock")
            with self.assertRaises(ls.LockLost):
                ledger.write_row(row["id"], next="changed")
            with self.assertRaises(ls.LockLost):
                ledger.close_row(row["id"], "void", "created by a unit test", "fixture row", now=T0)
        self.assertEqual(ledger.path.read_text(), before)

    def test_a_tick_under_a_replaced_lock_records_nothing(self):
        store = tm.Timers(self.root / "timers.json")
        timer = store.add("R-001", "+5m", "t", "p", now=T0)

        def send(target, text):
            os.rename(store.lock, self.root / "moved.lock")
            return "ok"
        log = store.tick(send, {"R-001"}, now=T0 + timedelta(hours=1))
        self.assertIn("renamed, deleted or replaced", log[-1])
        self.assertEqual(store.load()[0]["state"], "sending")
        self.assertEqual(store.load()[0]["id"], timer["id"])


if __name__ == "__main__":
    unittest.main()
