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
# A required check must actually run and pass (neutral passes, as on most VCS hosts); skipped is green only
# for checks a repo doesn't require.
REQUIRED_GREEN = {"success", "neutral"}
VERDICT_LINE = re.compile(r"(?m)^\s*verdict:\s*(clean|findings)\s*$")
PENDING = {"queued", "in_progress", "pending", "waiting", "requested"}
# Row holds are the owner's words: an uppercase token or a fixed phrase, not any prose containing "held".
ROW_HOLD = re.compile(r"\bHOLD\b|\bHELD\b|(?i:\bon hold\b|\bdo not merge\b|\bdon'?t merge\b)")
PR_HOLD = re.compile(r"(?i)\bdraft:|\bdraft until\b|\bmerge only after\b|\bdo not merge\b|\bdon'?t merge\b|\bon hold\b|\bwip\b")
# Markdown that renders a hold marker: backslash escapes (Draft\:) and emphasis or code (do **not** merge) (#28), a
# link or image whose label carries the words (do [not](url) merge) and inline HTML (do <span>not</span> merge) (#31).
# The label stays and the destination goes; an autolink <https://...> renders as its address.
MD_ESCAPE = re.compile(r"\\([!-/:-@\[-`{-~])")
MD_MARKS = re.compile(r"[*_`~]+")
MD_LINK = re.compile(r"!?\[([^\]]*)\](?:\([^)]*\)|\[[^\]]*\])")
MD_AUTOLINK = re.compile(r"<([a-z][a-z0-9+.-]*:[^<>\s]*)>", re.I)
HTML_TAG = re.compile(r"<!--.*?-->|</?[a-z][a-z0-9-]*(?:\s[^<>]*)?/?>", re.I | re.S)
# A fence delimiter is indented at most 3 spaces (CommonMark): a "    ```" line inside a fence does not close it (#31).
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
# An indented code line (4 spaces or a tab) is an example, like a fenced block (#28).
INDENTED = re.compile(r"^(?: {4,}|\t)")
# "Findings: None" (or a Findings heading over "None") says the list is empty (#28).
EMPTY_FINDINGS = re.compile(r"(?m)^[#\s*]*findings?\s*(?::\s*|\n\s*)(?:none|nothing|n/a)\.?\s*$")
# A conditional clause about what the review bot does with findings is boilerplate, not a finding (#28):
# "If Codex has suggestions, it will comment". The subject must be the bot: "if the connection has issues, ..."
# is a finding and stays (Codex on #30). Only the clause goes; the rest of the sentence still counts.
CONDITIONAL = re.compile(r"\bif\s+(?:the\s+)?(?:codex|copilot|review(?:er|ing)?(?:\s+bot)?|bot|[\w-]+(?:\[bot\]|-bot|bot))"
                         r"\s+(?:has|have|finds?)\s+(?:any\s+)?(?:suggestions?|issues?|findings?)\b", re.I)
SHORT_SHA = re.compile(r"[0-9a-f]{10,39}")
FULL_SHA = re.compile(r"[0-9a-f]{40}")


@dataclass
class Review:
    sha: str
    body: str
    at: datetime | None = None      # when the receipt was posted; a short sha binds only with it (see _receipts)


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
    finding_phrases: tuple[str, ...] = ("issue", "finding", "suggest", "bug", "p0", "p1", "p2", "p3", "p4")
    # Checks each repo must report on the head SHA. A listed check that has not reported means WAIT, until
    # check_wait after the last push, then REPORT (a renamed or path-filtered job must not stall the repo quietly).
    # A listed check that was skipped is not green. Unlisted checks may be skipped (path filters).
    required_checks: dict[str, frozenset[str]] = field(default_factory=dict)
    merge_drafts: bool = False
    review_wait: timedelta = timedelta(minutes=45)
    check_wait: timedelta = timedelta(minutes=30)


@dataclass
class Decision:
    action: str
    reason: str


def _outside_fences(body: str) -> list[str]:
    """The lines of body that are not inside a fenced block. A fence opens with ``` or ~~~ and closes only at a
    delimiter of the same character at least as long with nothing after it, so a backtick line inside a tilde
    example (#22) or a ```not-a-close line (#28) does not end it; an unclosed fence runs to the end."""
    out, fence, fence_size = [], None, 0
    for line in body.splitlines():
        if opener := FENCE.match(line):
            here = opener.group(1)
            if fence is None:
                fence, fence_size = here[0], len(here)
                continue
            if fence == here[0] and len(here) >= fence_size and not line[opener.end():].strip():
                fence, fence_size = None, 0
                continue
        if fence is None:
            out.append(line)
    return out


def _verdict_lines(body: str) -> list[str]:
    """Token values on verdict lines **outside** fenced blocks, in file order.

    Bots write examples of the same format (or instructions saying to use it) in the same receipt, so a
    bare search would treat an illustrating code block as the verdict. Fenced regions are not instructions to
    the gate. Nor are indented code lines (#28).
    """
    return [m.group(1) for line in _outside_fences(body) if not INDENTED.match(line) and (m := VERDICT_LINE.match(line))]


def _receipts(pr: PR) -> list[Review]:
    """The receipts that name the head. A full 40-hex SHA always binds. A 7-hex prefix is ~28 bits, so whoever can
    push can craft a commit that shares it: an abbreviation (as bots write it, #28) binds only when it is 10+ hex,
    a prefix of the head, posted at or after the last push, and not also a prefix of a full SHA another receipt
    names. With any full-SHA receipt for the head, only those count."""
    head = (pr.head_sha or "").lower()
    full = [r for r in pr.reviews if (r.sha or "").strip().lower() == head and FULL_SHA.fullmatch(head)]
    if full:
        return full
    others = {(r.sha or "").strip().lower() for r in pr.reviews} - {head}
    others = {o for o in others if FULL_SHA.fullmatch(o)}
    out = []
    for r in pr.reviews:
        sha = (r.sha or "").strip().lower()
        if sha == head:
            out.append(r)
        elif (SHORT_SHA.fullmatch(sha) and head.startswith(sha) and r.at is not None and r.at >= pr.last_push_at
              and not any(o.startswith(sha) for o in others)):
            out.append(r)
    return out


def review_verdict(pr: PR, policy: Policy) -> str | None:
    """'clean', 'findings', 'unparsed', or None when no receipt names the head SHA."""
    receipts = _receipts(pr)
    if not receipts:
        return None
    # Fences and verdict lines are read in the source (rendering drops the backticks); findings in the rendered words.
    body = receipts[-1].body.lower()
    # Explicit verdict wins over phrases, but the last one rather than the first: a receipt that repeats its
    # convention before the actual decision should not be judged by the example.
    tokens = _verdict_lines(body)
    # A clean phrase counts only outside fences and indented code (a quoted example is not a verdict); findings
    # count anywhere.
    outside = "\n".join(line for line in _outside_fences(body) if not INDENTED.match(line))
    clean = any(p in outside for p in policy.clean_phrases)
    # "no issues", "no major findings" say there is nothing; they are not finding words themselves. Read in the
    # rendered words, so "P**3**" is a P3 and "no **major** issues" is still nothing (#31).
    residue = re.sub(r"\bno\s+(?:major\s+)?(?:issues?|findings?|bugs?)\b", " ", rendered(body))
    residue = CONDITIONAL.sub(" ", EMPTY_FINDINGS.sub(" ", residue))
    for phrase in policy.clean_phrases:
        residue = residue.replace(phrase, " ")
    # Findings anywhere in the receipt beat a clean verdict token: the token is a summary, the list is the
    # evidence that something is left.
    if _has_finding(residue, policy.finding_phrases):
        return "findings"
    if tokens:
        return tokens[-1]
    return "clean" if clean else "unparsed"


def rendered(text: str) -> str:
    """The words Markdown renders, line by line: link and image labels without their destinations, HTML tags and
    comments dropped, escapes undone, emphasis and code marks dropped, runs of spaces read as one. `Draft\\:` reads
    `Draft:`, and `do **not** merge`, `do [not](url) merge` and `do <span>not</span> merge` read `do not merge`, so
    formatting never hides a hold marker (#28, #31) or a finding."""
    text = HTML_TAG.sub("", MD_AUTOLINK.sub(r"\1", MD_LINK.sub(r"\1", text or "")))
    return re.sub(r"[ \t]+", " ", MD_MARKS.sub("", MD_ESCAPE.sub(r"\1", text)))


def _has_finding(text: str, phrases) -> bool:
    """A finding phrase in text. Words match as substrings ("issues", "suggested"); a severity marker such as p3 is
    a standalone token, so MP3, MP4 or HTTP3 is not a finding (Codex on #30)."""
    for phrase in phrases:
        if re.fullmatch(r"p\d", phrase):
            if re.search(rf"(?<![\w-]){phrase}(?!\w)", text):  # p2-yellow (a badge) still counts
                return True
        elif phrase in text:
            return True
    return False


def review_problem(pr: PR, policy: Policy) -> str | None:
    """Why the review on the head SHA is not clean, or None. The one definition: decide() and the close use it."""
    verdict = review_verdict(pr, policy)
    if verdict is None:
        return f"no review receipt for {pr.head_sha[:7]}"
    if verdict == "findings":
        return "review on head has findings"
    if verdict == "unparsed":
        return "review output not recognized; treat as not clean"
    if pr.unresolved_threads:
        return f"{pr.unresolved_threads} unresolved review threads"
    return None


def decide(pr: PR, row_text: str, policy: Policy, now: datetime | None = None) -> Decision:
    now = now or datetime.now(timezone.utc)
    if ROW_HOLD.search(row_text or ""):
        return Decision("REPORT", "row is on hold")
    # a paragraph's line break renders as a space: "do not\nmerge" is a hold
    if PR_HOLD.search(" ".join(rendered(f"{pr.title}\n{pr.body}").split())):
        return Decision("REPORT", "PR title or body carries a hold marker")
    if pr.is_draft and not policy.merge_drafts:
        return Decision("REPORT", "PR is a draft")
    if pr.mergeable is None:
        return Decision("WAIT", "mergeability not computed yet")
    if not pr.mergeable:
        return Decision("REPORT", "not mergeable")
    # A required check that never ran is reported even when nothing ran at all (#31): before the no-checks WAIT.
    required = policy.required_checks.get(pr.repo, frozenset())
    missing = sorted(required - set(pr.checks))
    if missing:
        if now - pr.last_push_at >= policy.check_wait:
            return Decision("REPORT", "required checks never reported: " + ", ".join(missing))
        return Decision("WAIT", "required checks not yet reported: " + ", ".join(missing))
    if not pr.checks:
        return Decision("WAIT", f"no checks reported on {pr.head_sha[:7]} yet")
    states = {name: s.lower() for name, s in pr.checks.items()}
    if any(s in PENDING for s in states.values()):
        return Decision("WAIT", "checks pending on head")
    red = sorted(n for n, s in states.items()
                 if s not in GREEN or (n in required and s not in REQUIRED_GREEN))
    if red:
        return Decision("REPORT", "checks not green on head: " + ", ".join(red))
    if review_verdict(pr, policy) is None:
        quiet = now - pr.last_push_at
        if quiet >= policy.review_wait and pr.head_sha not in pr.review_requested_for:
            return Decision("REQUEST_REVIEW", f"no review receipt for {pr.head_sha[:7]} after {quiet}")
        return Decision("WAIT", f"waiting for review of {pr.head_sha[:7]}")
    problem = review_problem(pr, policy)
    if problem:
        return Decision("REPORT", problem)
    if pr.repo in policy.deploys_on_merge:
        return Decision("REPORT", "ready, but this repo deploys on merge: coordinator merges and live-checks")
    return Decision("MERGE", f"green on {pr.head_sha[:7]}")


def pr_review_policy(lookup, policy: Policy | None = None):
    """A can_close policy (#19): a row that names a PR closes as `merged` only when the review on that PR's head SHA
    is clean, by review_problem(), the same definition decide() merges on.

    lookup(repo, number) returns the PR (its state after the merge); None or a lookup error refuses the close. Only
    the review is checked here, not decide() as a whole: after a merge, mergeability, hold markers and drafts no
    longer mean "not done". Compose with ledger_store.pr_merge_policy to require the merge SHA too.
    """
    policy = policy or Policy(frozenset())

    def can_close(row, proof):
        from ledger_store import FIELDS, pr_refs
        if proof.kind != "merged":
            return None
        for repo, number in pr_refs(" ".join(str(row.get(k, "")) for k in FIELDS)):
            try:
                pr = lookup(repo, number)
            except Exception as error:  # a broken lookup must not read as "allowed"
                return f"cannot read {repo}#{number} ({error})"
            if pr is None:
                return f"cannot rule out findings on {repo}#{number}: no PR state was returned"
            problem = review_problem(pr, policy)
            if problem:
                return f"{repo}#{number} review on head {pr.head_sha[:7]} is not clean: {problem}"
        return None
    return can_close


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
