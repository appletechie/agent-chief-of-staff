# Adapting it to your stack

Every component is defined by a contract. Pick whatever satisfies it.

## Component map

| component | contract | options |
|---|---|---|
| **Agent runner** | Start an agent with a prompt, labels and a working directory; list agents with status across hosts; send a message into a running or idle agent (queued if busy); read an agent's last message; archive it | Any CLI or daemon that can spawn and message agents: a coding-agent CLI driven from scripts, a multi-agent desktop app with an API, a hosted agent platform, or your own wrapper around a model API |
| **Models** | Planning, coding and review capability; live quota visibility helps routing | Any. Mixing vendors for review catches more than a single vendor reviewing itself |
| **Sandboxes (optional)** | Isolated container per task, clone, push a branch, open a draft PR, restricted egress | Hosted sandbox services, your own container runner, VMs, or plain worktrees on one machine |
| **Issue tracker** | Search by query, read assignee/status/labels/comments, post comments, move status | Jira, Linear, GitHub Issues, GitLab Issues, a Markdown folder |
| **VCS host** | Search open PRs by text across orgs, read checks on a commit, read review threads, merge with an expected-head guard | GitHub, GitLab, Gitea, Bitbucket |
| **CI** | Per-commit check status with conclusions | Whatever the VCS host or your CI server provides |
| **Review bot (optional)** | A review receipt tied to a commit SHA with a parseable verdict | Any automated PR reviewer, or a reviewer agent you dispatch yourself |
| **Scheduler** | Run a script every minute and every N minutes, survive reboots, log exit codes | cron, launchd, systemd timers, a cloud scheduler, a CI schedule (coarser) |
| **State store** | Files with an advisory lock, or anything with transactions | Local files with `flock`, SQLite, a small Postgres table, a key-value store with compare-and-set |
| **Secret store** | Read by name into a child process; separate identity for agents; fail closed | Any password manager with a CLI and service accounts, a cloud secret manager, a vault server, OS keychain |
| **Memory store (optional)** | Store short lessons; recall a few relevant ones by query | A folder of Markdown notes with an index, a vector store, a hosted memory service |
| **Dashboard (optional)** | Render rows from the ledger | Your runner's progress board, a static HTML page, a tracker board synced one way |
| **Notification channel** | Reach the owner with the brief and urgent items | Chat app, email draft, the runner's own UI. Have a fallback and check it is actually delivering |
| **Browser automation (optional)** | Drive a signed-in browser for steps with no API | Any automation tool; treat it as acting as the owner and gate it accordingly |
| **Decision model (optional)** | Pick among written options with a calibrated confidence | Any model asked for a probability per option; run it in trial mode (log, don't apply) until you have measured agreement with the owner |

## Minimal vs full

| capability | minimal (day one) | full |
|---|---|---|
| Ledger | Markdown table, checker script, close-with-proof rule | Locked writes, spill to notes, queues, generated dashboard, closed archives |
| Dispatch | One script that searches open PRs for the key, then starts the worker with template + rules | Router with live quota, placement table, workspaces per task, claims, check-by timers, lesson recall |
| Waking | A cron that sends the coordinator "run a pass" every 30 min | Wake gate with hashed parts, durable timers, missed-wake redelivery, event feed |
| Merge | Coordinator checks CI on the head SHA by hand before merging | Merge gate script with holds, deploy-on-merge list, review requests |
| Intake | Owner messages only | Tracker intake, backlog pump, drift report |
| Hygiene | None | Schedule health, doc lint, dead-worker sweep, archive proposals |
| Sessions | One session only | Lease, registry, companion rules, halt file, boot brief, rollover |
| Guardrails | The hard-gate list in the coordinator's instructions | Charter with tiers, recorded approvals, holds, redaction hook, read-only lanes |

## What to build first

Ordered by how much pain each removes per hour of work:

1. **Ledger + checker + proof rule.** The coordinator stops forgetting.
2. **Brief template + shared worker rules.** Workers stop doing surprising things, secrets stop leaking.
3. **Duplicate guard.** No more competing PRs.
4. **Durable timers + WAIT protocol.** Timed steps stop disappearing.
5. **Wake gate.** Cost and context size drop sharply; the coordinator notices real changes.
6. **Merge gate.** CI polling leaves the model; deploy-on-merge accidents stop.
7. **Tracker intake + backlog pump.** Work arrives without being asked for.
8. **Dead-worker sweep across all hosts.** Nothing stalls silently.
9. **Lease, registry, event feed, halt file.** A second session becomes safe.
10. **Schedule health + doc lint.** Rot is found by scripts.
11. **Rollover.** Long-lived coordinators stay fast.

## Testing the mechanisms

The scripts are small and deterministic; test them like any other code.

- Ledger: concurrent writers do not lose each other's edits; ids are never reused across a closed archive; the checker catches each malformed case.
- Wake gate: an unreadable source reports CHANGED; max-quiet forces CHANGED; a companion cannot save the shared snapshot.
- Timers: a crash between `sending` and `delivered` never resends; a timer on a closed row is cancelled; a bad due time defaults rather than failing.
- Every state file that gates behaviour, with valid JSON and a bad field (a naive or unparseable time, a wrong type, a missing id): the wake gate reads CHANGED, the tick skips that record and ticks the rest, the guard refuses that name, the lease raises its named error.
- Dispatch guard: replay the incident that motivated it (an open PR by a teammate) and a negative case; a failed VCS search surfaces as a warning.
- Merge gate: each row of the decision table, plus "review output in an unknown format".

Inject every external call (VCS, tracker, runner, clock) so tests run offline.

## Things to decide up front

- Which identities your agents use on the VCS host and tracker, and whether the coordinator writes as the owner or as its own account. If it writes as the owner, intake must recognize its own comments.
- Which repos deploy on merge.
- The per-task prepaid spend cap for the "do, then report" tier (zero is a fine answer).
- Where workers run by default, and what needs the owner's own browser.
- The owner's time zone and the brief schedule.
- Which channel counts as the owner's direct word.
