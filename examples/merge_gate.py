#!/usr/bin/env python3
"""Merge gate decision as a pure function. Fetching PR data and performing the merge are the caller's job.

decide(pr, row_text, policy, now) -> Decision(action, reason)
  action is one of MERGE, REQUEST_REVIEW, WAIT, REPORT
The caller merges only on MERGE, and passes pr.head_sha as the expected head so a later push aborts it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

GREEN = {"success", "skipped", "neutral"}
# A required check must actually run and pass; skipped is green only for checks a repo doesn't require.
REQUIRED_GREEN = {"success"}
VERDICT_LINE = re.compile(r"(?m)^\s*verdict:\s*(clean|findings)\s*$")
PENDING = {"queued", "in_progress", "pending", "waiting", "requested"}
# Row holds are the owner's words: an uppercase token or a fixed phrase, not any prose containing "held".
ROW_HOLD = re.compile(r"\bHOLD\b|\bHELD\b|(?i:\bon hold\b|\bdo not merge\b|\bdon'?t merge\b)")
PR_HOLD = re.compile(r"(?i)\bdraft:|\bdraft until\b|\bmerge only after\b|\bdo not merge\b|\bdon'?t merge\b|\bon hold\b|\bwip\b")


@dataclass
class Review:
    sha: str
    body: str


@dataclass
class PR:
    repo: str
    number: int
    title: str
    body: str
    head_sha: str
    is_draft: bool
    mergeable: bool | None          # None = the host has not computed it yet
    checks: dict[str, str]          # check name -> conclusion or status, for head_sha only
    reviews: list[Review]           # review-bot receipts with the SHA each one reviewed
    unresolved_threads: int
    last_push_at: datetime
    review_requested_for: set[str] = field(default_factory=set)


@dataclass
class Policy:
    deploys_on_merge: frozenset[str]
    clean_phrases: tuple[str, ...] = ("no major issues", "looks good to merge")
    finding_phrases: tuple[str, ...] = ("issue", "finding", "suggest", "bug", "p1", "p2")
    # Checks each repo must report on the head SHA. A listed check that never reported means WAIT,
    # and a listed check that was skipped is not green. Unlisted checks may be skipped (path filters).
    required_checks: dict[str, frozenset[str]] = field(default_factory=dict)
    merge_drafts: bool = False
    review_wait: timedelta = timedelta(minutes=45)


@dataclass
class Decision:
    action: str
    reason: str


def review_verdict(pr: PR, policy: Policy) -> str | None:
    """'clean', 'findings', 'unparsed', or None when no receipt names the head SHA."""
    receipts = [r for r in pr.reviews if r.sha == pr.head_sha]
    if not receipts:
        return None
    body = receipts[-1].body.lower()
    # Preferred: an explicit verdict on a line of its own, e.g. "verdict: clean" or "verdict: findings".
    token = VERDICT_LINE.search(body)
    if token:
        return token.group(1)
    # Fallback: phrase matching. Remove the clean phrases first, because they can contain finding words
    # ("no major issues" contains "issue"); a receipt with any finding word left over is not clean.
    clean = any(p in body for p in policy.clean_phrases)
    residue = body
    for phrase in policy.clean_phrases:
        residue = residue.replace(phrase, " ")
    if any(p in residue for p in policy.finding_phrases):
        return "findings"
    return "clean" if clean else "unparsed"


def decide(pr: PR, row_text: str, policy: Policy, now: datetime | None = None) -> Decision:
    now = now or datetime.now(timezone.utc)
    if ROW_HOLD.search(row_text or ""):
        return Decision("REPORT", "row is on hold")
    if PR_HOLD.search(f"{pr.title}\n{pr.body}"):
        return Decision("REPORT", "PR title or body carries a hold marker")
    if pr.is_draft and not policy.merge_drafts:
        return Decision("REPORT", "PR is a draft")
    if pr.mergeable is None:
        return Decision("WAIT", "mergeability not computed yet")
    if not pr.mergeable:
        return Decision("REPORT", "not mergeable")
    if not pr.checks:
        return Decision("WAIT", f"no checks reported on {pr.head_sha[:7]} yet")
    states = {name: s.lower() for name, s in pr.checks.items()}
    required = policy.required_checks.get(pr.repo, frozenset())
    missing = sorted(required - set(states))
    if missing:
        return Decision("WAIT", "required checks not yet reported: " + ", ".join(missing))
    if any(s in PENDING for s in states.values()):
        return Decision("WAIT", "checks pending on head")
    red = sorted(n for n, s in states.items()
                 if s not in GREEN or (n in required and s not in REQUIRED_GREEN))
    if red:
        return Decision("REPORT", "checks not green on head: " + ", ".join(red))
    verdict = review_verdict(pr, policy)
    if verdict is None:
        quiet = now - pr.last_push_at
        if quiet >= policy.review_wait and pr.head_sha not in pr.review_requested_for:
            return Decision("REQUEST_REVIEW", f"no review receipt for {pr.head_sha[:7]} after {quiet}")
        return Decision("WAIT", f"waiting for review of {pr.head_sha[:7]}")
    if verdict == "findings":
        return Decision("REPORT", "review on head has findings")
    if verdict == "unparsed":
        return Decision("REPORT", "review output not recognized; treat as not clean")
    if pr.unresolved_threads:
        return Decision("REPORT", f"{pr.unresolved_threads} unresolved review threads")
    if pr.repo in policy.deploys_on_merge:
        return Decision("REPORT", "ready, but this repo deploys on merge: coordinator merges and live-checks")
    return Decision("MERGE", f"green on {pr.head_sha[:7]}")


if __name__ == "__main__":
    t0 = datetime(2026, 1, 16, 14, 0, tzinfo=timezone.utc)
    policy = Policy(deploys_on_merge=frozenset({"org/website"}))
    pr = PR(repo="org/app", number=88, title="ISSUE-231 Fix retry loop", body="Closes ISSUE-231",
            head_sha="abc1234def", is_draft=False, mergeable=True,
            checks={"test": "success", "lint": "success", "e2e": "skipped"},
            reviews=[Review("abc1234def", "No major issues found.")],
            unresolved_threads=0, last_push_at=t0)
    print(decide(pr, "Fix the retry loop", policy, now=t0 + timedelta(hours=1)))
    pr.body += "\n\nDraft: merge only after owner sign-off"
    print(decide(pr, "Fix the retry loop", policy, now=t0 + timedelta(hours=1)))
