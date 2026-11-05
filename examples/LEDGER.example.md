# Ledger

One line per open item: what happens next, who does it, what it waits on. History and proofs go in the
tracker or `ledger/notes/<id>.md`. Ids are never reused. Closed rows move to `ledger/closed-YYYY-MM.md`
with their proof.

## Open

| id | key | owner | next action | waits on | due |
|---|---|---|---|---|---|
| R-012 | ISSUE-231 | worker-7f3a2c | Fix the retry loop in the sync job; open a draft PR | — | — |
| R-013 | ISSUE-235 | coordinator | Dispatch a reviewer for the cache-eviction PR once CI is green | worker | — |
| R-014 | — | owner | Choose the starter-plan price: A $9 / B $12 (recommend A) | owner: decision | 01-16 |
| R-015 | ISSUE-238 | owner | Sign in to the analytics dashboard and grant read access to the agent account | owner: hands | — |
| R-016 | ISSUE-240 | coordinator | Ask for the staging credentials again if no reply | vendor contact | 01-18 |
| R-017 | ISSUE-241 | coordinator | Re-check error rate after the 24 h soak, then close or roll back | time | 01-17 09:00Z |
| R-018 | ISSUE-244 | worker-91bd04 | Migrate the report job to the new queue. HOLD: owner reviews the PR before merge | worker; then owner | — |
