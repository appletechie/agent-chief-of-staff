# Agent Chief of Staff

A design for running many AI coding and operations agents without losing track of them.

One long-running **coordinator** agent sits between a human owner and a fleet of short-lived **worker** agents. It keeps a durable ledger of open work, starts workers with precise briefs, refuses duplicate work, gates merges and deploys, checks results for itself, and reports to the owner in a short brief. Cheap deterministic scripts do the polling; the language model only wakes when something actually changed.

This repository is a design document plus small reference sketches. It does not depend on any particular agent runner, model vendor, issue tracker, VCS host or scheduler. Every component is described by its contract, so you can build it on whatever you already run.

---

## Why this exists

Once you run more than three or four agents at a time, the failure modes stop being "the model wrote bad code" and become coordination failures:

- Two agents open competing pull requests for the same ticket.
- A reminder ("check the deploy at 14:40") lived in a conversation that got compacted, and nobody checked.
- An agent says "verified" and the thing is not, in fact, working.
- A merge bot merges a PR that was meant to wait for a human, and the repo deploys on merge.
- A secret is printed into a transcript because a "read-only" task did not get the secret-handling rules.
- The coordinator was mid-turn when its scheduled wake-up fired, the scheduler dropped it, and the record says "completed".
- The owner has to open six sessions to learn what is waiting on them.

The design below is a set of small, boring mechanisms that close each of these gaps. None of them is clever. Together they make an agent fleet something you can leave running overnight.

## The core loop

```
        +-------------------------------------------------------------+
        |                        Owner (human)                        |
        |  direct messages, tracker issues tagged for the coordinator |
        +-----------------------------+-------------------------------+
                                      |               ^
                          direction   |               |  brief: waiting on you,
                                      v               |  done (with proof), stuck
        +-----------------------------+---------------+---------------+
        |                     Coordinator (primary)                   |
        |  holds the lease; wakes only when the wake gate says so     |
        +----+----------------------+-----------------------+---------+
             |  read/write          |  dispatch             ^ finish notices,
             |  under a lock        |  (guarded)            | timers, gate reports
             v                      v                       |
     +-------+--------+    +--------+---------+    +--------+----------+
     |     Ledger     |    |  Dispatcher      |    | Scheduled scripts |
     | one row per    |    |  router, dup     |    | wake gate, timers,|
     | open item      |    |  guard, claims,  |    | merge gate,       |
     | + notes        |    |  workspace, brief|    | intake, hygiene   |
     | + closed/proof |    +--------+---------+    +--------+----------+
     +----------------+             |                       ^
                                    v                       |
                      +-------------+-------------+         |
                      |  Workers (isolated        |         |
                      |  workspace per task)      |         |
                      +-------------+-------------+         |
                                    |                       |
                                    v                       |
                      +-------------+-------------+         |
                      |  PRs, infra changes,      +---------+
                      |  reports, tracker comments|  CI, review bot,
                      +---------------------------+  live checks, read-backs
```

1. The owner gives direction, either directly or through a tagged issue in the tracker.
2. The coordinator records it as a ledger row: what happens next, who does it, what it waits on.
3. The dispatcher starts a worker for the row, after checking nobody else is already on it.
4. The worker does the job in its own workspace and ends with a fixed-format report, or a `WAIT` line naming what it is waiting for.
5. Scheduled scripts watch CI, review bots, timers and the tracker. They wake the coordinator only when there is something new.
6. Merges and deploys pass a gate. Nothing closes without proof the coordinator checked itself.
7. The coordinator sends a brief: what needs the owner, what finished (with proof), what is stuck.

## Principles

1. **Files are the record, the conversation is not.** Any session must be able to rebuild full state from disk and live systems. Context windows get compacted; ledgers do not.
2. **Scripts poll, the model decides.** Polling with an LLM is slow, expensive and forgetful. A deterministic script hashes state and wakes the model on change.
3. **One system per job.** One ledger, one dashboard (generated from the ledger), one place for history, one owner per item. Parallel trackers drift.
4. **Done means proven.** A row closes on evidence the coordinator read itself: an exit code with a count, a CI conclusion on the head commit, a read-back, a live request. A worker's "verified" is a claim, not proof.
5. **Guard the irreversible, automate the reversible.** Anything with a backup, a read-back and a one-command rollback can run without asking. Money, people outside the team, production data, secrets and deletes always go to the owner.
6. **Every timed step is a durable timer.** Never an in-memory sleep, never a one-shot wake that silently fails when the target is busy.
7. **Single writer, many readers.** One primary coordinator holds a lease. Other sessions may read everything and write only through locked paths.

## Documentation

| doc | covers |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Roles, components, state layout, the pass procedure |
| [docs/ledger.md](docs/ledger.md) | Row format, queues, notes, closing with proof, the checker, generated views, timestamps |
| [docs/waking.md](docs/waking.md) | The wake gate, durable timers, missed deliveries, a busy coordinator, the event feed |
| [docs/dispatch.md](docs/dispatch.md) | Routing and placement, workspaces, the duplicate guard, briefs, the WAIT protocol, finishing and archiving |
| [docs/guardrails.md](docs/guardrails.md) | Action tiers, the hard gate, approvals and holds, purchases, outside contact, secrets, destructive ops, infra changes, stop |
| [docs/merge-gate.md](docs/merge-gate.md) | The merge decision, holds, deploy-on-merge repos, review bot parsing |
| [docs/intake-and-hygiene.md](docs/intake-and-hygiene.md) | Tracker intake, the backlog pump, stuck sweeps, drift, schedule health, doc lint, dead workers, lesson recall |
| [docs/sessions-and-recovery.md](docs/sessions-and-recovery.md) | Lease, companions, session registry, boot brief, stop-everywhere, rollover, rollbacks |
| [docs/adapting.md](docs/adapting.md) | Mapping each component to your tools; minimal vs full build; what to build first |
| [docs/lessons.md](docs/lessons.md) | Pitfalls found in production, generalized |

## Examples

Small, dependency-free Python sketches (standard library only, POSIX file locks). They are meant to be read and adapted, not imported as a framework.

| file | what it shows |
|---|---|
| [examples/LEDGER.example.md](examples/LEDGER.example.md) | A ledger file with rows in every queue |
| [examples/ledger_store.py](examples/ledger_store.py) | Locked row writes, spill to notes, close with proof, never-reused ids, queue assignment, the checker |
| [examples/wake_gate.py](examples/wake_gate.py) | Hash durable state, compare to the last snapshot, decide CHANGED / UNCHANGED |
| [examples/timers.py](examples/timers.py) | Durable at-most-once timers (locked adds, one tick at a time) and the `WAIT` line parser |
| [examples/dispatch_guard.py](examples/dispatch_guard.py) | The duplicate-dispatch guard with locked, test-and-set claims |
| [examples/merge_gate.py](examples/merge_gate.py) | A pure merge decision function |
| [examples/lease.py](examples/lease.py) | Primary lease: init, renew, explicit takeover, and the epoch that fences a replaced primary |
| [examples/worker-brief.md](examples/worker-brief.md) | The brief template every worker gets |
| [examples/worker-rules.md](examples/worker-rules.md) | The shared worker rules appended to every brief |
| [examples/test_examples.py](examples/test_examples.py) | Unit tests for the sketches |
| [examples/test_durability.py](examples/test_durability.py) | Concurrency (threads and processes) and lease-fencing tests |

Run the tests:

```sh
cd examples
python3 -m unittest -v test_examples test_durability
```

## Minimal viable version

You can get most of the value in a day:

1. A `LEDGER.md` with six columns and a checker script.
2. A brief template plus one shared rules file, appended to every worker prompt.
3. A duplicate guard that searches open PRs for the ticket key before starting a worker.
4. A timers file and a one-minute cron that delivers due timers.
5. A rule, enforced in the coordinator's instructions: no row closes without a quoted proof.

Then add the wake gate, the merge gate, intake and hygiene as the fleet grows. [docs/adapting.md](docs/adapting.md) has the full build order.

## Status

This is a design distilled from running a coordinator over dozens of concurrent workers across several machines. The sketches are deliberately small. Contributions that keep them small and vendor-neutral are welcome.

## License

MIT. See [LICENSE](LICENSE).
