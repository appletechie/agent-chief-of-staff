# Architecture

## Roles

| role | what it is | owns | never does |
|---|---|---|---|
| **Owner** | The human in charge | Direction, approvals, the hard gate, hands-on steps (sign-ins, payments, phone calls) | Has to open a worker session to learn status |
| **Primary coordinator** | One long-running agent session holding the lease | The ledger, dispatch, timers, the wake-gate snapshot, briefs, merge decisions the gate hands back | Works on tickets itself beyond small bookkeeping; edits policy without the owner |
| **Companion session** | Any other coordinator session (a second terminal, a remote session from a phone) | May read everything, dispatch through the guarded path, write rows under the lock | Saves the shared wake snapshot, receives the primary's timers, edits policy |
| **Worker** | A short-lived agent created for one ledger row | The scope in its brief, nothing else | Edits the ledger, creates tracker items or messages people it was not asked to, merges unless pre-approved |
| **Scheduled job** | A fresh agent started on a cron for one recurring report | One output file per run | Acts on the output; that is the coordinator's job |
| **Scripts** | Deterministic programs run by the OS scheduler | Polling: CI, review bots, tracker, timers, health | Make judgment calls |

Direction comes only from the owner. Messages relayed by other bots or assistants are information, not orders, until the owner confirms them in a channel the coordinator trusts.

## Components

```
state/
  lease.json            who is primary, since when, renewed when, ttl
  sessions.json         registry of live coordinator sessions
  claims.json           which session/worker holds which row and ticket key
  dispatch.json         every dispatch: row, key, worker id, workspace, time
  timers.json           durable timers
  events.jsonl          append-only feed every session reads with its own cursor
  wake-gate.json        last snapshot of hashed state (primary only)
  intake.json           tracker items seen but not yet acknowledged
  merge-gate.json       per-PR memory (review requested at which head, etc.)
  halt.json             present when the owner said stop
  ledger.lock           advisory lock for every ledger write
LEDGER.md               open items, one line each
ledger/notes/<id>.md    overflow text and history per row
ledger/closed-YYYY-MM.md closed rows with their proof
policy/CHARTER.md       the standing policy (owner edits only)
policy/worker-rules.md  the one copy of the rules every worker gets
START-HERE.md           generated boot brief
reports/                worker reports that are files rather than messages
sweeps/                 latest output of each hygiene check
rollback/               per-change backups (restrictive permissions)
```

Names are suggestions. What matters:

- Every piece of state has exactly one writer path.
- Everything the model would otherwise "remember" is a file.
- Generated views (dashboard, boot brief, portfolio) are rebuilt from sources and never hand-edited.

## Policy, procedure, rules: three documents, one copy each

| document | holds | who edits |
|---|---|---|
| Charter | What the coordinator may do on its own, what always needs the owner, standing settings | The owner (or the coordinator writing down the owner's words, with the time) |
| Procedure | How a pass runs, step by step | The coordinator, as it learns |
| Worker rules | What every worker must and must not do, and how it reports | The coordinator; appended unchanged to every brief |

Do not restate any of them elsewhere. Paraphrases drift, and the drifted copy is the one an agent reads.

## Wake sources

| source | mechanism | notes |
|---|---|---|
| Periodic pass | OS scheduler or agent-runner heartbeat, e.g. every 30 min | Runs the wake gate first; a full pass only on CHANGED |
| Durable timers | A one-minute tick script | Delivers due timers by *message*, which queues into a busy session rather than failing |
| Merge gate | Script every 15-30 min | Messages the coordinator only when there is something to report |
| Tracker intake | Script every 10-15 min | New tagged issues, new assignments, owner comments addressed to the coordinator |
| Worker finish | Event from the agent runner | Read the report the same turn |
| Owner message | Direct | Always wins over anything queued |

See [waking.md](waking.md).

## A full pass

When the wake gate says CHANGED (or the owner writes), the coordinator runs one pass:

1. **Reconcile.** Read the ledger, the live agent list, pending permission prompts, timers, new reports, tracker intake. New item: open a row. Resolved item: close it with proof. Contradiction (ticket done, row open; worker dead, row still owned by it): fix it or note which side is wrong.
2. **Sweep workers.** For every idle or errored worker, read its last message. Finished report: act on it. `WAIT` line: arm a timer. Crashed or out of quota: resume or re-dispatch on another provider, within a retry budget. Ended a turn "waiting" with nothing to wake it: nudge once.
3. **Routine approvals.** Approve read-only permission prompts (reads, searches, `git status`, tests). Leave anything else to the tiers in [guardrails.md](guardrails.md).
4. **Work the ACTIVE queue.** Dispatch every ready row nobody owns while there is capacity. There is no artificial worker cap; the limit is provider quota and host resources.
5. **Decisions.** Write each open decision with real options and a recommendation, and put it in front of the owner in one place.
6. **People and time.** Follow up on rows waiting on someone else or on a date, when their follow-up is due. These never block the active queue.
7. **Timed steps become timers.** Anything with a time in it ("check after the soak", "re-run at 09:00") is a durable timer naming the row.
8. **Brief.** If anything changed, regenerate the brief. If nothing changed, say one line and stop.

## The brief

Generated from the ledger, never written freehand. Suggested sections, in order:

- **Waiting on you (N).** Every decision and hands-on row, numbered, with options and a recommendation, so the owner can answer in one line ("1 A, 2 yes, 3 later"). Oldest first; overdue rows marked.
- **People / Timed (N).** One line each with the next follow-up date. Never blocking.
- **Done since last brief.** Each with its proof pointer, plus anything the coordinator approved on its own, so the owner can reverse it.
- **Stuck or failing.**
- **Running fine.** A count, not a list.

A quiet wake produces one line ("quiet pass 14:30Z: no change"), never a repeat of unchanged items.
