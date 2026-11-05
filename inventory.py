#!/usr/bin/env python3
"""Classify every `## section` of every doc (pass --verbose to list each cited claim and its verdict): PROBED (cites passing claims), NOCODE, ADVICE, PROCESS or FILED.

Exits non-zero when a doc gains a section the manifest does not know about, or when a PROBED section cites a
claim that does not exist in claims-report.json or is failing. That is what stops this inventory from rotting:
the repo added sections in #2 and #5, and an unclassified section is exactly how a claim goes unexamined.
"""
import json, pathlib, sys

# (doc, section) -> (verdict, evidence)
# PROBED: evidence lists claim-name fragments that must exist in claims.py's report and pass.
MANIFEST = {
 ("adapting.md","Component map"): ("ADVICE","a menu of interchangeable tools per contract, no behaviour to run"),
 ("adapting.md","Minimal vs full"): ("ADVICE","coverage table; the shipped sketches are the 'minimal' column"),
 ("adapting.md","What to build first"): ("ADVICE","build order; 6 of 11 steps ship a sketch (measured)"),
 ("adapting.md","Testing the mechanisms"): ("PROBED",["lock is re-entrant","20 concurrent timer adds","3 overlapping timer ticks","an unreadable source is CHANGED","decision table: check failed"]),
 ("adapting.md","Things to decide up front"): ("ADVICE","owner decisions"),

 ("architecture.md","Roles"): ("ADVICE","responsibilities for the prompts, not a script"),
 ("architecture.md","Components"): ("NOCODE","11 of 20 state paths have no producer in examples/ (sessions, dispatch, events, intake, merge-gate, halt, policy, boot brief, sweeps, rollback)"),
 ("architecture.md","Policy, procedure, rules: three documents, one copy each"): ("NOCODE","documents; no code reads or compares them"),
 ("architecture.md","Wake sources"): ("NOCODE","scheduler wiring for the owner's runner"),
 ("architecture.md","A full pass"): ("PROCESS","the coordinator's turn procedure; steps 1-2 probed individually"),
 ("architecture.md","The brief"): ("NOCODE","no brief generator in examples/"),

 ("dispatch.md","Routing and placement"): ("NOCODE","router with live quota; not sketched"),
 ("dispatch.md","Workspaces"): ("NOCODE","worktree/branch lifecycle; not sketched"),
 ("dispatch.md","The duplicate guard"): ("PROBED",["an open PR naming the key","an outside assignee","a live worker on the row","a failed lookup is a warning","allow-duplicate without a reason","the claim itself is test-and-set","#8.1 a close refuses a bare prose proof"]),
 ("dispatch.md","Briefs"): ("NOCODE","templates ship as worker-brief.md / worker-rules.md; no code composes them"),
 ("dispatch.md","The WAIT protocol"): ("PROBED",["lenient due parsing"]),
 ("dispatch.md","Finishing"): ("NOCODE","procedure; step 5 'cancel the worker's timers' has no Timers.cancel (see #6 follow-up)"),

 ("guardrails.md","Action tiers"): ("NOCODE","policy text for the charter"),
 ("guardrails.md","Approvals are recorded verbatim"): ("NOCODE","no approval store"),
 ("guardrails.md","Holds"): ("PROBED",["decision table: hold marker on the row","decision table: hold marker in the PR"]),
 ("guardrails.md","Purchases: stop on the payment page"): ("NOCODE","prompt policy"),
 ("guardrails.md","Calendar"): ("NOCODE","prompt policy"),
 ("guardrails.md","Anything someone outside sees"): ("NOCODE","prompt policy"),
 ("guardrails.md","Secrets"): ("NOCODE","prompt policy plus a runner hook that examples/ does not have"),
 ("guardrails.md","Destructive operations"): ("NOCODE","prompt policy"),
 ("guardrails.md","Infrastructure changes"): ("NOCODE","prompt policy"),
 ("guardrails.md","Stop means stop"): ("NOCODE","halt.json is documented, not implemented"),
 ("guardrails.md","Logging automatic actions"): ("NOCODE","no action log"),

 ("intake-and-hygiene.md","Tracker intake"): ("NOCODE","no intake script"),
 ("intake-and-hygiene.md","The backlog pump"): ("NOCODE","no pump"),
 ("intake-and-hygiene.md","Drift detection"): ("NOCODE","no drift report"),
 ("intake-and-hygiene.md","Schedule health"): ("NOCODE","no schedule sweep"),
 ("intake-and-hygiene.md","Doc lint"): ("NOCODE","no doc lint"),
 ("intake-and-hygiene.md","Dead-worker sweep"): ("NOCODE","no sweep"),
 ("intake-and-hygiene.md","Lessons and memory"): ("NOCODE","no memory store"),
 ("intake-and-hygiene.md","Observability findings"): ("NOCODE","no findings collector"),

 ("ledger.md","Row format"): ("PROBED",["ids are never reused","a row closes only with proof","checker flags duplicate id"]),
 ("ledger.md","Queues"): ("PROBED",["queue derives from owner"]),
 ("ledger.md","Writes go through one function, under one lock"): ("PROBED",["lock is re-entrant","lock file records the holder"]),
 ("ledger.md","The checker"): ("PROBED",["checker flags duplicate id","checker warns on a new row"]),
 ("ledger.md","Closing with proof"): ("PROBED",["a row closes only with proof","#8.1 a close refuses a bare prose proof","#8.4 the full proof is preserved in notes"]),
 ("ledger.md","Exact timestamps"): ("NOCODE","a writing rule; no script parses or rejects a timestamp"),
 ("ledger.md","Generated views"): ("NOCODE","no dashboard or boot-brief generator"),
 ("ledger.md","History lives elsewhere"): ("PROCESS","where to put the story"),

 ("lessons.md","Coordination"): ("ADVICE","post-mortem"),("lessons.md","Waking and timers"): ("ADVICE","post-mortem"),
 ("lessons.md","Merges and deploys"): ("ADVICE","post-mortem"),("lessons.md","Secrets and shell"): ("ADVICE","post-mortem"),
 ("lessons.md","Approvals and people"): ("ADVICE","post-mortem"),("lessons.md","Infrastructure"): ("ADVICE","post-mortem"),
 ("lessons.md","Capacity"): ("ADVICE","post-mortem"),

 ("merge-gate.md","Scope"): ("NOCODE","which PRs are in scope is the caller's query; decide() starts from a PR"),
 ("merge-gate.md","The decision"): ("PROBED",["decision table: all green","decision table: check failed","decision table: check pending","decision table: required check missing inside check_wait","decision table: required check missing past check_wait","decision table: required check skipped","decision table: required check neutral"]),
 ("merge-gate.md","Holds: the lesson behind rule 1 and 2"): ("PROBED",["decision table: hold marker on the row","decision table: draft PR"]),
 ("merge-gate.md","Repos that deploy on merge"): ("PROBED",["decision table: repo deploys on merge"]),
 ("merge-gate.md","Review bots"): ("PROBED",["decision table: receipt says findings","decision table: receipt unparseable","decision table: no receipt, already asked","#8.2 a receipt with findings under a clean token is not clean","#8.2b a verdict inside a fenced example is ignored"]),
 ("merge-gate.md","What the gate never does"): ("PROBED",["decision table: repo deploys on merge","decision table: hold marker on the row","decision table: receipt unparseable"]),

 ("sessions-and-recovery.md","The lease"): ("PROBED",["#8.1 a close refuses a bare prose proof", "an expired lease is flagged","only the holder renews","a takeover needs a reason","a takeover increments the epoch","a stale epoch is refused"]),
 ("sessions-and-recovery.md","Primary vs companion"): ("PROBED",["a companion (persist=False)","a stale snapshot save is refused"]),
 ("sessions-and-recovery.md","Session registry"): ("NOCODE","sessions.json is documented, not implemented"),
 ("sessions-and-recovery.md","The boot brief"): ("NOCODE","no generator"),
 ("sessions-and-recovery.md","Stop in one is stop in all"): ("NOCODE","halt.json is documented, not implemented"),
 ("sessions-and-recovery.md","Multiple machines"): ("NOCODE","placement and credentials policy"),
 ("sessions-and-recovery.md","Recovery"): ("PROCESS","boot procedure, partially probed via the lease claims"),
 ("sessions-and-recovery.md","Common failures"): ("ADVICE","symptom table"),

 ("waking.md","The wake gate"): ("PROBED",["#H1 wake gate tolerates a corrupt snapshot", "UNCHANGED when nothing moved","CHANGED when an item moves","an unreadable source is CHANGED","max_quiet forces a pass","a companion (persist=False)","a stale snapshot save is refused","#8.3 an unreadable snapshot still produces a pass"]),
 ("waking.md","Durable timers"): ("PROBED",["a timer found in sending","a due timer on a closed row","lenient due parsing","20 concurrent timer adds","3 overlapping timer ticks","timer ids are unique","#8.5 the id redraw is bounded"]),
 ("waking.md","Recovering missed wakes"): ("NOCODE","sweep over the runner's own schedules; needs the runner API"),
 ("waking.md","The event feed"): ("NOCODE","events.jsonl is documented, not implemented"),
 ("waking.md","Three kinds of scheduled thing"): ("ADVICE","taxonomy for the operator"),
}

FILE_ISSUES = {"FILED": "#6 (open) plus #3 follow-ups"}

def sections(doc_dir):
    out=[]
    for f in sorted(doc_dir.glob("*.md")):
        for line in f.read_text().splitlines():
            if line.startswith("## "):
                out.append((f.name, line[3:].strip()))
    return out

def main():
    doc_dir = pathlib.Path("docs")
    report = {r["claim"]: r["verdict"] for r in json.loads(pathlib.Path("claims-report.json").read_text())}
    found = sections(doc_dir)
    unclassified, bad_citation, counts, known_failing = [], [], {}, []
    for doc, sec in found:
        entry = MANIFEST.get((doc, sec))
        if entry is None:
            unclassified.append(f"{doc} :: {sec}"); continue
        verdict, ev = entry
        counts[verdict] = counts.get(verdict, 0) + 1
        if verdict in ("PROBED","FILED"):
            for frag in ev:
                hits = [c for c in report if frag in c]
                if not hits:
                    bad_citation.append(f"{doc}::{sec} cites '{frag}' (no such claim)")
                elif any(report[h] != "PASS" for h in hits):
                    failing = [h for h in hits if report[h] != "PASS"]
                    # a FILED section is allowed to cite a defect that is still open; PROBED is not
                    if verdict == "PROBED":
                        bad_citation.append(f"{doc}::{sec} cites '{frag}' (failing: {failing})")
                    else:
                        known_failing.extend(failing)
    staged = 0
    for doc in sorted({d for d,_ in found}):
        rows=[(s,MANIFEST.get((doc,s))) for d,s in found if d==doc]
        print(f"\n{doc}")
        for s,e in rows:
            if e is None: print(f"   UNCLASSIFIED  {s}"); continue
            v,ev=e
            mark={"PROBED":"probed ","NOCODE":"no-code","ADVICE":"advice ","PROCESS":"process","FILED":"filed  "}[v]
            detail = f"{len(ev)} claims" if v=="PROBED" else (FILE_ISSUES.get("FILED") if v=="FILED" else ev)
            print(f"   {mark}  {s[:58]:58} {detail}")
            if "--verbose" in sys.argv and v in ("PROBED","FILED"):
                for frag in ev:
                    hits=[c for c in report if frag in c]
                    for h in hits:
                        print(f"               {'ok  ' if report[h]=='PASS' else 'FAIL'} {h}")
    print(f"\n{len(found)} sections: " + ", ".join(f"{k}={v}" for k,v in sorted(counts.items())))
    print(f"unclassified: {len(unclassified)}", *unclassified, sep="\n  " if unclassified else " ")
    print(f"bad citations: {len(bad_citation)}", *bad_citation, sep="\n  " if bad_citation else " ")
    print(f"known-failing claims cited by FILED sections (expected, tracked in the open issue): {sorted(set(known_failing)) or 'none'}")
    return 1 if (unclassified or bad_citation) else 0

if __name__ == "__main__":
    sys.exit(main())
