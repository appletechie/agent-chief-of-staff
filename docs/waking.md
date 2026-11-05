# Waking: the wake gate, timers and the event feed

An LLM coordinator that polls on a timer spends most of its budget re-reading state that has not changed, and a long context full of identical status checks makes it worse at noticing the one that did. The fix: a cheap deterministic script decides whether the model needs to run at all.

## The wake gate

A script, run at the start of every periodic wake, that:

1. Collects each part of the state a pass would act on.
2. Reduces each part to a map of `item -> short hash`.
3. Compares with the snapshot saved last time.
4. Prints `CHANGED` or `UNCHANGED` on the first line, then one line per changed part ("ledger: R-014 changed, R-019 new").
5. Saves the new snapshot (primary only).

### Parts worth hashing

| part | items | changed when |
|---|---|---|
| ledger | open row id -> hash of the row text | a row is added, removed or edited |
| due | rows whose `due` has passed | a row becomes overdue |
| agents | worker id -> status (running / idle / error / closed) | any worker changes state |
| permits | pending permission prompts | a new prompt appears |
| timers | due-but-undelivered and failed timers | a timer comes due or fails |
| reports | file names in `reports/` | a new report lands |
| intake | pending tracker items, last intake error | new item or new error |
| health | finding ids from the latest health sweep, plus a "sweep is stale" item | a **new** failing id appears (old ones do not re-wake) |

### Rules

- **An unreadable source counts as CHANGED** and keeps its previous snapshot. A broken collector must never look like a quiet system.
- **Force a pass after a maximum quiet period** (for example 4 hours), so nothing outside the hashed sources goes unchecked forever.
- **Only the lease holder saves the shared snapshot.** A companion either runs with `--no-save` or keeps its own snapshot file. Otherwise a companion's look would "consume" a change and the primary would never act on it.
- **A dropped wake never runs the gate**, so its changes simply show up on the next one. That is the property that makes the gate safe against missed heartbeats.
- On UNCHANGED, the coordinator replies with one line and re-reads nothing.

See [examples/wake_gate.py](../examples/wake_gate.py).

## Durable timers

Every step with a time in it is a timer record on disk:

```json
{
  "id": "t-3f9a1c",
  "row": "R-016",
  "name": "soak check",
  "due": "2026-01-17T09:00:00Z",
  "target": "coordinator",
  "owner": "worker-7f3a2c",
  "prompt": "R-016: the 24 h soak window ended. Check error rate and close or reopen.",
  "state": "armed",
  "attempts": 0,
  "source": "wait-line"
}
```

A tick script runs every minute from the OS scheduler and delivers due timers.

### Delivery rules

- **Deliver by message, not by starting a run.** Many agent runners reject "start a turn" on an agent that is already mid-turn, and some schedulers then mark the run failed and never retry. A queued message that is injected when the agent next becomes free does not have that problem. If your runner only offers "start a run", have the tick script retry until it succeeds.
- **At most once.** Mark the timer `sending` and persist that *before* sending; mark `delivered` with the receipt after. A timer found in `sending` on the next tick (a crash mid-send) becomes `failed` and is surfaced, never resent blind.
- **Bounded retries.** A send error retries on later ticks up to a limit (say 5), then `failed`.
- **Skip what no longer matters.** A due timer whose row is closed is cancelled with a reason, not delivered. Put reminders on open rows.
- **Check the owner when a timer fires.** If the worker it was about is closed or archived, read its last report and update the row; re-dispatch only if work remains.
- **Optional busy deferral.** If delivering into a busy session would interrupt the owner's live conversation, defer for a bounded time (say up to 30 min), then deliver anyway.
- **Lenient parsing.** An unreadable due time never fails the command; it defaults to a safe value (+60 min) and says so.

See [examples/timers.py](../examples/timers.py).

### Never use the alternatives

| tempting | why not |
|---|---|
| `sleep 3600` inside an agent turn | Dies with the session, burns a slot, invisible to everyone else |
| "I'll check back at 14:40" in the transcript | Nothing will wake the agent; compaction may delete the promise |
| A one-shot scheduled wake on the runner | Some runners drop it if the target is busy and record it as completed |

## Recovering missed wakes

If you also use the runner's own scheduler (recurring heartbeats, cron-started agents), run a small sweep every few minutes:

1. List schedules whose latest run failed and which will not run again soon.
2. Redeliver each miss once, by message, recording the run id as handled.
3. Never treat a "completed" one-shot as having run without checking its run record.

## The event feed

`state/events.jsonl` is append-only. The merge gate, intake, timer deliveries, dispatches and wake-gate CHANGED summaries each append one JSON line:

```json
{"at": "2026-01-16T14:02:11Z", "source": "merge-gate", "text": "merged org/app#88 (ISSUE-231) at abc1234"}
```

Every coordinator session reads it from its own cursor (stored per session). A hook in the agent runner prints new events at the start of each prompt, at most a dozen lines. That way a companion session sees what the primary did without polling anything. Appenders fail open: a broken feed must never break the script that writes to it.

## Three kinds of scheduled thing

Keep them distinct in your head and in your docs:

| kind | what it does | use for |
|---|---|---|
| Scheduled job | Starts a **fresh** agent on a cron | Recurring reports (morning state, weekly archive proposal) |
| Heartbeat | Sends a prompt **into an existing** agent on a cron | The coordinator's periodic pass, gated by the wake gate |
| Durable timer | A record in `timers.json`, delivered by the tick script | Every one-off timed step |
