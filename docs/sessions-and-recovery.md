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
  "reason": "initial"
}
```

- Exactly one session is primary: the one named in `state/lease.json`.
- The primary renews the lease on every pass (the wake gate is a convenient place).
- **An expired lease is flagged, never taken over automatically.** Automatic takeover turns a slow primary into two primaries. Takeover is an explicit command with a reason.
- Everything that targets "the coordinator" (timers, gate messages, intake wakes) resolves the target from the lease at send time, not from a hard-coded id.

See [examples/lease.py](../examples/lease.py).

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

`lease take <new id> --reason "<why>"`, after confirming the old primary is really gone (no recent activity, not mid-turn). Then run a full pass.

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
| Two sessions acted on the same change | Companion saved the shared snapshot | Snapshot save refuses unless the caller holds the lease |
