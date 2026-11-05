# Dispatch

Every worker starts through one entry point. Starting agents by hand "just this once" is how duplicates, unowned rows and missing rules happen.

```
dispatch --row R-014|auto --key ISSUE-231 --action "Fix the retry loop" \
         --repo <path or url> --kind code|read --brief-file brief.md \
         [--host host-a] [--pin provider/model] [--role plan|exec|review] \
         [--check-by +45m] [--open-row "next action text"] [--readonly] [--dry-run]
```

Steps, in order. Each one can refuse; a refusal names its evidence.

1. **Validate inputs** (check-by parses, row exists or `--open-row` given, the row does not already track a different key).
2. **Halt check.** If `state/halt.json` exists, refuse.
3. **Duplicate guard** (below).
4. **Route**: pick host, provider and model.
5. **Workspace**: reuse or create an isolated one.
6. **Compose the brief**: task brief + shared worker rules + recalled lessons.
7. **Create** the worker with labels naming the row, key and role.
8. **Record**: set the row's owner, append to `dispatch.json`, write the claim, append an event, arm a check-by timer.
9. **Check and regenerate**: ledger checker, dashboard.

Always run with `--dry-run` first when doing it by hand.

## Routing and placement

Placement is policy, not a per-dispatch whim. Write it down as a table:

| work | where | why |
|---|---|---|
| Code in hosted repos | Sandboxed cloud workers that clone, push a branch and open a draft PR | Isolation, parallelism, no local state |
| Web research, live-page reads | A host with web egress | Sandboxes are often allow-listed |
| Steps needing the owner's signed-in browser | Ask the owner first | A stored cookie is not proof of a live session, and acting as the owner is sensitive |
| Repos with special access | The host that holds that access | Avoid spreading credentials |

Model choice rules that worked:

- Route to the newest version of each model family only. Pin a model only for a deliberate reason (a strong planner, a second-vendor reviewer).
- Read live quota before routing; skip providers above about 80% of their window.
- Spread workers across providers and accounts that have headroom. Idle capacity is wasted capacity.
- Log each routing decision (no task text, no credentials) so you can audit it later.

Role-based routing is a useful pattern: **plan** (strong reasoning model writes a plan with a "Done when" section), **exec** (sandboxed coder implements it), **review** (a model from a different vendor reviews the PR). Chaining the three per ticket gives you a "swarm" that never merges on its own; the merge stays with the gate.

## Workspaces

- **One isolated workspace per task.** For code: a worktree or fresh clone on a branch named after the ticket key (`ISSUE-231-retry-loop`). For read-only work: a plain local workspace.
- **Reuse** the workspace a row or key already used if it is still live, instead of creating another.
- The coordinator's own directory accepts only read-only or bookkeeping workers.
- **Post-merge steps do not belong in the merged worktree.** Pruners delete merged worktrees, and a worker still inside one dies. Do post-merge and post-deploy checks in a separate read-only workspace, or keep-list the worktree until the row closes.
- Archive the workspace when its row closes.

## The duplicate guard

Before creating anything, refuse if any of these is true:

| check | source | why |
|---|---|---|
| An open PR names the ticket key, **by anyone** | VCS search across every org you work in, title and body | A teammate or another agent is already on it. Searching only your own PRs misses exactly the case that hurts |
| The ticket is assigned to someone other than the owner or the coordinator | Issue tracker | Someone else owns it |
| A live worker already carries the row or the key | Agent labels on every host | Two workers, one job |
| Another session holds a live claim on the row or key | `claims.json` (with a TTL), written under a lock; the claim itself refuses a name another session holds, so two sessions that pass the guard at the same moment cannot both claim. A claim with a bad field (no session or worker, an `at` without a time zone) can't be aged out, so it refuses that name until a person repairs it | Two coordinator sessions dispatching at once |
| A dispatch for the same key happened recently from another session | `dispatch.json` | Belt and braces for claims that expired |

Rules:

- **A failed check is a warning, never a silent pass.** If the VCS search errors out, say so in the output. Decide explicitly whether that blocks.
- **Override only with a reason**: `--allow-duplicate "taking over: original worker died at 14:10Z"`. The reason is recorded with the dispatch.
- A second worker on the same row is allowed only with an explicit flag (`--add-worker`); the first stays the owner of record.
- Test the guard by replaying the incident that motivated it (an open PR by a teammate must refuse), plus a negative case.

See [examples/dispatch_guard.py](../examples/dispatch_guard.py).

## Briefs

Every worker prompt is the same template filled in, plus one shared rules file appended unchanged. Read-only tasks get the rules too: secret leaks tend to come from briefs that skipped the secrets section because the task "only read status".

Template sections (see [examples/worker-brief.md](../examples/worker-brief.md)):

1. **Task**, with row id and ticket key: one paragraph, the outcome, not the steps.
2. **Context**: what is done already, with SHAs, run ids and paths; what was tried and failed.
3. **Scope**: host, exact repo/server/account, allowed changes and files owned, explicit exclusions.
4. **Pre-approved**: the owner's verbatim yes, with time, for each step that would otherwise stop (merge, deploy, restart), or "none".
5. **Done means**: the observable check.

Then the shared worker rules ([examples/worker-rules.md](../examples/worker-rules.md)): the stop-and-ask list, no unrequested external items, secret handling, shell safety, the WAIT protocol and the report format.

Optionally append a **Lessons** section: a handful of relevant facts recalled from a memory store (past gotchas for this repo or system), filtered for secrets, fail-open if the store is down.

For build-then-stop work, the brief must tell the worker to put an explicit hold marker in every PR body ("Draft: merge only after owner sign-off"). Draft status alone is not a hold; see [merge-gate.md](merge-gate.md).

## The WAIT protocol

A worker must never end its turn silently waiting for something. If it needs to come back later, its **last line** is:

```
WAIT <kind> <ref> <check-by>
```

| field | values | example |
|---|---|---|
| kind | `ci`, `review`, `deploy`, `render`, `date`, `person`, `other` | `ci` |
| ref | One checkable token: `org/repo#PR`, a run id, a deploy id, a URL, a row id | `org/app#88` |
| check-by | `HH:MMZ`, `+90m`, ISO UTC | `+45m` |

Only `WAIT` is required; the parser is lenient and defaults check-by to +60 min. When the coordinator sees a WAIT line it arms a timer the same turn, targeted at itself, naming the row and the worker. When the timer fires it checks the ref, then resumes or closes the worker.

Workers that end a turn "waiting" without a WAIT line and without a live background process get one nudge, then are treated as stalled.

## Finishing

When a worker finishes:

1. Read its report **in the same turn**.
2. Lift every "needs owner" item into the right queue, caveats into the row's notes.
3. Check one artifact yourself (the PR's CI on the head SHA, the read-back, the live URL). The worker's "verified" is not proof.
4. Write the row (close with proof, or update the next action). Run the checker.
5. Cancel the worker's timers.
6. Only then archive the worker and its workspace.

Order matters. Archiving in the same batch as a row write that then fails leaves a row owned by an archived worker.

### Archiving rules

- Archive a worker when its row closes with proof.
- Never archive a running worker, one with uncommitted or unpushed work it did not mention, or one on a keep list.
- Never use a bulk "archive all finished" button on the coordinator. It archives workers that ended on a WAIT line with a live background watch.
- If archiving a parent cascades to its children in your runner, never archive the coordinator.
- Run a guard each pass that finds archived owners of open rows and restores them (or marks the row `coordinator (owner closed: …)` for re-dispatch).

### Re-dispatching dead workers

Same brief, same key, a note to "check live state first, don't redo finished steps", preferably another provider. Cap it (say two per row per 24 h); after that the row is Broken and goes to the owner.
