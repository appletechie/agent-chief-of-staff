# The ledger

The ledger is the coordinator's memory. It is a plain Markdown table with one row per open item, small enough to read in full every pass.

## Row format

```
| id | key | owner | next action | waits on | due |
|---|---|---|---|---|---|
| R-014 | ISSUE-231 | worker-7f3a2c | Fix the retry loop in the sync job; draft PR | — | — |
| R-015 | — | owner | Pick a pricing option for the starter plan (Q-9) | owner: decision | 01-16 |
| R-016 | ISSUE-240 | coordinator | Re-run the soak check after the 24 h window | time | 01-17 09:00Z |
```

| column | meaning | rules |
|---|---|---|
| `id` | Stable row id | Pattern like `R-001`. Allocated from the highest id in the open ledger **and** every closed archive. Never reused, never renumbered |
| `key` | Issue-tracker key, or `—` | At most one key per row. A new key gets a new row |
| `owner` | Who acts next | A live worker's full id, `owner` (the human), or `coordinator`. If a worker died, `coordinator (owner closed: <short id>)` until re-dispatched |
| `next action` | The very next step | One sentence, imperative. Never empty |
| `waits on` | What blocks it, or `—` | Drives the queue (below). First clause wins; later steps start with "then" |
| `due` | When it should next be looked at, or `—` | `MM-DD`, `MM-DD HH:MMZ`, or ISO date. Always an explicit time zone |

### Size limits

- Keep a row under about 500 characters, owner cell excluded. Treat it as a warning, not an error: a refused write in the middle of a close-out is worse than a long row.
- The write function spills anything longer into `ledger/notes/<id>.md`, then cuts each over-long cell (next action first, then waits on) and leaves a pointer to the note in the cut cell. In `waits on` it never cuts a clause that starts with "then": it cuts the longest other clause from its end, so the queue and the later step (often the owner's) both still show.
- History, caveats, side findings and proofs go in notes or as tracker comments, never as new rows.

### When to open a row

Only for: work the owner asked for, a decision or hands-on step that blocks that work, or a timed gate. Side findings go into the notes of the row that found them. A follow-up closes its source row with a pointer to the new id.

## Queues

Every open row is in exactly one queue, derived by a script from `owner` and the first clause of `waits on`. The script is the authority; the coordinator never assigns queues by hand.

| queue | when | worked how |
|---|---|---|
| ACTIVE | A worker or the coordinator can act now (`waits on` names a worker or the coordinator, or is empty on a row the owner does not hold) | Dispatched this pass |
| DECISION | Needs the owner's yes or choice, including a row the owner holds with an empty `waits on` | Goes into the brief with options and a recommendation; optionally pre-screened by a decision model with a confidence threshold, never for hard-gate items |
| HANDS | Only the owner's hands can do it (a dashboard click, a sign-in, a phone call) | One consolidated click list with direct links |
| PEOPLE | Waits on a named person other than the owner | Has a follow-up date; never blocks |
| TIME | Waits on a timer, date, soak window or event | Has a timer; never blocks |

A worker-owned row whose later step needs the owner stays ACTIVE, and the brief still lists the owner's part. A row the owner holds with nothing in `waits on` is DECISION, never ACTIVE: no pass dispatches a worker onto the owner's own task, and the brief asks which it is (a choice, or hands-on).

## Writes go through one function, under one lock

```
write_row(row_id, fields)       # create or update, under state/ledger.lock
close_row(row_id, proof)        # move to ledger/closed-YYYY-MM.md, proof required
next_free_id()                  # scans open + closed
check()                         # structural checker, exit non-zero on error
```

- Take an exclusive advisory lock (`flock`/`fcntl` on POSIX, a lock file with O_EXCL elsewhere, or a database row lock) for every write, including dashboard regeneration that reads the ledger.
- Write to a temp file in the same directory and rename over the original. Never leave a half-written ledger.
- Make the lock re-entrant within a process and record the holder's pid and script name in the lock file, so a timeout can say who is holding it.
- After every write, run the checker and regenerate the dashboard. If the checker fails, fix the row before doing anything else in the same turn (no sends, no archives, no closes).
- Never hand-edit a row while another session is live.
- With a lease, every write carries the writer's lease epoch and is refused when the lease has moved on. The check and the write share one fence (the lease lock, held shared, taken before the ledger lock), so a takeover can't land between them (see [sessions-and-recovery.md](sessions-and-recovery.md#fencing)).

See [examples/ledger_store.py](../examples/ledger_store.py).

## The checker

A small deterministic script that fails loudly on:

- wrong column count or header;
- malformed or duplicate ids, or an id that also appears in a closed archive;
- a key cell that is not one key or `—`;
- an empty next action;
- an owner that is not a live worker, `owner`, or `coordinator` (with a live-agent lookup when available; skip the lookup offline rather than guessing);
- a due cell that does not parse.

It warns, without failing, on over-budget rows and on rows without a key where your policy requires one (for example any worker-owned row, since the worker's branch and PR need it).

## Closing with proof

A row closes only by moving to `ledger/closed-YYYY-MM.md` with a proof pointer the coordinator read itself. The archive line is written first, then the ledger line is removed. An id in both the ledger and the archive means a close crashed after the archive write: re-run the close or delete the ledger line; never re-append the archive line. Acceptable proofs:

| claim | proof |
|---|---|
| Tests pass | The command, exit code 0 and the summary line with counts, from this run |
| CI is green | The CI run id with `completed / success` **on the head commit SHA** you are merging or merged |
| Merged | The merge commit SHA |
| Deployed | Deploy id plus a fresh readiness check and one pass of the user flow that was broken |
| Config changed | The setting read back after the write |
| Message sent | The message or comment id from the API response |
| Owner decided | The owner's words, verbatim, with the time |

Not proof: a worker's "verified", a file's modification time, an HTTP 200 on a page that might be an error page, a watcher process that exited 0, a CI run on a different commit, a cancelled or timed-out step.

If something cannot be checked, the row says "unconfirmed" and why. That is a valid state; a false "done" is not.

## Exact timestamps

Rows and notes touching production, money or security carry the real UTC minute from the system clock (`date -u`), not a remembered or rounded time, and never a placeholder like `xx:xxZ`. Elsewhere a date is enough. Always write the zone.

## Generated views

| view | built from | refresh |
|---|---|---|
| Dashboard (kanban, progress board, whatever your runner offers) | The ledger, through a fixed mapping | After every ledger write |
| Boot brief (`START-HERE.md`) | Policy headers, open rows by queue, armed timers, live workers on every host, latest brief, lease, recent events | Every 15 min and on every CHANGED pass |
| Portfolio (optional) | Tracker, VCS, deploy platform, ledger | Daily; a failed source keeps its last copy, marked STALE |

Nothing generated is hand-edited. If a view is wrong, fix the source or the generator.

## History lives elsewhere

The ledger is an index, not a journal. Put the story of an item in tracker comments (proof, caveats, what was tried) or in its notes file. A tracker issue moves to done only with its caveats written on the issue.
