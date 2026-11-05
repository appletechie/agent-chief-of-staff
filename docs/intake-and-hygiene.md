# Intake, the backlog pump and hygiene

Work should flow in without the owner having to ask, and rot should be found by scripts before it is found by the owner. Same split as everywhere else: scripts check, the model judges.

## Tracker intake

A script every 10-15 minutes polls the issue tracker and records new items in `state/intake.json` until the coordinator acknowledges them. New means "not already tracked by an open or closed ledger row" and one of:

| reason | rule |
|---|---|
| inbox | Open issues carrying a coordinator label (for example `coordinator-inbox`), new or updated since the last poll |
| assigned | Issues created for or newly assigned to the owner since the last poll |
| comment | A comment **by the owner** that starts with the coordinator's name or mentions it, on an open issue assigned to the owner or labelled for the coordinator (tracked issues included, since a comment can change direction on existing work) |

Rules:

- **Ignore your own comments.** If the coordinator writes tracker comments through the owner's account, intake must recognize them (they cite a row id, or open with a fixed report phrase) or it will wake itself in a loop.
- **Act only on the owner's words.** An inbox item counts as direction when it quotes the owner. Anything else is information.
- **Cursor with overlap.** Store the last poll time and re-read a couple of minutes before it each run to cover clock skew; dedupe by issue and comment id.
- **Errors wake too.** A tracker failure is recorded as `last_error` and wakes the coordinator once per distinct error, so a silently broken intake does not look like a quiet inbox.
- **Ack explicitly.** The coordinator runs `intake ack ISSUE-231 ISSUE-240` once it has opened a row or decided to drop the item. Until then the item keeps the wake gate's `intake` part non-empty.

## The backlog pump

A scheduled job (every few hours in working hours) that pulls ready work into workers:

1. List tracker items in "to do" on the owner, with enough labelling to know the repo, that no ledger row tracks.
2. List stuck rows: ACTIVE rows owned by `coordinator` (nobody is on them), or owned by a dead worker.
3. For each, plan and dispatch through the normal dispatcher, so the duplicate guard applies.
4. Never merge, never message people, never touch hard-gate items. Write one report file.

The pump turns "the owner forgot to ask" into "it was already in progress".

## Drift detection

A read-only report, daily:

- Open tracker issues on the owner whose key appears only in **closed** ledger rows (the row closed, the ticket did not).
- Untracked open issues older than N days.
- Status mismatches (ticket "In review" with no open PR; row says merged, ticket still "In progress").

Fixes go through the tracker housekeeping guardrail: status moves only with a proof comment; due dates and assignees never change automatically; bulk edits touch only named keys and post a key / old / new table.

## Schedule health

Everything scheduled is a silent failure waiting to happen. A deterministic, read-only sweep, daily, over:

| source | checks |
|---|---|
| Agent-runner schedules and heartbeats, on every host | Failed runs in the window; prompts naming closed rows; active schedules with no open row; schedules that stopped firing; the coordinator's heartbeat not targeting the lease holder |
| OS schedulers (cron, launchd, systemd timers) on every machine | Last exit code; keep-alive jobs not running; definitions not loaded; logs older than the job's period; failed units |
| CI scheduled workflows | Last 3 completed runs failed; last 10 all skipped |
| Serverless / edge crons | Last scheduled invocation status |
| Durable timers | Armed timers whose target is not the lease holder; failed timers |
| HTTP-calling crons | 5xx responses in the window, counted from logs |

Rules:

- **A check that cannot complete is "skipped", never "ok".**
- **Findings carry stable ids** so the wake gate's `health` part fires only on new ones.
- **Only names, counts, paths and times leave a host.** One read-only script per host.

## Doc lint

The rules agents read (global instructions, per-project instruction files, the charter, the worker rules, skills, memory notes, schedule prompts) rot as systems change. A daily, deterministic lint:

- **Retired terms.** A list of names that must not appear any more: decommissioned hosts, retired tools, old model versions, superseded process names. Each with the replacement.
- **Canonical facts.** A small file of facts with a single correct value (who is primary, where workers run, the current production host). Flag contradictions; re-read facts that have a live source.
- **Broken references.** Every path mentioned must exist (skip lines about remote hosts).
- **Skill hygiene.** Duplicate or divergent copies of the same skill across harness directories; broken links.
- **Memory index.** Every note is linked from the index; every link resolves.
- An allow-list for intentional mentions.

Report only findings new since the last run. A monthly judgment pass (a model) reads the lint output and proposes one fix PR per repo plus a charter diff for the owner.

## Dead-worker sweep

Hourly, across **every** host, classify each idle or errored worker from its last message:

| class | signal | action |
|---|---|---|
| DONE | A finished report | Coordinator reads it and closes or updates the row |
| WAITING | A WAIT line or clearly waiting text | Check a timer exists; if nothing will wake it, nudge once |
| ASKS | A question to the coordinator or owner | Lift it into the right queue |
| LIMIT | Usage limit or model capacity error | Move the scope to another provider; stop the old session |
| DEAD | Connection errors, "session closed", API errors | Resume with "check live state first", or re-dispatch within the budget |

Nudges are one per (worker, last line), recorded, so a stuck worker is not spammed.

Also weekly: a read-only archive proposal across all hosts (closed workers whose rows are closed, empty workspaces with no open row, keep-list validity).

## Lessons and memory

Hard-won gotchas are worth more than any prompt engineering. Keep them:

- **As short notes**, one per lesson: what happened, why it matters, how to apply it. Index them in one file the coordinator loads every session.
- **Recalled into briefs.** At dispatch, query the memory store with the task's repo and key and append a few relevant facts as a "Lessons" section, filtered for secrets, fail-open if the store is unavailable.
- **Updated when the owner corrects you.** A correction is a lesson; write it down the same turn.
- **Linted.** Lessons reference systems that change; doc lint covers them.

## Observability findings

If you run something that analyzes agent sessions and files findings, triage them daily: verify each new high-severity finding against live state, resolve only on evidence (a merged fix, a passing run, a corrected file), and route the rest to the owning row. Do not resolve a finding because the worker said so.
