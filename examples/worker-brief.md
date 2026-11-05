# Worker brief template

Fill every section. Leave none out, including for read-only checks. The dispatcher appends
`worker-rules.md` unchanged after this; do not restate or paraphrase those rules here.

```
## Task  (row R-___, key ISSUE-___ or "no key: <reason>")
<One paragraph: the outcome you want, not the steps.>

## Context
<What is already done, with commit SHAs, run ids, PR numbers and file paths.
What was tried and failed, and why.>

## Scope
- Host: <sandbox | host-a | host-b>
- Repo / server / account: <exact names>. Confirm the VCS remote before any sign-in or token creation.
- Allowed: <the changes you may make and the files you own>
- Not allowed: <explicit exclusions: other repos, prod data, shared config, ...>

## Pre-approved (owner's words, time)
<For each step that would otherwise stop (merge, deploy, restart, push), quote the owner's yes
verbatim with its time and channel. Or "none".>

## Done means
<The observable check, for example:
 - "PR #N merged and CI shows completed/success on the merge SHA"
 - "a fresh request to <endpoint> returns 200 with <field> = <value>, and the signup flow completes once"
 - "the failing test re-run passes: exit 0, N passed">
```

## Filled example

```
## Task  (row R-012, key ISSUE-231)
The nightly sync job stops retrying forever on a 4xx from the upstream API, and the fix is in a draft PR.

## Context
Logs from the last three nights show the job looping on HTTP 404 for deleted records (see
ledger/notes/R-012.md). The retry helper is in src/sync/retry.py. A previous attempt (closed PR #71)
retried 4xx with backoff, which only slowed the loop.

## Scope
- Host: sandbox
- Repo: org/app. Branch ISSUE-231-retry-4xx from main.
- Allowed: src/sync/**, tests/sync/**
- Not allowed: other packages, CI config, any deploy

## Pre-approved (owner's words, time)
none

## Done means
Draft PR open with "Draft: merge only after owner sign-off" in the body; CI completed/success on its head
SHA; a new test that fails on main and passes on the branch, with the test command's exit code and
summary quoted in the report.
```

## After it finishes (coordinator side)

1. Read the report in the same turn.
2. If the last line is `WAIT ...`, arm a timer from it now.
3. Lift every "needs owner" item into its queue; caveats into the row's notes.
4. Check one artifact yourself before closing the row.
5. Write the row, run the checker, cancel the worker's timers, then archive.
