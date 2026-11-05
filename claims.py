"""Probe every normative claim the docs make about behaviour, and report PASS / FAIL / NO-CODE."""
import sys, json, tempfile, shutil
sys.path.insert(0, 'examples')
from pathlib import Path
from datetime import datetime, timezone, timedelta
import ledger_store as ls, timers as tm, wake_gate as wg, lease as lz, dispatch_guard as dg, merge_gate as mg
T0 = datetime(2026,1,16,14,0,tzinfo=timezone.utc)
rows = []
def chk(claim, where, ok, note=""):
    rows.append((("PASS" if ok else "FAIL"), claim, where, note))

# ---------- ledger_store.py vs docs/ledger.md ----------
def R(): return Path(tempfile.mkdtemp())
r=R(); led=ls.Ledger(r); a=led.write_row(None,next="first"); led.close_row(a["id"],proof="merged abc")
chk("ids are never reused across closed archives","ledger.md",led.write_row(None,next="x")["id"]=="R-002")
r=R(); led=ls.Ledger(r); led.write_row(None,next="ok")
with led.path.open("a") as f: f.write("| R-001 | bad | nobody | — | — | tomorrow |\n")
j="\n".join(led.check()[0])
chk("checker flags duplicate id, key, empty next, owner, due","ledger.md",all(t in j for t in ("duplicate id","key must be","no next action","owner must be","due must be")))
r=R(); led=ls.Ledger(r, require_key=lambda row: row["owner"].startswith("worker-"))
led.write_row(None, owner="worker-7f3a2c", next="x")
chk("checker warns on a new row opened without a key","ledger.md",bool(led.check()[1]))
r=R(); led=ls.Ledger(r); row=led.write_row(None,next="short",waits="x"*900)
chk("a row over 500 chars spills and stays under budget","ledger.md",ls.row_chars(row)<=ls.ROW_BUDGET,f"{ls.row_chars(row)} chars")
r=R(); led=ls.Ledger(r); led.write_row(None,next="x"*900)
chk("long text spills to ledger/notes/<id>.md","ledger.md",next((r/"ledger"/"notes").glob("*.md"),None) is not None)
r=R(); led=ls.Ledger(r); row=led.write_row(None,owner="owner",next="pick a price",waits="—")
chk("queue derives from owner AND waits-on","ledger.md",ls.queue_of(row)!="ACTIVE",f"got {ls.queue_of(row)} (filed: #3 s5)")
try:
    r=R(); ls.Ledger(r).close_row("R-001","")
    chk("a row closes only with proof","ledger.md",False)
except ValueError:
    chk("a row closes only with proof","ledger.md",True)
r=R(); led=ls.Ledger(r); led.write_row(None,next="a")
with led.lock.open() as fh:
    pass
chk("lock file records the holder","ledger.md", "pid=" in (led.lock.read_text() if led.lock.exists() else "pid="), "pid only, no script name")
r=R(); led=ls.Ledger(r)
with ls.file_lock(led.lock):
    with ls.file_lock(led.lock):
        inner=True
chk("lock is re-entrant within a process","ledger.md",inner)

# ---------- timers.py vs docs/waking.md ----------
r=R(); t=tm.Timers(r/"timers.json"); t.add("R-1","+1m","x","p",now=T0)
t.add("R-2","+5m","y","q",now=T0)
data=t.load(); [x for x in data if x["row"]=="R-2"][0]["state"]="sending"; t.save(data)
log=t.tick(lambda a_,b_:"ok",{"R-1","R-2"},T0+timedelta(hours=1))
chk("a timer found in sending is failed, never resent","waking.md",any("crashed mid-send" in l for l in log))
r=R(); t=tm.Timers(r/"timers.json"); t.add("R-1","+1m","x","p",now=T0)
log=t.tick(lambda a_,b_:"ok",{"R-9"},T0+timedelta(hours=1))
chk("a due timer on a closed row is cancelled","waking.md",any("cancelled" in l for l in log))
chk("lenient due parsing defaults and says so","waking.md",tm.parse_due("tomorrow 9am",T0)[1] is not None)
r=R(); t=tm.Timers(r/"timers.json")
_real_hex=tm.secrets.token_hex
_seq=iter(["aaaaaa","aaaaaa","bbbbbb","cccccc"])          # a *sequence*: a constant makes the redraw loop spin
tm.secrets.token_hex=lambda n: next(_seq,"dddddd")
try:
    t.add("R-1","+1m","x","p",now=T0); t.add("R-2","+1m","y","q",now=T0)
finally:
    tm.secrets.token_hex=_real_hex
chk("timer ids are unique","waking.md",len({x["id"] for x in t.load()})==2, [x["id"] for x in t.load()])

# ---------- wake_gate.py vs docs/waking.md ----------
r=R(); p=r/"wg.json"; state={"n":1}; src={"ledger":lambda:{"R-1":state["n"]}}
wg.run(src,p,persist=True,now=T0); chk("UNCHANGED when nothing moved","waking.md",not wg.run(src,p,persist=True,now=T0+timedelta(minutes=20)).changed)
state["n"]=2; chk("CHANGED when an item moves","waking.md",wg.run(src,p,persist=True,now=T0+timedelta(minutes=40)).changed)
def boom(): raise OSError("locked")
res=wg.run({"ledger":boom},p,persist=True,now=T0+timedelta(minutes=60))
chk("an unreadable source is CHANGED","waking.md",res.changed)
p2=r/"wg2.json"; p2.write_text(json.dumps({"parts":{},"checked_at":T0.isoformat(),"last_changed_at":(T0+timedelta(days=10)).isoformat()}))
chk("max_quiet forces a pass","waking.md",wg.run({},p2,persist=True,now=T0+timedelta(days=5),max_quiet=timedelta(hours=4)).changed,"(filed: #3 s6, clock skew)")

p3=Path(tempfile.mkdtemp())/"wg3.json"
wg.run({"n":lambda:{"a":1}},p3,persist=False,now=T0)
chk("a companion (persist=False) leaves the snapshot alone","waking.md",not p3.exists())


# ---------- merge_gate.py vs docs/merge-gate.md ----------
pol = mg.Policy(deploys_on_merge=frozenset({"org/site"}))  # no required-check policy: the 15 generic rows
req = mg.Policy(deploys_on_merge=frozenset({"org/site"}), required_checks={"org/app":frozenset({"ci","e2e"})})
def _pr(**kw):
    b=dict(repo="org/app",number=1,title="t",body="",head_sha="a"*40,is_draft=False,mergeable=True,
           checks={"ci":"success","e2e":"success"},reviews=[mg.Review("a"*40,"No major issues")],
           unresolved_threads=0,last_push_at=T0)
    b.update(kw); return mg.PR(**b)
def _d(row="", now=T0+timedelta(hours=2), **kw): return mg.decide(_pr(**kw), row, pol, now).action
for label, expect, kw in [
    ("hold marker on the row","REPORT",dict(row="HOLD: owner review")),
    ("hold marker in the PR","REPORT",dict(body="merge only after the migration")),
    ("draft PR","REPORT",dict(is_draft=True)),
    ("not mergeable","REPORT",dict(mergeable=False)),
    ("mergeability not computed","WAIT",dict(mergeable=None)),
    ("check pending","WAIT",dict(checks={"ci":"in_progress"})),
    ("check failed","REPORT",dict(checks={"ci":"failure"})),
    ("check cancelled","REPORT",dict(checks={"ci":"cancelled"})),
    ("check timed out","REPORT",dict(checks={"ci":"timed_out"})),
    ("no receipt, already asked","WAIT",dict(reviews=[mg.Review("old","x")],review_requested_for={"a"*40})),
    ("receipt says findings","REPORT",dict(reviews=[mg.Review("a"*40,"verdict: findings")])),
    ("receipt unparseable","REPORT",dict(reviews=[mg.Review("a"*40,"Summary: refactors")])),
    ("unresolved threads","REPORT",dict(unresolved_threads=1)),
    ("repo deploys on merge","REPORT",dict(repo="org/site")),
    ("all green","MERGE",dict()),
]:
    chk(f"decision table: {label} -> {expect}", "merge-gate.md", _d(**kw)==expect, _d(**kw))

def _req(now, **kw):
    return mg.decide(_pr(**kw), "", req, now).action
# inside the check_wait window nothing has "never reported" yet
chk("decision table: required check missing inside check_wait -> WAIT","merge-gate.md",
    _req(T0+timedelta(minutes=10), checks={"ci":"success"})=="WAIT")
chk("decision table: required check missing past check_wait -> REPORT","merge-gate.md",
    _req(T0+timedelta(minutes=45), checks={"ci":"success"})=="REPORT")
chk("decision table: required check pending -> WAIT","merge-gate.md",
    _req(T0+timedelta(minutes=10), checks={"ci":"in_progress","e2e":"success"})=="WAIT")
chk("decision table: required check skipped -> REPORT","merge-gate.md",
    _req(T0+timedelta(minutes=10), checks={"ci":"success","e2e":"skipped"})=="REPORT")
chk("decision table: required check neutral -> MERGE","merge-gate.md",
    _req(T0+timedelta(minutes=10), checks={"ci":"success","e2e":"neutral"})=="MERGE")

# ---------- lease.py vs docs/sessions-and-recovery.md ----------
r=R(); L=lz.Lease(r/"lease.json"); L.init("a","h",now=T0)
chk("an expired lease is flagged","sessions-and-recovery.md",L.state(T0+timedelta(hours=3))=="expired")
try: L.renew("b",now=T0); chk("only the holder renews","sessions-and-recovery.md",False)
except lz.LeaseError: chk("only the holder renews","sessions-and-recovery.md",True)
try: L.take("b","h2","",now=T0); chk("a takeover needs a reason","sessions-and-recovery.md",False)
except lz.LeaseError: chk("a takeover needs a reason","sessions-and-recovery.md",True)
e1=L.epoch(); fenced=ls.Ledger(r, fence=L.fenced)
fenced.write_row(None, expect_epoch=e1, next="by the primary")
L.take("b","h2","rollover",now=T0+timedelta(hours=3),force=True)
try:
    fenced.write_row(None, expect_epoch=e1, next="stale")
    chk("a non-primary cannot write the ledger","sessions-and-recovery.md",False,"stale write landed")
except ls.StaleEpoch:
    chk("a non-primary cannot write the ledger","sessions-and-recovery.md",True)

# ---------- dispatch_guard.py vs docs/dispatch.md ----------
r=R(); claims=dg.Claims(r/"claims.json")
S=lambda **k: dg.Sources(open_prs_naming=k.get("prs",lambda k_:[]),assignee=k.get("asg",lambda k_:None),live_workers=k.get("w",lambda:[]))
chk("an open PR naming the key refuses","dispatch.md",not dg.guard("K-1","R-1","s",S(prs=lambda k:[{"repo":"o/a","number":1,"author":"t"}]),claims,now=T0).allowed)
chk("an outside assignee refuses","dispatch.md",not dg.guard("K-1","R-1","s",S(asg=lambda k:"someone"),claims,now=T0).allowed)
chk("a live worker on the row refuses","dispatch.md",not dg.guard("K-1","R-1","s",S(w=lambda:[{"id":"w1","row":"R-1"}]),claims,now=T0).allowed)
chk("a failed lookup is a warning, not a pass","dispatch.md",bool(dg.guard("K-1","R-1","s",S(prs=lambda k:(_ for _ in ()).throw(OSError())),claims,now=T0).warnings))
try:
    dg.guard("K-1","R-1","s",S(),claims,now=T0,allow_duplicate=" ")
    chk("allow-duplicate without a reason raises","dispatch.md",False)
except ValueError: chk("allow-duplicate without a reason raises","dispatch.md",True)


# ---------- durability: the concurrency claims in adapting.md and waking.md ----------
import threading, time as _time
def _together(n, fn):
    bar=threading.Barrier(n)
    def go(i): bar.wait(); fn(i)
    ts=[threading.Thread(target=go,args=(i,)) for i in range(n)]
    [t.start() for t in ts]; [t.join() for t in ts]

r=R(); _together(20, lambda i: tm.Timers(r/"timers.json").add("R-001","+5m",f"t{i}",f"p{i}",now=T0))
chk("20 concurrent timer adds all survive","waking.md",len(tm.Timers(r/"timers.json").load())==20)
r=R(); tm.Timers(r/"timers.json").add("R-001","+1m","s","p",now=T0); _sends=[]
def _send(a,b):
    _time.sleep(0.03); _sends.append(b); return "ok"
_together(3, lambda i: tm.Timers(r/"timers.json").tick(_send,{"R-001"},T0+timedelta(minutes=5)))
chk("3 overlapping timer ticks deliver once","waking.md",len(_sends)==1)
r=R(); _together(20, lambda i: dg.Claims(r/"claims.json").claim([f"ISSUE-{i}"],f"s{i}",f"w{i:06x}",T0))
chk("20 concurrent claims are all recorded","dispatch.md",len(dg.Claims(r/"claims.json").load())==20)
r=R(); _c=dg.Claims(r/"claims.json"); _c.claim(["ISSUE-1"],"a","w1",T0)
try:
    _c.claim(["ISSUE-1"],"b","w2",T0); chk("the claim itself is test-and-set","dispatch.md",False)
except dg.ClaimHeld: chk("the claim itself is test-and-set","dispatch.md",True)
r=R(); _L=lz.Lease(r/"state/lease.json"); _L.init("a1","host-a",now=T0); _e=_L.epoch()
_fl=ls.Ledger(r, fence=_L.fenced); _fl.write_row(None, expect_epoch=_e, next="by a1")
_L.take("b2","host-b","rollover",now=T0+timedelta(hours=3),force=True)
chk("a takeover increments the epoch","sessions-and-recovery.md",_L.epoch()==_e+1)
try:
    _fl.write_row(None, expect_epoch=_e, next="stale"); chk("a stale epoch is refused","sessions-and-recovery.md",False)
except ls.StaleEpoch: chk("a stale epoch is refused","sessions-and-recovery.md",True)
_p=r/"state/wg.json"
try:
    wg.run({"n":lambda:{"a":1}},_p,persist=True,now=T0,fence=_L.fenced,expect_epoch=_e)
    chk("a stale snapshot save is refused","waking.md",False)
except wg.StaleEpoch: chk("a stale snapshot save is refused","waking.md",True)


# ---------- filed findings: the fixed ones must stay fixed, the open ones fail until they are ----------
def _verdict(body):
    pol_=mg.Policy(deploys_on_merge=frozenset())
    pr_=mg.PR(repo="o/a",number=1,title="t",body="",head_sha="a"*40,is_draft=False,mergeable=True,
              checks={"ci":"success"},reviews=[mg.Review("a"*40,body)],unresolved_threads=0,last_push_at=T0)
    return mg.review_verdict(pr_, pol_)
chk("#1.1 a clean phrase does not hide a finding","merge-gate.md",_verdict("Looks good to merge. One P1 bug in the retry loop.")=="findings")
chk("#1.1 a clean receipt still reads clean","merge-gate.md",_verdict("No major issues.")=="clean")
chk("#1.1 an unparseable receipt is not clean","merge-gate.md",_verdict("Summary: refactors the loop.")=="unparsed")
_w=tm.parse_wait("WAIT ci +45m",T0)
chk("#1.4 the check-by is not eaten when the ref is omitted","waking.md",_w["ref"]=="" and _w["due"]==T0+timedelta(minutes=45))
r=R(); _st=tm.Timers(r/"t.json"); _st.add("R-1","+1m","x","p",now=T0)
def _boom(a,b): raise ConnectionError("down")
for _i in range(5): _st.tick(_boom,{"R-1"},T0+timedelta(minutes=5+_i))
_got=[]; _st.tick(lambda a,b: _got.append(b) or "ok",{"R-1"},T0+timedelta(minutes=30))
chk("#3.6 a timer keeps retrying inside RETRY_WINDOW","waking.md",len(_got)==1)

# #6.1 a takeover must wait for a write already past the check (fixed in #7: take now waits)
r=R(); _L3=lz.Lease(r/"state/lease.json"); _L3.init("lap","h",now=T0); _e3=_L3.epoch()
_l3=ls.Ledger(r, fence=_L3.fenced); _real=ls.atomic_write
_g_in=threading.Event(); _g_out=threading.Event()
def _gated(path,text):
    _g_in.set(); _g_out.wait(5); return _real(path,text)
ls.atomic_write=_gated; _res={}
def _stale():
    try: _res["id"]=_l3.write_row(None, expect_epoch=_e3, next="in flight")["id"]
    except Exception as ex: _res["err"]=type(ex).__name__
_th=threading.Thread(target=_stale); _th.start(); _g_in.wait(5)
_take={}
def _taker():
    try: _take["epoch"]=_L3.take("new","h2","slept past the TTL",now=T0+timedelta(hours=3),force=True)["epoch"]
    except Exception as ex: _take["err"]=type(ex).__name__
_t2=threading.Thread(target=_taker); _t2.start(); _t2.join(timeout=0.5)
_waited=_t2.is_alive()                      # the take must be blocked while the write is in flight
_g_out.set(); _th.join(); _t2.join(timeout=5); ls.atomic_write=_real
chk("#6.1 a takeover waits for a write already past the check","sessions-and-recovery.md",
    _waited and _take.get("epoch")==_e3+1 and _res.get("id")=="R-001",
    {"take blocked":_waited,"take":_take,"write":_res})
try:
    _l3.write_row(None, expect_epoch=_e3, next="after the take"); chk("#6.1 a stale write after the take is refused","sessions-and-recovery.md",False)
except ls.StaleEpoch: chk("#6.1 a stale write after the take is refused","sessions-and-recovery.md",True)

# #6.1b the wake-gate save is inside the fence too
r=R(); _L4=lz.Lease(r/"state/lease.json"); _L4.init("lap","h",now=T0); _e4=_L4.epoch(); _p4=r/"state/wg.json"
_real_replace=wg.os.replace; _s_in=threading.Event(); _s_out=threading.Event()
def _slow_replace(a,b):
    _s_in.set(); _s_out.wait(5); return _real_replace(a,b)
wg.os.replace=_slow_replace; _save={}
def _saver():
    try: wg.run({"n":lambda:{"a":1}},_p4,persist=True,now=T0,fence=_L4.fenced,expect_epoch=_e4); _save["ok"]=True
    except Exception as ex: _save["err"]=type(ex).__name__
_t4=threading.Thread(target=_saver); _t4.start(); _s_in.wait(5)
_t5={}
def _taker2():
    try: _t5["epoch"]=_L4.take("new","h2","skew",now=T0+timedelta(hours=3),force=True)["epoch"]
    except Exception as ex: _t5["err"]=type(ex).__name__
_t6=threading.Thread(target=_taker2); _t6.start(); _t6.join(timeout=0.5)
_waited2=_t6.is_alive()
_s_out.set(); _t4.join(); _t6.join(timeout=5); wg.os.replace=_real_replace
chk("#6.1b a takeover waits for a snapshot save in flight","waking.md",
    _waited2 and _t5.get("epoch")==_e4+1 and _p4.exists(), {"take blocked":_waited2,"take":_t5,"save":_save})

# #6.2 expect_epoch is never silently ignored (OPEN)
r=R(); _led=ls.Ledger(r); _ignored=False
try: _led.write_row(None, expect_epoch=999, next="unfenced ledger")
except Exception: _ignored=True
chk("#6.2 an epoch is never silently ignored","ledger.md",_ignored)

# #6.4 the spill keeps a later then-owner step (OPEN)
r=R(); _led=ls.Ledger(r); _row=_led.write_row(None,next="y"*400,waits="x"*900+"; then owner: review and pay")
chk("#6.4 the spill keeps a later then-owner step","ledger.md","then owner" in _row["waits"])


# ---------- #8: the close path (OPEN until fixed) ----------
import re as _re
def _close_with(proof):
    r_ = R(); l_ = ls.Ledger(r_); x_ = l_.write_row(None, key="ISSUE-9", next="fix it")
    try: l_.close_row(x_["id"], proof=proof); return "closed"
    except Exception as e: return type(e).__name__
chk("#8.1 a close refuses a bare prose proof", "ledger.md", _close_with("verified") != "closed", _close_with("verified"))
chk("#8.1 a close refuses an empty proof too", "ledger.md", _close_with("") != "closed")
_chk_ok = mg.Decision("MERGE", "r") ; _sha="a"*40
_c8=mg.PR(repo="o/a",number=1,title="t",body="",head_sha=_sha,is_draft=False,mergeable=True,checks={"ci":"success"},
          reviews=[mg.Review(_sha,"## Review\n\n### Findings\n- P1: drops messages\n- P2: no test\n\nverdict: clean")],
          unresolved_threads=0,last_push_at=T0)
chk("#8.2 a receipt with findings under a clean token is not clean","merge-gate.md",
    mg.review_verdict(_c8, mg.Policy(deploys_on_merge=frozenset()))=="findings")
_c8b=mg.PR(repo="o/a",number=1,title="t",body="",head_sha=_sha,is_draft=False,mergeable=True,checks={"ci":"success"},
           reviews=[mg.Review(_sha,"About this review:\n```\nverdict: clean\n```\nFindings:\n- P1: drops messages")],
           unresolved_threads=0,last_push_at=T0)
chk("#8.2b a verdict inside a fenced example is ignored","merge-gate.md",
    mg.review_verdict(_c8b, mg.Policy(deploys_on_merge=frozenset()))=="findings")
_r8 = R(); _p8 = _r8/"wg.json"; _p8.write_text("{not json")
try:
    _r8res = wg.run({"x": lambda: {"a": 1}}, _p8, persist=True, now=T0)
    chk("#8.3 an unreadable snapshot still produces a pass","waking.md", _r8res.changed)
except Exception as e:
    chk("#8.3 an unreadable snapshot still produces a pass","waking.md", False, type(e).__name__)
_chk_clean = ls.clean("exit 0: `cargo test | tail -1` -> 1171 passed")
chk("#8.4 the archived proof is verbatim","ledger.md","|" in _chk_clean, _chk_clean)
_r5 = R(); _t5 = tm.Timers(_r5/"t.json")
_real = tm.secrets.token_hex; tm.secrets.token_hex = lambda n: "aaaaaa"
_done = threading.Event()
def _spin():
    _t5.add("R-1","+1m","a","p",now=T0)
    _t5.add("R-2","+1m","b","q",now=T0)   # second add hits the redraw loop
    _done.set()
_sp = threading.Thread(target=_spin, daemon=True); _sp.start(); _done.wait(2)
tm.secrets.token_hex = _real
chk("#8.5 the id redraw always terminates","waking.md", _done.is_set(), "spins against a constant RNG")
chk("#8.6 an expired claim with a recent dispatch record still refuses","dispatch.md",
    "recent_dispatches" in [f.name for f in dg.Sources.__dataclass_fields__.values() if hasattr(f,'name')] or
    any("dispatch" in f for f in dg.Sources.__dataclass_fields__), list(dg.Sources.__dataclass_fields__))


# ---------- held findings (H1 / H7): corrupt snapshot crashes ----------
_held_lease_path = R()/"state"/"lease.json"; _held_lease_path.parent.mkdir(parents=True, exist_ok=True)
_held_lease_path.write_text("{not json")
try:
    lz.Lease(_held_lease_path).epoch()
    _lease_res = "ok"
except Exception as _e:
    _lease_res = type(_e).__name__
chk("#H7 lease.epoch tolerates a corrupt snapshot","sessions-and-recovery.md", _lease_res == "ok", _lease_res)
_held_wg = R()/"wg.json"; _held_wg.write_text("{not json")
try:
    _res = wg.run({"x": lambda: {"a": 1}}, _held_wg, persist=True, now=T0)
    _wg_res = "ok"
except Exception as _e:
    _wg_res = type(_e).__name__
chk("#H1 wake gate tolerates a corrupt snapshot","waking.md", _wg_res == "ok", _wg_res)

print(f"{'verdict':7} {'claim':62} {'source':28} note")
print("-"*135)
for v,c,w,n in rows: print(f"{v:7} {c[:62]:62} {w[:28]:28} {n}")
print(f"\n{sum(1 for r_ in rows if r_[0]=='PASS')} pass, {sum(1 for r_ in rows if r_[0]=='FAIL')} fail, {len(rows)} total")
if "--json" in sys.argv:
    import pathlib as _p
    _p.Path("claims-report.json").write_text(json.dumps([{"verdict":v,"claim":c,"source":w,"note":n} for v,c,w,n in rows], indent=2))
    print("wrote claims-report.json")
