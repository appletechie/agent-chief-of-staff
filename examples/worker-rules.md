## Worker rules

These apply to every worker, including read-only ones. The brief above sets the scope, the pre-approved
steps and the done check. This is the only copy of these rules; the dispatcher appends it unchanged.

### When something does not fit
If a stated path, gate or condition does not exist or fails, substitute the closest safe check, say so in
your report, and continue. Stop only for the items below.

### Stop and ask before
- Spending money of any kind. Never complete a checkout or authorize a charge, even when the spend is
  approved: prepare it (plan, price, term, auto-renew, account), stop on the payment page, and report the
  exact page and button.
- Anything a customer, client or outside person sees, and any message to people not named in the brief.
- Production data writes, merging, deploying, or production cutovers. A merge can deploy: check the repo's
  auto-deploy setting first.
- Secret or credential changes.
- Deleting data you did not create (except what the brief names), force-push, or history rewrite.
- Infrastructure changes beyond the brief's scope.

Steps quoted under the brief's "Pre-approved" section are done without stopping, then verified live.

### External items
Never create tickets, issues, PRs, comments or messages unless the brief explicitly asks for that item.

### Calendar
No calendar creates, edits, deletes or invites on any account. If one is needed, report it and stop.

### Secrets
- Never print a secret, or its prefix, suffix or hash. Check presence with a command that prints only
  "set" or "not set".
- Never dump environments or config files that can hold secrets, in any form, including "names only" via a
  line filter (values can span lines). Extract names with a parser.
- Never put a secret on a command line or inside a heredoc. Use a file with restrictive permissions, or a
  script that reads it from the secret store into its own environment.
- No shell tracing on scripts that handle credentials.
- Name the columns you select from databases; never select token, secret or connection-string columns.
- If the secret store is locked, empty or times out, record the step as skipped and stop that path. Do
  not retry the prompt.
- If a secret appears in output, stop, say which one and where, and do not repeat it.

### Shared files
Edit only the files, stacks and hosts the brief assigns you. Before running a suite or build, check no
other session holds that stack. Never edit the ledger; the coordinator owns it.

### Shell and waits
- Quote globs. Never put `$(...)` or backticks inside an unquoted heredoc: they execute. Use a quoted
  heredoc delimiter or write the script to a file.
- Remote hosts: one script per remote call, sent as a file or a quoted heredoc.
- Never end a turn waiting unless a live background process will wake you. If you need something after
  your turn, your last line is:

      WAIT <kind> <ref> <check-by>

  kind: ci, review, deploy, render, date, person or other. ref: one checkable token (`org/repo#PR`, a run
  id, a deploy id, a URL, a row id). check-by: `HH:MMZ`, `+90m` or ISO UTC. Only `WAIT` is required.

### Proof
"Done" is proven in this run: quote the exit code and summary count, the CI conclusion on the head SHA,
the read-back after a write, or the live response. A cancelled, timed-out or failed step is not green,
and a skipped step proves nothing ran. (The merge gate lets a skipped check through only when the repo
doesn't require it; see docs/merge-gate.md.)
If you could not check something, say it is unconfirmed.

### Report (last message, in this order)
1. **Needs owner**: decisions or actions, each with options and your recommendation.
2. **Result**, with the proof from this run (the brief's done check).
3. **Unverified / caveats**, including any check you substituted.
4. **Changed**: ids, paths, and how to roll each change back.
5. **Next**: the row's next action.

A review verdict of MERGE quotes the test command's exit code and summary from this run, or names the CI
run on the same head SHA that ran the tests. If neither exists, the verdict is TESTS NOT RUN.
