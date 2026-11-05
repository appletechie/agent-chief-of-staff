# Sessions, multiple machines and recovery

Sooner or later there is more than one coordinator session: a second terminal, a remote session from a phone, a scheduled job that loads the same instructions. Without rules, two sessions dispatch the same work, one consumes the other's wake signals, and a stop in one does not reach the other.

## The lease

```json
{
  "primary": "coordinator-a1",
  "host": "host-a",
  "since": "2026-01-16T09:00:00Z",
  "renewed_at": "2026-01-16T14:30:02Z",
  "ttl_s": 7200,
  "epoch": 1,
  "reason": "initial",
  "previous": null
}
```

- Exactly one session is primary: the one named in `state/lease.json`.
- The primary renews the lease on every pass (the wake gate is a convenient place).
- **An expired lease is flagged, never taken over automatically.** Automatic takeover turns a slow primary into two primaries. Takeover is an explicit command with a reason, and it refuses a lease that is still held unless forced (`--force`, for a planned rollover). It records the previous holder, its host, its epoch and when it was last seen.
- **Every takeover increments `epoch`.** The epoch is a fencing token: see below.
- Everything that targets "the coordinator" (timers, gate messages, intake wakes) resolves the target from the lease at send time, not from a hard-coded id.
- **A broken lease is not "no lease".** A file that does not parse, or parses with a bad field (no primary, a `renewed_at` without a time zone, a `ttl_s` or `epoch` that is not a positive whole number), raises a named error from every read, so fenced writes refuse. Scheduled jobs read that error as "not primary here": one log line and skip. Nothing repairs it automatically; fix the field by hand, keeping the epoch. A lease written before epochs existed is still valid.

See [examples/lease.py](../examples/lease.py).

### Fencing

A lease nothing checks is advice. The case it must survive: the primary's laptop sleeps past the TTL, another session takes over, and the laptop wakes mid-turn still believing it is primary. A lock serializes both sessions' writes happily; it does not stop the stale one.

So every shared write carries the writer's epoch and is refused when it is not the lease's current epoch:

- the ledger (`write_row`, `close_row`), timers (`add`, `tick`) and the wake-gate save each take `expect_epoch`;
- the check and the write happen inside one fence: the writer holds the lease lock *shared* for the whole write (before its own store lock), and takeover, init and renew hold it *exclusive*. A takeover therefore waits for writes already past the check, and every write after it is refused. Writers don't block each other;
- lock order is lease lock, then store lock. The lease's exclusive holders never take a store lock, so this can't deadlock;
- the one exception is the timer tick, which releases the fence around each send so a takeover never waits on a slow send. After the send it re-takes the fence and re-checks the epoch before recording the outcome. The bounded window is one send: a takeover during a send can't recall a message already on its way, but the replaced tick records nothing and stops, and the new primary's next tick marks that timer failed ("not resent"), so it's surfaced and never sent twice;
- the primary gets its epoch from its renewal at the start of each pass; a companion reads it at boot;
- a refused write means "re-read the lease": the old primary finds it is no longer primary, says so, and continues as a companion.

In the sketches each store accepts `fence=lease.fenced` (a context manager that holds the lease lock shared and yields the current epoch; passed in, so the modules stay independent). Once a store has a fence, it refuses any write without the current epoch. A single-session setup can leave it unset. Passing `expect_epoch` to a store with no fence is an error, not a no-op: the caller believes it is fenced.

## Primary vs companion

| thing | primary | companion |
|---|---|---|
| Periodic heartbeat, timer delivery, gate and intake wakes | Receives them | Sees them in the event feed |
| Wake-gate snapshot | Saves the shared one | Compares only (`--no-save`) or keeps its own file |
| Ledger rows | Read/write under the lock | Read/write under the lock; owner cell records which session took it |
| Dispatch | Yes, with a claim | Yes, with a claim and the duplicate guard |
| Timers | Creates them | May create them; they are delivered to the primary |
| Answering the owner | Yes | Yes; the owner's direction is appended to the event feed with the session id so the other session sees it next turn |
| Policy edits | Records the owner's words | Never |
| Hard gate, holds, stop | Obeys | Obeys |

A companion tells the primary what it did with a short message, and the primary's next pass picks it up from the event feed anyway.

## Session registry

`state/sessions.json` holds one entry per live coordinator session: id, kind, role, host, pid, working directory, started, last seen.

- A session-start hook in the agent runner registers the session and prints the top of the boot brief and the lease.
- A per-prompt hook prints new events since the session's cursor.
- A stop hook refreshes `last_seen`.
- Entries go stale after about 30 minutes without a refresh and are pruned after a day idle. The primary is never pruned.
- **Scheduled runs and workers never register.** Detect them by a label or environment variable the dispatcher and scheduler set. Otherwise every cron-started job shows up as a "companion" and the registry fills with noise.

## The boot brief

`START-HERE.md` is regenerated every 15 minutes and on every CHANGED pass. It holds:

- where each piece of policy lives (one line each);
- open rows grouped by queue;
- armed timers;
- live workers on **every** host, with their rows;
- the latest brief;
- the lease and the registry;
- the last 20 events.

If it is older than twice its refresh period when a session starts, regenerate it before trusting it.

## Stop in one is stop in all

A stop from the owner in any session writes `state/halt.json` with the time and the session. Every session's prompt hook shows it, the dispatcher refuses while it exists, and the primary halts paid and running work as its next action (see [guardrails.md](guardrails.md)). Only the owner's fresh go removes it.

## Multiple machines

- **One primary, wherever it runs.** Workers can be spread across machines and sandboxes; the coordinator and its scripts (timers, gates, intake, boot brief) live together on one host so the lock and the files are local.
- **Every sweep covers every host.** Dead-worker sweeps, the boot brief and schedule health must list each host explicitly. A sweep that silently covers only the local machine is the most common gap.
- **Remote credentials** for reaching another host's agent runner come from the secret store into the child process's environment only, are never printed, and a locked store marks that host "unreachable" rather than retrying.
- **Placement is written policy** (see [dispatch.md](dispatch.md)), not a habit.

## Recovery

### A new session boots

1. Read the charter in full.
2. Read the boot brief (regenerate if stale), then the ledger.
3. Check the lease. If you are not primary, follow the companion rules.
4. Run the wake gate without saving to see what changed.
5. List live workers on every host.
6. Say one line: "companion of <primary>; N open rows, M running workers; last pass <time>". Or, if primary, run a pass.

### Planned handover (rollover)

Long-lived coordinator sessions get large. A rollover starts a fresh coordinator from durable state:

1. Dry run: write a handoff file (charter pointers, open rows, timers, live workers, latest brief) and the list of things that will be repointed.
2. Real run, from the current primary: create the new coordinator with the same provider, model and permission mode; write the lease; repoint armed timers, heartbeats and schedule prompts that name the old id; relabel live workers' parent; read each change back.
3. Tell the old coordinator to forward anything that still reaches it.
4. Keep the old coordinator idle. **Never archive it** if archiving cascades to its children.
5. Keep the run record; it drives the rollback (repoint everything back).

### Emergency takeover

`lease take <new id> --reason "<why>"` once the lease has expired, after confirming the old primary is really gone (no recent activity, not mid-turn); add `--force` only when the old primary cannot renew but is not yet expired. The new epoch fences the old primary if it comes back anyway. Then run a full pass.

### Where rollbacks live

- `rollback/<change>-<stamp>/` in the coordinator directory, restrictive permissions: copies of every policy, prompt or config file before a change.
- `state/rollover/`: handoffs and rollover run records.
- On hosts: a rollback directory per change, image tags like `:rollback-<stamp>`, database dumps beside each stack, each named in the row's notes.

Every row that made a change names its rollback command. If you cannot write the rollback command, the change is not in the "do, then report" tier.

## Common failures

| symptom | likely cause | fix |
|---|---|---|
| A periodic wake shows "failed" while the coordinator was busy | Runner rejects a new turn on a mid-turn agent and does not retry | Durable timers delivered by message; the next pass catches up from the snapshot |
| Boot brief is stale | Its scheduler job stopped | Regenerate by hand, then fix the job and add it to schedule health |
| Checker fails | Malformed or over-budget row | Fix through the write function before any other action |
| Dispatcher refuses | Duplicate guard hit | Take over or wait; override only with a recorded reason |
| Owner of an open row is archived | Bulk archive, cascade, or pruner | Restore guard each pass; re-dispatch if restore fails |
| Gate merged something meant to wait | Draft treated as hold; hold not written as text | Explicit hold markers in row and PR body |
| Two sessions acted on the same change | Companion saved the shared snapshot | Snapshot save refuses unless the caller holds the current lease epoch |
| A session's writes are refused with a stale epoch | The lease was taken while it slept or stalled | Re-read the lease and the state it guards before writing again; continue as a companion |
