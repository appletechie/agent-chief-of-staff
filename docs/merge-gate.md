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
- **Green means success (or neutral) on the head SHA.** Pending is wait. Failure, cancelled, timed out, action-required and stale are red.
- **Required checks are explicit.** Configure the checks each repo must report. A required check that has not reported means wait, not merge: the gate can't tell "no check configured" from "check never ran" on its own. A required check that was skipped is not green; one that concluded neutral ran and passes.
- **A required check that never reports is reported.** Wait at most `check_wait` (say 30 minutes) after the last push, then report "required checks never reported". A renamed job or a path filter that skips it would otherwise stall every PR in the repo without a word.
- **Skipped is green only for checks the repo doesn't require**, such as jobs a path filter skips for this change. This is the one place skipped counts; proof that something ran never rests on a skipped step (see the worker rules).
- **Review verdicts are explicit where possible.** Ask the review bot (or wrap it) to end with a line `verdict: clean` or `verdict: findings`, and prefer that over phrase matching. When you must match phrases, strip the clean phrases before looking for finding words ("no major issues" contains "issue"), and treat anything unrecognised as not clean. A verdict or clean phrase inside a fenced block or an indented code block is an example, not the verdict; a fence closes only at a bare delimiter indented at most three spaces (a ```` ```not-a-close ```` line, or one indented four spaces, does not close it). Hold markers and finding words are matched in the rendered words: emphasis, escapes, link and image destinations and HTML tags dropped, so `do [not](url) merge`, `do <span>not</span> merge` and `P**3**` all count (after karutoil's [#31](https://github.com/appletechie/agent-chief-of-staff/issues/31)). That rendering is one CommonMark inline pass (`examples/md_inline.py`), not a stack of regexes: escapes, code spans, autolinks, raw HTML, links and entities are read once in the spec's precedence order, with a code span's contents kept literal (`<WIP>` inside backticks is a visible hold) and an escaped character shown as itself, so `` \` `<WIP>` `` no longer hides the code span ([#36](https://github.com/appletechie/agent-chief-of-staff/issues/36)). Two deliberate choices: an escaped emphasis mark is dropped rather than shown (joining words can only reveal a hold), and a link reference definition line stays in the text as written. Rendering is linear in the input, because a PR body is attacker-controlled text and the gate must not be stallable by a body of 30,000 unmatched brackets. Finding words include every severity the bot uses (`P0` to `P3`, and `P4` if it has one). An empty list ("Findings: None") and a conditional sentence about what the bot does ("if it has suggestions, it will comment") are not findings (after karutoil's [#28](https://github.com/appletechie/agent-chief-of-staff/issues/28)).
- **Squash or your house style**, but one style, set in the script.
- **One message per run.** If anything needs the coordinator, send one consolidated message, and append each decision to the event feed.

## Holds: the lesson behind rule 1 and 2

If your workers open PRs as drafts by default, a gate that "readies" drafts before merging will merge drafts. That is the point of automation, and it means **draft status is not a hold**. Make holds explicit text the gate matches:

- In the ledger row: `HOLD`, `HELD`, "on hold", "do not merge".
- In the PR title or body: `Draft:`, "draft until", "merge only after", "do not merge", `WIP`, "on hold". Match the text as Markdown renders it, so `Draft\:` and "do **not** merge" still hold (#28).

Match conservatively. "held at version 1.0" in a row's prose should not count; an uppercase `HOLD` token or a fixed phrase should. A conditional hold ("merge only after the migration is applied") stays held even after the condition is met, because the gate cannot evaluate conditions. The coordinator checks the condition and merges by hand with the expected-head guard.

## Repos that deploy on merge

Keep an explicit list of repos where a merge to the default branch ships to production (hosting platforms deploying from the main branch, CD pipelines, image builds that roll automatically). The gate **never** auto-merges these. It reports "ready"; the coordinator merges deliberately, then runs a live check of the deployed flow before the row is done, or holds for the owner if that deploy is a production cutover.

Do not trust memory for this list. Check the platform's auto-deploy setting when you add a repo, and re-check during hygiene.

## Review bots

- **Request once per head SHA**, after a quiet period (for example 45 minutes after the last push with no receipt). Record that you asked for that SHA so the next run does not ask again.
- **A bot can sit "running" for a long time.** Waiting is cheaper than re-requesting; set a second, longer threshold for "tell the coordinator the review is stuck".
- **Parse defensively.** Review bot output formats change without notice. The receipt must name the head SHA or be attached to it by the API. Bind to the full 40-hex SHA: a 7-hex prefix is about 28 bits, so anyone who can push can craft a commit that shares it. A bot that writes only an abbreviation (10+ hex) is trusted only when the receipt was posted at or after the head's push and the abbreviation is not also a prefix of a full SHA another receipt names; a full-SHA receipt for the head outranks it, and its verdict must match a known "clean" phrase. Anything you cannot parse is "not clean", reported, never merged.
- **Optional second reviewer.** If the primary bot is stuck past a long threshold, a reviewer from a different model vendor may approve instead. Record which one did.
- **Second approver.** If branch protection requires a non-author approval and your agents share an identity, decide up front whether a separate approver identity is allowed, and log every use of it.

## What the gate never does

- Merge a PR for a ticket that has no open row.
- Merge without checks on the exact head SHA.
- Merge a deploy-on-merge repo.
- Merge through a hold, including a conditional one.
- Change a PR's content, force-push, or resolve review threads on its own.
