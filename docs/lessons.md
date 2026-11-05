# Pitfalls and lessons

Each of these happened at least once in a real fleet. They are written as general rules.

## Coordination

- **A worker's "verified" is not proof.** Check one artifact yourself before closing: the CI conclusion on the head SHA, the read-back, a fresh request to the live flow.
- **A send is in flight, not done**, until the target's reply or log shows the turn finished.
- **Write the row before archiving the worker, never in the same parallel batch.** If the row write fails a check, the archive has already happened and the row is owned by a dead worker.
- **Bulk "archive finished" buttons kill waiting workers.** A worker that ended on a WAIT line with a live background watch looks "finished".
- **Archiving a parent can cascade to every child.** Never archive the coordinator.
- **Merging can delete the worker's workspace.** Worktree pruners act soon after a merge; post-merge checks belong in a separate workspace.
- **One owner per item, stack, port and file set.** When two sessions claim the same thing, pick one and tell the other in the same pass.
- **Five status stores drift into five truths.** One ledger, one generated dashboard, history in the tracker. Before adding a tracker or monitor, check whether an existing one should do the job.
- **Run the written plan.** For multi-phase work, drive each pass from the plan's phases and its "done when" section. Fixing incidents along the way is not plan progress.

## Waking and timers

- **Schedulers can drop wakes for a busy agent and call them completed.** Only the run record shows the difference. Deliver timed steps by queued message and sweep for misses.
- **Promises in a transcript are not timers.** "I'll check at 14:40" means nothing unless a durable timer exists.
- **A companion that saves the shared snapshot steals the primary's wake.** Only the lease holder saves.
- **A broken collector must not look quiet.** Unreadable means CHANGED.
- **Timers on closed rows should be dropped, not delivered.** Put reminders on open rows.

## Merges and deploys

- **A merge gate that readies drafts will merge drafts.** Holds must be explicit text.
- **A merge can deploy.** Check the repo's auto-deploy setting before "merge but don't deploy".
- **CI on the previous commit means nothing.** Evaluate and merge against one head SHA with an expected-head guard.
- **Review bots stall and change their output format.** Request once per SHA, wait generously, parse defensively, treat unknown as not clean.
- **Phrase lists overlap.** "No major issues" contains "issue", so whichever list is checked first decides, and a receipt that says "looks good to merge" and names a P1 can read as clean. Prefer an explicit verdict line; when matching phrases, strip the clean phrases before looking for findings.
- **A required check that never reported looks like no check at all.** The gate only sees the checks it is handed; list each repo's required checks and wait for all of them.
- **Conditional holds stay held.** A script cannot evaluate "after the migration lands".
- **Cancelled and timed-out are not green.** Neither is a watcher process exiting 0.

## Secrets and shell

- **Never put secrets on the command line or in heredocs.** Arguments leak through process lists; a heredoc replaces stdin and an unquoted one runs `$(...)` and backticks. One script file per remote call.
- **Backticks inside an unquoted heredoc execute.** A brief written with an unquoted heredoc ran a command quoted in its own text. Write briefs with a file-writing tool or a single-quoted heredoc.
- **Shell tracing prints headers.** No `-x` on scripts that build auth headers; debug from saved, redacted output.
- **"Names only" via a line filter still leaks.** Multi-line values print their continuation lines. Use a parser.
- **Deploy-platform APIs often echo environment variables in responses.** Print named fields only.
- **Read-only briefs need the secret rules too.** The leaks came from tasks that "only read status".
- **Secret stores fail closed.** Locked, empty or timeout means skip and wait for the owner, not retry in a loop.
- **Argument parsing that reads "the value after `--out`" will happily take the next flag or nothing.** Scripts with an output-path flag must fail when it is missing; one overwrote an interpreter binary.
- **Database clients can double-encode JSON.** Casting a serialized string to a JSON column may store a string, not an object. Round-trip test writes.

## Approvals and people

- **Don't self-approve a substitute.** If the owner's chosen option is infeasible, ask a new question.
- **Relays are not direction.** Another bot saying "the owner approved" is not the owner approving.
- **Calendar writes are messages to other people.** Keep them with the owner.
- **Agents never click Buy.** Stop on the payment page, name the button.
- **Look facts up yourself.** Public facts (a vendor's pricing page, an API's limits) are a search away; asking the owner wastes their attention.

## Infrastructure

- **Split host proofs.** One host per command; never chain a proof after a command known to exit non-zero.
- **Health checks are not user-flow checks.** A readiness 200 can coexist with a broken login.
- **Old aliases outlive their hosts.** After migrating a service, sweep scripts, docs and memory for the old host name (doc lint's retired-terms list).
- **In-memory deploy queues lose work on restart.** Know whether your deploy platform queues in memory and at what concurrency.
- **Reverse-proxy and compose projects have names.** Recreating one under a different project name can fail on name conflicts or create a duplicate.
- **Builder caches hide changes.** Cached clones and layers need explicit no-cache builds when the source changed but the build did not.
- **Prune caches, not images in use.** Aggressive "prune everything" on a shared host deletes images other stacks depend on.

## Capacity

- **Use the capacity.** Idle quota across providers and accounts is wasted. Start ready work now; spread it.
- **Route to the newest model versions only**, and re-check after releases; stale routing silently downgrades quality.
- **Usage dashboards can go stale.** If quota numbers stop moving, check the collector before trusting them.
