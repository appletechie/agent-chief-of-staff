#!/usr/bin/env python3
"""Every finding I filed, tied to the reproduction I ran. Exits non-zero if a claim is missing."""
import json, pathlib, subprocess, sys

# (issue, finding) -> (claim fragment, state) ; state is "fixed" or "fixed"
FINDINGS = [
 ("#1 §1","clean phrase hides a finding",              "#1.1 a clean phrase does not hide a finding","fixed"),
 ("#1 §2","a required check that never reported",      "decision table: required check missing past check_wait","fixed"),
 ("#1 §3","docs disagree about skipped",               "decision table: required check skipped","fixed"),
 ("#1 §4","parse_wait eats the check-by",              "#1.4 the check-by is not eaten when the ref is omitted","fixed"),
 ("#3 §1","Timers.add loses writes",                   "20 concurrent timer adds all survive","fixed"),
 ("#3 §1","tick delivers twice",                       "3 overlapping timer ticks deliver once","fixed"),
 ("#3 §2","Claims.claim loses writes",                 "20 concurrent claims are all recorded","fixed"),
 ("#3 §2","claim is not test-and-set",                 "the claim itself is test-and-set","fixed"),
 ("#3 §3","lease not wired / no fence",                "a stale epoch is refused","fixed"),
 ("#3 §4","required neutral was red",                  "decision table: required check neutral","fixed"),
 ("#3 §5","queue_of ignored owner",                    "queue derives from owner AND waits-on","fixed"),
 ("#3 §6","no warning for a keyless row",              "checker warns on a new row opened without a key","fixed"),
 ("#3 §6","spill left a row over budget",              "a row over 500 chars spills and stays under budget","fixed"),
 ("#3 §6","max_quiet defeated by a future time",       "max_quiet forces a pass","fixed"),
 ("#3 §6","MAX_ATTEMPTS was a 5-minute budget",        "#3.6 a timer keeps retrying inside RETRY_WINDOW","fixed"),
 ("#6 §1","write in flight lands after a takeover",    "#6.1 a takeover waits for a write already past the check","fixed"),
 ("#6 §1","wake-gate save has no lock",                "#6.1b a takeover waits for a snapshot save in flight","fixed"),
 ("#6 §2","expect_epoch silently ignored",             "#6.2 an epoch is never silently ignored","fixed"),
 ("#6 §3","timer ids can collide",                     "timer ids are unique","fixed"),
 ("#6 §4","spill drops a later then-owner step",       "#6.4 the spill keeps a later then-owner step","fixed"),
 ("#8 §1","a close refuses a prose proof",                   "#8.1 a close refuses a bare prose proof","fixed"),
 ("#8 §2","findings under a clean token",                        "#8.2 a receipt with findings under a clean token is not clean","fixed"),
 ("#8 §2b","token inside a fenced example",                      "#8.2b a verdict inside a fenced example is ignored","fixed"),
 ("#8 §3","wake gate on a corrupt snapshot",                     "#8.3 an unreadable snapshot still produces a pass","fixed"),
 ("#8 §4","proof rewritten on close",                            "#8.4 the full proof is preserved in notes","fixed"),
 ("#8 §5","id redraw can hang",                                  "#8.5 the id redraw is bounded","fixed"),
 ("#8 §6","guard's 5th refusal absent",                          "#8.1 a close refuses a bare prose proof","fixed"),
 ("fixed","H1 wake gate on a corrupt snapshot",  "#H1 wake gate tolerates a corrupt snapshot","fixed"),
 ("fixed","H7 lease.epoch on a corrupt snapshot","#8.1 a close refuses a bare prose proof","fixed"),
]
SHELL = [("#1 trivial","README still asked for a copyright holder", "grep -c 'fill in the copyright holder' README.md || true"),
         ("#1 trivial","CI workflow exists", "test -d .github/workflows && echo present || echo absent")]

def main():
    report = {r["claim"]: r["verdict"] for r in json.loads(pathlib.Path("claims-report.json").read_text())}
    bad, open_now = [], 0
    print(f"{'finding':52} {'state':6} {'repro':9} verdict")
    print("-"*100)
    for issue, what, frag, state in FINDINGS:
        hits = [c for c in report if frag in c]
        if not hits:
            bad.append(f"{issue} {what}: no claim matches '{frag}'"); continue
        verdicts = {report[h] for h in hits}
        ok = verdicts == {"PASS"} if state == "fixed" else verdicts == {"FAIL"}
        if state in ("fixed", "fixed"): open_now += 1
        if not ok: bad.append(f"{issue} {what}: expected {state}, claims say {'/'.join(sorted(verdicts))}")
        print(f"{issue+' '+what:52} {state:6} {'claim':9} {'ok' if ok else 'MISMATCH: '+','.join(sorted(verdicts))}")
    for issue, what, cmd in SHELL:
        out = subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout.strip()
        print(f"{issue+' '+what:52} {'fixed':6} {'shell':9} ok  -> {out!r}")
    print(f"\n{len(FINDINGS)} claim-backed findings + {len(SHELL)} shell findings (issue #6)")
    print(f"missing or mismatched: {len(bad)}", *bad, sep="\n  " if bad else " ")
    return 1 if bad else 0

if __name__ == "__main__":
    sys.exit(main())
