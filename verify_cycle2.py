"""Verify every claim in the maintainer's #3 closing comment, then try to break the new fence."""
import sys, json, tempfile, threading, time
sys.path.insert(0,'examples')
from pathlib import Path
from datetime import datetime, timezone, timedelta
import ledger_store as ls, timers as tm, dispatch_guard as dg, lease as lz, wake_gate as wg, merge_gate as mg
T0=datetime(2026,1,16,14,0,tzinfo=timezone.utc)
def R(): return Path(tempfile.mkdtemp())
out=[]
def v(claim, ok, detail=""):
    out.append(("PASS" if ok else "FAIL", claim, detail)); print(f"  {'PASS' if ok else 'FAIL'}  {claim}  {detail}")
def together(n, fn):
    bar=threading.Barrier(n)
    def go(i): bar.wait(); fn(i)
    ts=[threading.Thread(target=go,args=(i,)) for i in range(n)]
    [t.start() for t in ts]; [t.join() for t in ts]

print("=== claims from the closing comment ===")
r=R(); together(20, lambda i: tm.Timers(r/"timers.json").add("R-001","+5m",f"t{i}",f"p{i}",now=T0))
n=len(tm.Timers(r/"timers.json").load()); v("20 concurrent add -> 20", n==20, f"got {n}")

for ticks in (2,3,5):
    r=R(); tm.Timers(r/"timers.json").add("R-001","+1m","s","p",now=T0); sends=[]
    def send(a,b):
        time.sleep(0.03); sends.append(b); return "ok"
    together(ticks, lambda i: tm.Timers(r/"timers.json").tick(send,{"R-001"},T0+timedelta(minutes=5)))
    v(f"{ticks} overlapping ticks -> 1 send", len(sends)==1, f"got {len(sends)}")

# a tick during a live send leaves it in sending
r=R(); st=tm.Timers(r/"timers.json"); st.add("R-001","+1m","s","p",now=T0)
started=threading.Event(); release=threading.Event()
def slow_send(a,b):
    started.set(); release.wait(5); return "ok"
th=threading.Thread(target=st.tick,args=(slow_send,{"R-001"},T0+timedelta(minutes=5))); th.start()
started.wait(5)
second=st.tick(lambda a,b:"ok",{"R-001"},T0+timedelta(minutes=5))
release.set(); th.join()
mid={x["state"] for x in st.load()}
v("tick during a live send leaves sending (not failed)", mid=={"delivered"}, f"final {mid}; 2nd tick said {second}")

r=R(); together(20, lambda i: dg.Claims(r/"claims.json").claim([f"ISSUE-{i}"],f"s{i}",f"w{i:06x}",T0))
n=len(dg.Claims(r/"claims.json").load()); v("20 concurrent claim -> 20", n==20, f"got {n}")
r=R(); c=dg.Claims(r/"claims.json"); c.claim(["ISSUE-1"],"a","w1",T0)
try: c.claim(["ISSUE-1"],"b","w2",T0); v("claim is test-and-set (ClaimHeld)", False)
except dg.ClaimHeld: v("claim is test-and-set (ClaimHeld)", True)
c.claim(["ISSUE-1"],"b","w2",T0,override="owner said so")
v("override records the claim", c.load()["ISSUE-1"]["session"]=="b")

r=R(); L=lz.Lease(r/"state/lease.json"); L.init("a1","host-a",now=T0); e1=L.epoch()
led=ls.Ledger(r, lease_epoch=L.epoch); led.write_row(None, expect_epoch=e1, next="by a1")
L.take("b2","host-b","rollover",now=T0+timedelta(hours=3), force=True)
v("take increments the epoch", L.epoch()==e1+1, f"{e1} -> {L.epoch()}")
try: led.write_row(None, expect_epoch=e1, next="stale"); v("stale write_row refused", False)
except ls.StaleEpoch: v("stale write_row refused", True)
try: tt=tm.Timers(r/"state/timers.json", lease_epoch=L.epoch)
except TypeError as e: print("   Timers signature:", e); tt=tm.Timers(r/"state/timers.json")
try:
    tt.add("R-1","+1m","x","p",now=T0,expect_epoch=e1); v("stale Timers.add refused", False)
except tm.StaleEpoch: v("stale Timers.add refused", True)
except TypeError as e: v("stale Timers.add refused", False, f"signature: {e}")
p=r/"state/wg.json"
try:
    wg.run({"n":lambda:{"a":1}},p,persist=True,now=T0,lease_epoch=L.epoch,expect_epoch=e1); v("stale snapshot save refused", False)
except wg.StaleEpoch: v("stale snapshot save refused", True)

pol=mg.Policy(deploys_on_merge=frozenset(), required_checks={"org/app":frozenset({"ci","e2e"})})
def pr(**kw):
    b=dict(repo="org/app",number=1,title="t",body="",head_sha="a"*40,is_draft=False,mergeable=True,
           checks={"ci":"success","e2e":"success"},reviews=[mg.Review("a"*40,"No major issues")],
           unresolved_threads=0,last_push_at=T0)
    b.update(kw); return mg.PR(**b)
v("required neutral -> MERGE", mg.decide(pr(checks={"ci":"success","e2e":"neutral"}),"",pol,T0+timedelta(hours=2)).action=="MERGE")
v("required skipped -> REPORT", mg.decide(pr(checks={"ci":"success","e2e":"skipped"}),"",pol,T0+timedelta(hours=2)).action=="REPORT")
v("missing required after 30d -> REPORT", mg.decide(pr(checks={"ci":"success"}),"",pol,T0+timedelta(days=30)).action=="REPORT",
  mg.decide(pr(checks={"ci":"success"}),"",pol,T0+timedelta(days=30)).reason)
v("owner + em-dash -> DECISION", ls.queue_of({"owner":"owner","waits":"—"})=="DECISION")
v("worker + em-dash -> ACTIVE", ls.queue_of({"owner":"worker-7f3a2c","waits":"—"})=="ACTIVE")

r=R(); led=ls.Ledger(r, require_key=lambda row: row["owner"].startswith("worker-"))
led.write_row(None, owner="worker-7f3a2c", next="do it")
v("keyless worker row warns", any("key" in w for w in led.check()[1]), str(led.check()[1]))
r=R(); led=ls.Ledger(r); row=led.write_row(None,next="short",waits="x"*900)
v("spilled row fits the budget", ls.row_chars(row)<=ls.ROW_BUDGET, f"{ls.row_chars(row)} chars")
r=R(); p=r/"wg.json"; p.write_text(json.dumps({"parts":{},"checked_at":T0.isoformat(),"last_changed_at":(T0+timedelta(days=10)).isoformat()}))
res=wg.run({},p,persist=True,now=T0+timedelta(days=5),max_quiet=timedelta(hours=4))
v("future last pass forces a pass", res.changed)
v("saved last pass is clamped to now", json.loads(p.read_text())["last_changed_at"]==(T0+timedelta(days=5)).isoformat())

r=R(); st=tm.Timers(r/"t.json"); st.add("R-1","+1m","x","p",now=T0)
calls=[]
def boom(a,b): calls.append(1); raise ConnectionError("down")
st.tick(boom,{"R-1"},T0+timedelta(minutes=5))
delivered=[]
for i in range(1,5):   # 4 ticks = only a few minutes later, inside RETRY_WINDOW
    st.tick(boom,{"R-1"},T0+timedelta(minutes=5+i))
st.tick(lambda a,b: delivered.append(b) or "ok",{"R-1"},T0+timedelta(minutes=30))
v("timer retried after a short outage", len(delivered)==1, f"{len(calls)} errors, then {len(delivered)} delivery")
print(f"\n{sum(1 for x in out if x[0]=='PASS')} pass, {sum(1 for x in out if x[0]=='FAIL')} fail")
