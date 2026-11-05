# The merge gate

A script that runs every 15-30 minutes, finds PRs the coordinator is tracking, and either merges them, nudges the review bot, or tells the coordinator why it did not. It replaces the coordinator polling CI.

## Scope

A PR is in scope when all of these hold:

- it is open;
- its author is one of your agents' identities (your bot account, your sandbox app);
- its title carries a ticket key that an **open** ledger row tracks.

Anything else is ignored. A PR for a closed row is not merged by the gate.

## The decision

For each PR in scope, evaluate in this order. The first rule that fires decides.

| # | condition | action |
|---|---|---|
| 1 | Row text or PR title/body carries a hold marker | `REPORT held` |
| 2 | PR is a draft and the policy does not merge drafts | `REPORT draft` |
| 3 | Not mergeable (conflicts, branch protection) | `REPORT not mergeable` |
| 4 | Any check on the **head SHA** is pending | `WAIT` |
| 5 | Any check on the head SHA failed, was cancelled or timed out | `REPORT checks failing` |
| 6 | No review-bot receipt for the head SHA, and the last push was more than N minutes ago, and you have not already asked for this SHA | `REQUEST_REVIEW` (post the trigger once per head SHA) |
| 7 | No review-bot receipt for the head SHA (still within N, or already asked) | `WAIT` |
| 8 | The receipt for the head SHA reports findings, or cannot be parsed | `REPORT review not clean` |
| 9 | Any unresolved review thread | `REPORT unresolved threads` |
| 10 | Repo deploys production on merge | `REPORT ready, deploys on merge` |
| 11 | Otherwise | `MERGE` |

See [examples/merge_gate.py](../examples/merge_gate.py) for this as a pure function.

### Details that matter

- **Head SHA, always.** Checks and reviews on a previous commit mean nothing. Read the head SHA once, evaluate everything against it, and merge with an "expected head" guard (most VCS APIs have one) so a push between evaluation and merge aborts the merge.
- **Success, skipped and neutral are green.** Pending is wait. Failure, cancelled, timed out, action-required and stale are red. A CI step that was cancelled is not green.
- **Squash or your house style**, but one style, set in the script.
- **One message per run.** If anything needs the coordinator, send one consolidated message, and append each decision to the event feed.

## Holds: the lesson behind rule 1 and 2

If your workers open PRs as drafts by default, a gate that "readies" drafts before merging will merge drafts. That is the point of automation, and it means **draft status is not a hold**. Make holds explicit text the gate matches:

- In the ledger row: `HOLD`, `HELD`, "on hold", "do not merge".
- In the PR title or body: `Draft:`, "draft until", "merge only after", "do not merge", `WIP`, "on hold".

Match conservatively. "held at version 1.0" in a row's prose should not count; an uppercase `HOLD` token or a fixed phrase should. A conditional hold ("merge only after the migration is applied") stays held even after the condition is met, because the gate cannot evaluate conditions. The coordinator checks the condition and merges by hand with the expected-head guard.

## Repos that deploy on merge

Keep an explicit list of repos where a merge to the default branch ships to production (hosting platforms deploying from the main branch, CD pipelines, image builds that roll automatically). The gate **never** auto-merges these. It reports "ready"; the coordinator merges deliberately, then runs a live check of the deployed flow before the row is done, or holds for the owner if that deploy is a production cutover.

Do not trust memory for this list. Check the platform's auto-deploy setting when you add a repo, and re-check during hygiene.

## Review bots

- **Request once per head SHA**, after a quiet period (for example 45 minutes after the last push with no receipt). Record that you asked for that SHA so the next run does not ask again.
- **A bot can sit "running" for a long time.** Waiting is cheaper than re-requesting; set a second, longer threshold for "tell the coordinator the review is stuck".
- **Parse defensively.** Review bot output formats change without notice. The receipt must name the head SHA (or be attached to it by the API), and its verdict must match a known "clean" phrase. Anything you cannot parse is "not clean", reported, never merged.
- **Optional second reviewer.** If the primary bot is stuck past a long threshold, a reviewer from a different model vendor may approve instead. Record which one did.
- **Second approver.** If branch protection requires a non-author approval and your agents share an identity, decide up front whether a separate approver identity is allowed, and log every use of it.

## What the gate never does

- Merge a PR for a ticket that has no open row.
- Merge without checks on the exact head SHA.
- Merge a deploy-on-merge repo.
- Merge through a hold, including a conditional one.
- Change a PR's content, force-push, or resolve review threads on its own.
