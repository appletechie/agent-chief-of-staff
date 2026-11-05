#!/usr/bin/env python3
"""Broad sweep: property checks over randomised inputs, on top of claims.py's fixed probes.

claims.py is specific and targeted (one assertion per documented behaviour). This is the wide pass: random
ledgers, timer lines and PRs, asserting invariants that must hold for *any* input, not just the ones I thought of.
A failure here that is not already in claims.py is a new finding.
"""
import json, random, re, sys, tempfile
sys.path.insert(0, 'examples')
from pathlib import Path
from datetime import datetime, timezone, timedelta
import ledger_store as ls, timers as tm, merge_gate as mg, wake_gate as wg, dispatch_guard as dg
import lease as lz
T0 = datetime(2026,1,16,14,0,tzinfo=timezone.utc)
FAILS = []
STATS = {}
SEEN = set()
def bump(k, n=1): STATS[k] = STATS.get(k,0)+n
def bad(what, evidence):
    key = (what, re.sub(r"[0-9]+","N",str(evidence))[:60])
    if key not in SEEN:
        SEEN.add(key)
        print(f"  FAIL {what}\n       {evidence}")
    FAILS.append((what, evidence))   # collate all instances, even where we only print one
def not_vacuous(what, n):
    if n == 0: bad("vacuous sweep: never reached the interesting branch", what)
    else: bump(f"non-vacuous: {what}")

def sweep_ledger(rng, n=150):
    root = Path(tempfile.mkdtemp()); led = ls.Ledger(root); ids = []
    words = ["fix","retry","loop","deploy","check","owner","worker","cache","soak","invoice"]
    for i in range(n):
        next_ = " ".join(rng.choice(words) for _ in range(rng.randint(1,120)))
        waits = rng.choice(["—","worker","owner: decision","owner: hands","time","vendor contact",
                            "worker; then owner","owner: decision; then worker", "time; then owner"])
        owner = rng.choice(["coordinator","owner","worker-7f3a2c","coordinator (owner closed: 7f3a2c)"])
        try:
            row = led.write_row(None, owner=owner, next=next_, waits=waits)
        except ValueError:
            continue
        ids.append(row["id"]); bump("rows written")
        if "(detail:" in row["next"] or "(detail:" in row["waits"]: bump("rows spilled")
        if ls.row_chars(row) > ls.ROW_BUDGET:
            bad("a written row exceeds ROW_BUDGET", f"{row['id']} {ls.row_chars(row)} chars")
        if ls.queue_of(row) not in ls.QUEUES:
            bad("queue_of returned an unknown queue", f"{row['id']} waits={waits!r} -> {ls.queue_of(row)}")
        if not row["next"].strip():
            bad("a written row has an empty next action", str(row))
    errs, _ = led.check()
    if errs: bad("check() errors on rows written through write_row", str(errs[:3]))
    if len(ids) != len(set(ids)): bad("write_row reused an id", f"{len(ids)} rows, {len(set(ids))} unique")
    victim = ids[0]; led.close_row(victim, proof="proof")
    new = led.write_row(None, next="after a close")
    if new["id"] == victim: bad("an id was reused after close", victim)

def sweep_parsers(rng, n=800):
    alphabet = "0123456789+-:ZT whm_cirevdeploypersonother/.#"
    for _ in range(n):
        s = "".join(rng.choice(alphabet) for _ in range(rng.randint(0,22)))
        bump("parser inputs")
        try: tm.parse_due(s, T0)
        except Exception as e: bad("parse_due raised", f"{s!r} -> {type(e).__name__}: {e}")
        try: tm.parse_wait("WAIT " + s, T0)
        except Exception as e: bad("parse_wait raised", f"{s!r} -> {type(e).__name__}: {e}")

def sweep_merge_gate(rng, n=40):
    """Start from a fully green PR and perturb exactly one thing, with the action the docs require.

    A uniform random PR almost never reaches MERGE, so a uniform sweep would assert the interesting
    invariants zero times. Perturbing one field from a known-good baseline keeps every branch reachable.
    """
    pol = mg.Policy(deploys_on_merge=frozenset({"org/site"}),
                    required_checks={"org/app": frozenset({"ci","e2e"})})
    def base(**kw):
        b = dict(repo="org/app", number=1, title="fix the retry loop", body="", head_sha="a"*40,
                 is_draft=False, mergeable=True,
                 checks={"ci":"success","e2e":"success","lint":"success"},
                 reviews=[mg.Review("a"*40,"No major issues.")], unresolved_threads=0, last_push_at=T0)
        b.update(kw); return mg.PR(**b)
    CASES = [
        ("nothing perturbed", dict(), "MERGE"),
        ("a hold marker on the row", dict(_row="HOLD: owner review"), "REPORT"),
        ("a hold marker in the title", dict(title="Draft: merge only after sign-off"), "REPORT"),
        ("a hold marker in the body", dict(body="do not merge yet"), "REPORT"),
        ("a draft with merge_drafts off", dict(is_draft=True), "REPORT"),
        ("mergeability not yet computed", dict(mergeable=None), "WAIT"),
        ("a merge conflict", dict(mergeable=False), "REPORT"),
        ("ci still running", dict(checks={"ci":"in_progress","e2e":"success","lint":"success"}), "WAIT"),
        ("ci failed", dict(checks={"ci":"failure","e2e":"success","lint":"success"}), "REPORT"),
        ("ci cancelled", dict(checks={"ci":"cancelled","e2e":"success","lint":"success"}), "REPORT"),
        ("ci timed out", dict(checks={"ci":"timed_out","e2e":"success","lint":"success"}), "REPORT"),
        ("an optional check failed", dict(checks={"ci":"success","e2e":"success","lint":"failure"}), "REPORT"),
        ("an optional check skipped", dict(checks={"ci":"success","e2e":"success","lint":"skipped"}), "MERGE"),
        ("an optional check neutral", dict(checks={"ci":"success","e2e":"success","lint":"neutral"}), "MERGE"),
        ("a required check skipped", dict(checks={"ci":"success","e2e":"skipped","lint":"success"}), "REPORT"),
        ("a required check neutral", dict(checks={"ci":"success","e2e":"neutral","lint":"success"}), "MERGE"),
        ("a required check never reported", dict(checks={"ci":"success","lint":"success"}), "WAIT"),
        ("a receipt with findings", dict(reviews=[mg.Review("a"*40,"verdict: findings")]), "REPORT"),
        ("a receipt that cannot be parsed", dict(reviews=[mg.Review("a"*40,"Summary: refactors the loop")]), "REPORT"),
        ("a clean phrase over a real finding", dict(reviews=[mg.Review("a"*40,"Looks good to merge. One P1 bug in the retry loop.")]), "REPORT"),
        ("a receipt for an older commit", dict(reviews=[mg.Review("0ld5ha0","No major issues.")]), "REQUEST_REVIEW"),
        ("unresolved review threads", dict(unresolved_threads=2), "REPORT"),
        ("a repo that deploys on merge", dict(repo="org/site"), "REPORT"),
    ]
    for label, kw, expect in CASES:
        kw = dict(kw); row = kw.pop("_row", "fix it")
        for now in (T0, T0 + timedelta(hours=2), T0 + timedelta(days=3)):
            for trial in range(n // len(CASES) + 1):
                got = mg.decide(base(**kw), row, pol, now)
                bump(f"decide: {got.action}")
                if got.action != expect:
                    # a missing required check escalates from WAIT to REPORT once check_wait passes
                    if expect == "WAIT" and got.action == "REPORT" and now > T0 + timedelta(minutes=30):
                        bump("WAIT->REPORT escalation after check_wait"); continue
                    if expect == "REQUEST_REVIEW" and got.action == "WAIT" and now <= T0 + timedelta(minutes=45):
                        bump("REQUEST_REVIEW not yet due"); continue
                    bad(f"merge gate: {label}", f"expected {expect}, got {got.action} ({got.reason}) at {now}")
                else:
                    bump("oracle hit")


def sweep_timers(rng, n=90):
    """Property: across many ticks, no timer is ever delivered twice, and none is left mid-send."""
    root = Path(tempfile.mkdtemp()); st = tm.Timers(root/"timers.json")
    ids = []
    count = rng.randint(3, 12)
    for i in range(count):
        due = rng.choice(["+1m","+5m","+1h","garbage","","-5m"])
        t = st.add(f"R-{i:03d}", due, f"timer {i}", f"prompt for {i}", now=T0)
        ids.append(t["id"])
    all_rows = [f"R-{i:03d}" for i in range(count)]
    closed_rows = {r for r in all_rows if rng.random() < 0.15}
    open_rows = set(all_rows) - closed_rows
    delivered = {}
    def send(target, text):
        bump("timer sends")
        if rng.random() < 0.15: raise ConnectionError("down")
        for tid in ids:
            if f"[timer {tid} " in text:
                delivered[tid] = delivered.get(tid, 0) + 1
        return "ok"
    now = T0
    for _ in range(n):
        now = now + timedelta(minutes=rng.choice([1, 5, 30, 240]))
        log = st.tick(send, open_rows, now)
        states = [t["state"] for t in st.load()]
        if "sending" in states:
            bad("a tick finished with a timer still in sending", f"at {now}: {states}")
        if any(v > 1 for v in delivered.values()):
            bad("a timer was delivered more than once", str({k:v for k,v in delivered.items() if v>1}))
    not_vacuous("timer deliveries", sum(delivered.values()))
    for t in st.load():
        if t["row"] in closed_rows and t["state"] == "delivered":
            bad("a timer on a closed row was delivered", f"{t['id']} row={t['row']} state={t['state']}")
    lost = [t for t in st.load() if t["state"] == "armed" and datetime.fromisoformat(t["due"]) < now - timedelta(hours=4)]
    if lost: bad("a due timer stayed armed well past its due time", str([(t["id"],t["state"],t["due"]) for t in lost]))

def sweep_wake_gate(rng, n=120):
    """Property: unreadable always forces a pass; a saved snapshot is never in the future; a companion never writes."""
    root = Path(tempfile.mkdtemp())
    for i in range(n):
        p = root/f"wg{i}.json"
        mode = rng.choice(["fresh","present","future","garbage","unreadable"])
        if mode == "present":
            p.write_text(json.dumps({"parts":{},"checked_at":T0.isoformat(),"last_changed_at":T0.isoformat()}))
        elif mode == "future":
            p.write_text(json.dumps({"parts":{},"checked_at":T0.isoformat(),
                                     "last_changed_at":(T0+timedelta(days=rng.randint(1,30))).isoformat()}))
        elif mode == "garbage":
            p.write_text("{not json")
        val = rng.random()
        if mode == "unreadable":
            coll = {"x": (lambda: (_ for _ in ()).throw(OSError("locked")))}
        else:
            coll = {"x": (lambda v=val: {"a": v})}   # stable per iteration, so a second run can be UNCHANGED
        persist = rng.random() < 0.7
        nows = [T0+timedelta(minutes=rng.choice([5,60,600]))]
        try:
            res = wg.run(coll, p, persist=persist, now=nows[0])
        except json.JSONDecodeError:
            bad("wake gate raised on a garbage snapshot", str(p))
            continue
        except Exception as e:
            bad("wake gate raised", f"{mode} -> {type(e).__name__}: {e}"); continue
        bump("wake: CHANGED" if res.changed else "wake: UNCHANGED")
        if mode == "unreadable" and not res.changed:
            bad("an unreadable source reported UNCHANGED", str(res.render()))
        try:
            nows.append(T0+timedelta(minutes=rng.choice([6,61,601])))
            res2 = wg.run(coll, p, persist=persist, now=nows[-1])
            bump("wake: CHANGED" if res2.changed else "wake: UNCHANGED")
        except json.JSONDecodeError:
            bracket_snapshot_raises = True
        if persist and p.exists():
            try:
                saved = json.loads(p.read_text())
            except json.JSONDecodeError:
                bad("the wake gate saved invalid json", p.read_text()[:80]); continue
            # the invariant is "never ahead of the clock the caller passed in", not "ahead of a fixed bound"
            if saved.get("last_changed_at") and datetime.fromisoformat(saved["last_changed_at"]) > max(nows):
                bad("the saved last_changed_at is in the future", saved.get("last_changed_at"))
        if not persist and mode == "fresh" and p.exists():
            bad("a companion wrote the shared snapshot", str(p))

    not_vacuous("wake gate CHANGED", STATS.get("wake: CHANGED", 0))
    not_vacuous("wake gate UNCHANGED", STATS.get("wake: UNCHANGED", 0))

def sweep_dispatch(rng, n=200):
    """Property: the guard never raises on hostile sources, and only overrides allow a blocked dispatch."""
    root = Path(tempfile.mkdtemp())
    for i in range(n):
        claims = dg.Claims(root/f"claims{i}.json")
        if rng.random() < 0.5:
            claims.claim(["ISSUE-1"], "other-session", "w-aaaaaa", T0)
        raiser = (lambda k: (_ for _ in ()).throw(RuntimeError("vcs down")))
        src = dg.Sources(open_prs_naming=rng.choice([lambda k: [], lambda k: [{"repo":"o/a","number":1,"author":"t"}], raiser]),
                         assignee=rng.choice([lambda k: None, lambda k: "someone", raiser]),
                         live_workers=rng.choice([lambda: [], lambda: [{"id":"w1","row":"R-001"}], raiser]))
        override = rng.choice([None, "", "owner said so"])
        try:
            v = dg.guard("ISSUE-1", "R-001", "s1", src, claims, now=T0, allow_duplicate=override if override else None)
        except ValueError as e:
            if override is not None and override.strip() == "": continue   # documented refusal
            bad("the guard raised", str(e)); continue
        except Exception as e:
            bad("the guard raised", f"{type(e).__name__}: {e}"); continue
        bump("dispatch: refused" if v.refusals else "dispatch: allowed")
        if v.refusals and not (override and override.strip()):
            if v.allowed: bad("the guard allowed a refused dispatch", str(v.refusals))
        if v.override and not v.refusals:
            bump("guard override with nothing to override")

    not_vacuous("dispatch refusals", STATS.get("dispatch: refused", 0))
    not_vacuous("dispatch allowances", STATS.get("dispatch: allowed", 0))

def _proc_worker(args):
    i, root, per = args
    import sys as _s; _s.path.insert(0, 'examples')
    import ledger_store as _ls
    for j in range(per):
        _ls.Ledger(Path(root)).write_row(None, next=f"p{i}-{j}")

    # deterministic reproducer for snapshot corruption in the lease itself
    tmp = tempfile.mkdtemp(); lease_path = Path(tmp)/"state"/"lease.json"
    lease_path.parent.mkdir(parents=True)
    broken = lz.Lease(lease_path)
    for content in ["{not json", "[]", "null", ""]:
        lease_path.write_text(content)
        try:
            broken.epoch()
            bump(f"lease tolerated corrupt: {content[:8]}")
        except Exception as e:
            bad("lease.epoch() crashes on corrupt snapshot", f"{content[:10]!r} -> {type(e).__name__}")

def sweep_ledger_processes(nproc=4, per=8):
    """Cross-process, not just threads: the ledger lock has to hold between processes."""
    import multiprocessing as mp
    root = tempfile.mkdtemp()
    with mp.Pool(nproc) as pool: pool.map(_proc_worker, [(i, root, per) for i in range(nproc)])
    led = ls.Ledger(Path(root))
    ids = [r["id"] for r in led.rows()]
    if len(ids) != nproc*per or len(set(ids)) != len(ids):
        bad("cross-process write_row lost or duplicated a row", f"{len(ids)} rows, {len(set(ids))} unique, expected {nproc*per}")
    errs, _ = led.check()
    if errs: bad("check() errors after cross-process writes", str(errs[:3]))


def sweep_close(rng, n=100):
    """Property: a close always requires the shape the ledger doc calls proof, and is idempotent."""
    root = Path(tempfile.mkdtemp()); led = ls.Ledger(root)
    proofs = ["ok", "verified", "merged abc1234", "merged", "tests: 1171 passed", "exit 0"]
    empties = ["", " ", "\t"]
    for i in range(n):
        row = led.write_row(None, key=f"ISSUE-{i:03d}", next="fix it")
        proof = rng.choice(proofs if rng.random() < 0.7 else empties)
        if rng.random() < 0.1: proof = rng.choice(proofs) + "| pipe |"
        # double close should abort, not double archive
        try:
            led.close_row(row["id"], proof=proof)
            close_ok = True
        except Exception:
            close_ok = False
        if close_ok:
            try:
                led.close_row(row["id"], proof=proof)
                bad("#8.1 a second close of the same row archives again", proof)
            except Exception:
                pass
    # empty proofs must refuse *somewhere*, even if only at close_row
    bad_here = [(t, row) for t, row in FAILS if t.startswith("#8.1")]
    if bad_here:
        bad("close rows accept unnamed proof", str(bad_here[:3]))


def sweep_lease(rng, n=70):
    """Epoch strictly increases on take(), and never decreases."""
    path = Path(tempfile.mkdtemp())/"state"/"lease.json"
    L = lz.Lease(path)
    for step in range(n):
        t = T0 + timedelta(minutes=step*17)
        try:
            exists = L.read() is True  # careful: read returns the dict
            ops = ["init","renew","take"] if not exists else ["renew","take","force","corrupt"]
        except (json.JSONDecodeError, AttributeError):
            bump("lease: corrupt file ignored safely")
            continue
        op = rng.choice(ops)
        try:
            e0 = L.epoch() if path.exists() else 0
            h0 = L.holder()
        except (json.JSONDecodeError, AttributeError):
            bad("lease epoch/holder crashed on corrupt snapshot", path.read_text()[:30])
            continue
        # rotate holder id so `take` has something to steal
        new_id = rng.choice(["sid1","sid2","sid3"])
        try:
            if op == "init": L.init(new_id, "h", now=t)
            elif op == "renew": L.renew(h0, now=t) if h0 else L.renew(new_id, now=t)
            elif op == "take": L.take(new_id, "h", "why", now=t)
            elif op == "force": L.take(new_id, "h", "force", now=t, force=True)
            elif op == "corrupt": path.write_text(rng.choice(["{not json","[]","null"]))
        except lz.LeaseError:
            bump(f"lease refused: {op}")
            continue
        except (json.JSONDecodeError, AttributeError):
            bad("epoch() crashed on a corrupt lease", path.read_text()[:30])
            continue
        e1 = L.epoch() if path.exists() else 0
        if e1 < e0:
            bad("lease epoch decreased", f"{e0} -> {e1}")
        if op in ("take","force") and h0 != new_id:
            if e1 != e0 + 1:
                bad("lease take did not bump epoch by one", f"{e0} -> {e1} holder {h0}->{new_id}")
            if L.holder() != new_id:
                bad("lease did not change holder on a take", str(L.read()[:60]))
        if op == "init" and path.exists() and e1 != 1:
            bad("init did not start epoch at 1", str(L.read()[:60]))
        bump("lease ops applied")

rng = random.Random(20260116)
print("sweeping ledger rows...");  sweep_ledger(rng)
print("sweeping parsers...");       sweep_parsers(rng)
print("sweeping the merge gate..."); sweep_merge_gate(rng)
print("sweeping timers...");         sweep_timers(rng)
print("sweeping the wake gate...");  sweep_wake_gate(rng)
print("sweeping the close path...");       sweep_close(rng)
print("sweeping lease...");        sweep_lease(rng)
print("sweeping dispatch...");       sweep_dispatch(rng)
print("sweeping the ledger across processes..."); sweep_ledger_processes()
print("\ncoverage: " + ", ".join(f"{k}={v}" for k,v in sorted(STATS.items())))
print(f"{len(FAILS)} invariant violation(s)")
sys.exit(1 if FAILS else 0)
