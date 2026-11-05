"""Attack the new epoch fence: it checks the epoch under the store's lock, but take() bumps it under the lease's."""
import sys, tempfile, threading, json
sys.path.insert(0,'examples')
from pathlib import Path
from datetime import datetime, timezone, timedelta
import ledger_store as ls, lease as lz, wake_gate as wg, timers as tm
T0=datetime(2026,1,16,14,0,tzinfo=timezone.utc)

print("=== A1: a write already past the fence lands after the takeover ===")
r=Path(tempfile.mkdtemp()); L=lz.Lease(r/"state/lease.json"); L.init("laptop","host-a",now=T0)
led=ls.Ledger(r, lease_epoch=L.epoch)
e1=L.epoch()
real=ls.atomic_write
at_write=threading.Event(); release=threading.Event()
def slow_atomic(path, text):
    at_write.set(); release.wait(5)      # the fence already passed; the write is not yet durable
    return real(path, text)
ls.atomic_write=slow_atomic
result={}
def stale_write():
    try: result["row"]=led.write_row(None, expect_epoch=e1, next="the sleeping laptop writes")
    except Exception as e: result["err"]=type(e).__name__
th=threading.Thread(target=stale_write); th.start()
at_write.wait(5)
L.take("new-primary","host-b","laptop slept past the TTL", now=T0+timedelta(hours=3), force=True)
epoch_now=L.epoch()
release.set(); th.join(); ls.atomic_write=real
rows=[x for x in led.rows()]
print(f"  epoch went 1 -> {epoch_now} while the write was in flight")
print(f"  write result: {result.get('row',{}).get('id','REFUSED '+str(result.get('err')))}")
print(f"  ledger now holds: {[x['id'] for x in rows]}")
print(f"  => stale write landed after the takeover: {bool(rows) and epoch_now>e1}")

print()
print("=== A2: the wake-gate save has the same shape, and no lock at all ===")
r2=Path(tempfile.mkdtemp()); L2=lz.Lease(r2/"state/lease.json"); L2.init("laptop","h",now=T0)
p=r2/"state/wg.json"; e=L2.epoch()
real_replace=wg.os.replace
at_save=threading.Event(); rel=threading.Event()
def slow_replace(a,b):
    at_save.set(); rel.wait(5); return real_replace(a,b)
wg.os.replace=slow_replace
out={}
def save():
    try: wg.run({"n":lambda:{"a":1}},p,persist=True,now=T0,lease_epoch=L2.epoch,expect_epoch=e); out["ok"]=True
    except Exception as ex: out["err"]=type(ex).__name__
t=threading.Thread(target=save); t.start(); at_save.wait(5)
L2.take("new","h2","skew",now=T0+timedelta(hours=3),force=True)
rel.set(); t.join(); wg.os.replace=real_replace
print(f"  save completed: {out}, snapshot on disk: {p.exists()}, epoch now {L2.epoch()}")

print()
print("=== A3: expect_epoch is silently ignored when lease_epoch is not configured ===")
r3=Path(tempfile.mkdtemp()); led=ls.Ledger(r3)
row=led.write_row(None, expect_epoch=999, next="caller thinks it is fenced")
print(f"  write_row(expect_epoch=999) on an unfenced ledger -> {row['id']} (no error, no fence)")

print()
print("=== A4: timer ids can still collide ===")
r4=Path(tempfile.mkdtemp()); t=tm.Timers(r4/"t.json")
import secrets as s
real_hex=s.token_hex; s.token_hex=lambda n:"abcdef"
t.add("R-1","+1m","a","p",now=T0); t.add("R-2","+1m","b","q",now=T0)
ids=[x["id"] for x in t.load()]
print(f"  two adds -> ids {ids}, unique={len(set(ids))==len(ids)}")
s.token_hex=real_hex

print()
print("=== A5: a long waits-on loses its '; then owner' clause ===")
r5=Path(tempfile.mkdtemp()); led=ls.Ledger(r5)
row=led.write_row(None, next="short", waits="x"*430+"; then owner: review and pay the invoice")
print(f"  waits after spill: {row['waits'][-60:]!r}")
print(f"  'then owner' survives: {'then owner' in row['waits']}")
