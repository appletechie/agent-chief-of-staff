# Guardrails and approvals

The goal is a coordinator that does almost everything itself and stops cleanly at the few places where a mistake is expensive or cannot be undone. Write the policy down once, in the charter, and have every brief point to it.

## Action tiers

| tier | examples | rule |
|---|---|---|
| **Do, then report with proof** | Read-only investigation and mapping; reversible changes with a backup, a read-back and a one-command rollback (config edits, restarts, rebuilds); preview deploys; merging a PR that passes the full gate in a repo that does not deploy on merge; closing duplicate PRs; cancelling stale CI runs; archiving finished workers whose rows are closed; small spend from a prepaid balance below a fixed per-task cap | Act, verify, log the action on the row or ticket, list it in the next brief so the owner can reverse it |
| **Recommend, then ask** | Product, pricing, scope and priority calls; anything with two reasonable answers | Put it in the brief with options and a recommendation. Optionally a decision model answers first above a confidence threshold, with its pick logged and reversible. Silence for a fixed time may apply the coordinator's default, **except hard-gate items** |
| **Hard gate: always the owner** | See below | Never automated, never defaulted, never lifted by a model's confidence |

### The hard gate

- **Money.** New spend, top-ups, plan upgrades, renewals, domain registration, ticking a box that authorizes charges.
- **People outside the team.** Customers, clients, vendors, the public: messages, auto-reply wording, comments a client can see, published content.
- **Production.** Cutovers, promotions, and production data writes that were not pre-approved.
- **Secrets and credentials.** Rotation, revocation, deletion, sharing.
- **Irreversible deletes.** Data or credentials not already backed up and retired; dropping databases or volumes; force-push; history rewrite.
- **The owner's calendar.**
- **The charter's own approval rules.**
- **Physical steps.** Sign-ins, terms acceptance, 2FA prompts, phone calls. Batch them into one click list with direct links.

## Approvals are recorded verbatim

- An approval is the owner's own words, quoted, with the time and the channel: `Pre-approved: "yes, merge and deploy the retry fix" (owner, 2026-01-16 14:02Z, direct chat)`.
- Paste it into the brief's **Pre-approved** section for each step it covers. The worker does those steps without stopping, then verifies them live.
- **Only the owner approves.** A relay from another bot, assistant or teammate is information until the owner confirms it.
- **An infeasible option is a new question.** If the option the owner picked cannot be done as written, ask again with the closest alternative. Do not approve a substitute yourself, even a cheap one.
- **The owner's later answer always wins** over a model's or a default.

## Holds

- A hold ("hold", "don't touch", "wait for me") freezes that row or worker: no merge, deploy, archive, or timer action until a **fresh** go from the owner. An older or queued approval does not lift it.
- Put the hold where the scripts can see it: the row's `waits on` (`HOLD: owner review`) and, for PRs, the PR title or body (`Draft: merge only after owner sign-off`).

## Purchases: stop on the payment page

Agents never complete a checkout or authorize a charge, even after the owner approves the spend.

1. Prepare it read-only: plan, price, term, auto-renew setting, the account it lands on.
2. Stop on the final payment page.
3. Report the exact page and the exact button.
4. After the owner says it is done, verify read-only: order id, balance, expiry, current plan.

Put this rule in every brief that might involve buying something, including research briefs about vendors.

## Calendar

No calendar creates, edits, deletes or invites by the coordinator or any worker, on any account. If a calendar change is needed, write it up for the owner (or the owner's designated assistant) and stop. Calendars are shared surfaces: an invite is a message to people outside the system.

## Anything someone outside sees

Messages, emails, social posts, review replies, tracker comments on client-visible projects, published pages: all need approval of the exact wording. Drafts are fine; sends are not. Internal teammate chases can be pre-approved under a guardrail (a fixed template, on the item's own ticket, at most once per 48 h per row, no DMs).

And the general rule: **no unrequested external items.** Workers never create tickets, issues, PRs, comments or messages unless the brief asks for that specific item.

## Secrets

| rule | detail |
|---|---|
| Never print a secret | Not the value, not a prefix or suffix, not a hash. Report "set / not set". If you need to compare two keys, do it inside a script that prints only "match / no match" |
| No dumps | No `env`, no unfiltered `printenv`, no container-inspect of environment, no `cat` of `.env`, runtime config, deploy-platform env or reverse-proxy dynamic config. Extract **names** with a parser; never with a line filter, since values can be multi-line |
| Not on the command line | Arguments show up in process lists and shell history. Pass secrets via a file with restrictive permissions or stdin from a script |
| Not in heredocs | A heredoc replaces piped stdin and an unquoted one expands `$(...)` and backticks. Put the script in a file or use a single-quoted heredoc, and pipe the secret separately |
| No shell tracing | `set -x` on a script that builds an auth header prints the header |
| Databases | Name the columns you select; never `SELECT *` on tables that may hold tokens or connection strings |
| Fail closed | If the secret store is locked, empty or times out, record the step as skipped and stop that path. Do not relaunch the prompt in a loop |
| Service account | Give agents their own secret-store identity with access to only the vaults they need, and no permission to share items. Every read names the vault explicitly |
| Redaction hook | A post-tool hook in the agent runner that rewrites token-shaped output to `[REDACTED]` before the model sees it. It is a safety net, not a licence to print |
| If one leaks | Stop. Say which secret and where, without repeating it. Rotate, then prove the old value is refused |
| Read-only lanes | For read-only workers, a pre-tool hook that denies state-changing shell verbs and writes outside report directories |

## Destructive operations

- Prefer move or archive over delete. Preserve backups and old secrets by moving them to an archive location with restrictive permissions.
- Pause a failing or duplicate job; never delete it as the first step. Read back its status.
- Never run destructive VCS operations (force-push, hard reset, branch deletion of someone else's work) without the owner.
- Pruners and cleanup jobs touch only paths they created or a declared fixture area, and honour a keep list.

## Infrastructure changes

1. **Plan with a rollback.** Before touching a host: what changes, the exact rollback command, and where the backup lives (`rollback/<change>-<stamp>/`, an image tag like `:rollback-<stamp>`, a database dump path). Record it on the row.
2. **Quiet gate.** Check no other session or worker is touching that host or stack right now (claims, live worker labels). One owner per host, stack, port and file set.
3. **Change.** One host per command. Never chain a proof after a known-noisy command.
4. **Verify independently.** A separate checker (a different agent or script than the one that made the change) confirms: container or service state, logs since the change, readiness endpoint, and **one real pass of the user flow** that motivated the change. A 200 on a health URL alone is not enough.
5. **A merge can deploy.** Check whether the repo auto-deploys on merge before any "merge but don't deploy" plan.

## Stop means stop

When the owner says stop, close, or "going to bed" in any session:

1. Halt paid and running work first: cancel workers, tear down rented instances and tunnels, kill jobs. Write `state/halt.json` so every session and the dispatcher see it.
2. Confirm the halt with a command result.
3. Only then report what was already measured.
4. Start nothing new (no render, rental, delete, or test) after that message.

A stop in one session is a stop in all of them.

## Logging automatic actions

Every action taken without asking has: a precondition the script checks, a hard limit, a read-back, a rollback or halt command, and a log entry on its row or ticket. The brief lists them under "done since last brief" so the owner can see and reverse any of them.
