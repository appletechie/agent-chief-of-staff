#!/usr/bin/env python3
"""One small CommonMark **inline** pass, standard library only (#36).

The merge gate reads hold markers and finding words in the words a reviewer or owner would *see*. Rendering those
words with a stack of regexes kept interacting badly (an escaped backtick swallowed a code span's opener, so a
visible `<WIP>` disappeared), so this walks the text once, in the spec's precedence order, and returns the visible
text:

    backslash escapes -> code spans -> autolinks, raw HTML -> links and images -> entities -> emphasis

It deliberately renders **inline** content only; block structure stays with the caller (`merge_gate.rendered`),
which splits fences and indented code and collects link reference definitions from prose. Fail-closed choices from
#35 are kept: an escaped punctuation character shows as itself, a code span keeps its contents literal, and an
HTML tag or comment renders as nothing (a block-level tag as a space).

Two grammar rules here are easy to get wrong and are implemented from cmark's own scanners rather than by eye:

* the label of a link must be **immediately** followed by `(`, `[`, or nothing -- `[a] (x)` is not a link;
* a code span's contents never act as delimiters, so a `]` inside backticks cannot close a `[` outside them.

Both directions matter: a missed link leaves an invisible destination visible (a false hold), and a mis-paired
bracket can eat a visible one (a false merge). The test suite pins each of them (`test_inline_markdown.py`).
"""
from __future__ import annotations

import html
import html.entities
import re

# CommonMark: a backslash escape exists only before ASCII punctuation.
ESCAPABLE = set("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")
AUTOLINK_URI = re.compile(r"[A-Za-z][A-Za-z0-9+.-]{1,31}:[^<>\x00-\x20]*")
AUTOLINK_EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                            r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*")
# cmark: open tag  <name attributes* /?>   close tag  </name space* >   (a closing tag carries no attributes)
OPEN_TAG = re.compile(r"<[A-Za-z][A-Za-z0-9-]*(?:\s+[A-Za-z_:][A-Za-z0-9_.:-]*"
                      r"(?:\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s\"'=<>`]+))?)*\s*/?>")
CLOSE_TAG = re.compile(r"</[A-Za-z][A-Za-z0-9-]*\s*>")
# A comment is scanned by hand rather than by regex: cmark's grammar has no body ending in "-", and CommonMark's
# text rule rejects a body starting with ">" or "->". Both rejections are the fail-closed direction here -- an
# ambiguous "comment" then renders literally, so a hold marker inside it stays visible instead of vanishing.
# cmark: declaration = "<!" ASCIIletter ... ">"; processing instruction = "<? ... ?>"; CDATA = "<![CDATA[ ... ]]>"
HTML_DECL = re.compile(r"<![A-Za-z][^>]*>", re.S)
HTML_PI = re.compile(r"<\?.*?\?>", re.S)
HTML_CDATA = re.compile(r"<!\[CDATA\[.*?\]\]>", re.S)
BLOCK_TAG = re.compile(r"(?:address|article|aside|blockquote|br|caption|dd|details|dialog|div|dl|dt|fieldset"
                       r"|figcaption|figure|footer|form|h[1-6]|header|hr|li|main|nav|ol|p|pre|section|summary|table"
                       r"|tbody|td|tfoot|th|thead|tr|ul)", re.I)
ENTITY = re.compile(r"&(?:#[0-9]{1,7}|#[xX][0-9A-Fa-f]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});")
EMPHASIS = set("*_~")
# CommonMark inline nesting is bounded in practice; a crafted body of thousands of nested labels must not make the
# gate quadratic. Beyond this depth the marks are dropped (fail-closed: the words still show).
MAX_DEPTH = 24
# A link label longer than this is not link syntax in any practical sense, and rendering thousands of long labels is
# what makes a crafted body quadratic. Beyond it the brackets are literal text (the words still show).
MAX_LABEL = 1024
# A comment longer than this is not a comment for the gate's purposes: its words render literally.
MAX_COMMENT = 2048


def ref_label(label: str) -> str:
    """A link reference label, compared the way CommonMark compares them: case-folded, inner whitespace collapsed."""
    return " ".join(label.split()).casefold()


def _decode_entity(token: str) -> str | None:
    """Strict CommonMark entity decoding: a named entity must be in the HTML5 table (with its semicolon) and a numeric
    one must be a valid code point. `html.unescape` alone is lenient -- it reads `&notanentity;` as "\u00acanententity;",
    inventing words the reader never saw."""
    body = token[1:-1]
    if body.startswith("#"):
        try:
            code = int(body[2:], 16) if body[:2].lower() == "#x" else int(body[1:])
        except ValueError:
            return None
        if code == 0 or code > 0x10FFFF or 0xD800 <= code <= 0xDFFF:
            return "\ufffd"
        return chr(code)
    return html.entities.html5.get(body + ";")


class _Scan:
    """Per-string lookup tables, so neither an unmatched bracket nor a long run of ticks can make rendering
    quadratic: a PR body is attacker-controlled text and the gate must not be stallable (found by fuzzing)."""

    def __init__(self, text: str):
        self.text = text
        # The last "-->" in the text: a comment candidate with no closer after it is rejected in O(1) instead of
        # rescanning to the end (a body of 20,000 "<!--" must not be quadratic).
        self.comment_closers = [m.start() for m in re.finditer(r"-->", text)]
        # Backtick runs by length, so a code span's closing run is found by lookup, not by rescanning.
        self.runs: dict[int, list[int]] = {}
        i = 0
        while (i := text.find("`", i)) != -1:
            j = i
            while j < len(text) and text[j] == "`":
                j += 1
            self.runs.setdefault(j - i, []).append(i)
            i = j
        # Brackets are paired in prose *outside code spans*: a "]" inside backticks is literal content, so it must
        # not close a "[" before them (a mis-pairing both hides and invents words).
        self.match: dict[int, int] = {}
        stack: list[int] = []
        i = 0
        while i < len(text):
            c = text[i]
            if c == "\\" and i + 1 < len(text):
                i += 2
                continue
            if c == "`":
                end = self.code_span_end(i)
                if end is not None:
                    i = end
                    continue
                while i < len(text) and text[i] == "`":
                    i += 1
                continue
            if c == "[":
                stack.append(i)
            elif c == "]" and stack:
                self.match[stack.pop()] = i
            i += 1

    def first_run_at_least(self, length: int, start: int) -> int | None:
        import bisect
        positions = self.runs.get(length)
        if not positions:
            return None
        k = bisect.bisect_left(positions, start)
        return positions[k] if k < len(positions) else None

    def comment_end(self, i: int) -> int | None:
        """The index past an HTML comment opening at `i`, or None when this is not one."""
        j = i + 4
        if self.text[j:j + 1] == ">" or self.text.startswith("->", j):
            return None  # "<!-->" and "<!--->" are not comments here: show their words
        k = self._first_closer(j)
        if k is None:
            return None
        body = self.text[j:k]
        if "\n" in body or "--" in body or ">" in body or body.endswith("-"):
            return None
        return k + 3

    def _first_closer(self, j: int) -> int | None:
        """The first "-->" at or after `j`, by binary search over the closers found in one pass."""
        import bisect
        k = bisect.bisect_left(self.comment_closers, j)
        if k >= len(self.comment_closers):
            return None
        close = self.comment_closers[k]
        # A comment is short in practice. A longer candidate is treated as prose (fail-closed: its words show), which
        # also keeps a body of thousands of "<!--" from rescanning to the same distant closer.
        return close if close - j <= MAX_COMMENT else None

    def code_span_end(self, i: int) -> int | None:
        """The index just past a code span that opens at `i`, or None when this run has no matching closer."""
        j = i
        while j < len(self.text) and self.text[j] == "`":
            j += 1
        run = j - i
        close = self.first_run_at_least(run, j)
        return None if close is None else close + run


def _code_span(text: str, i: int, scan: "_Scan") -> tuple[str, int] | None:
    """A code span's literal content and the index past it; one space trimmed when the content is padded."""
    end = scan.code_span_end(i)
    if end is None:
        return None
    run = 0
    while text[i + run] == "`":
        run += 1
    content = text[i + run:end - run]
    if len(content) >= 2 and content[0] == " " and content[-1] == " " and content.strip(" "):
        content = content[1:-1]
    return content.replace("\n", " "), end


def _destination(text: str, i: int) -> int | None:
    """The end of an inline link destination starting at `i` (just after the '('), or None. Per the spec a bare
    destination may contain balanced parentheses -- `u(a(bug))` is a destination, not visible words."""
    n = len(text)
    i = _skip_ws(text, i)
    if i < n and text[i] == "<":
        j = text.find(">", i + 1)
        if j == -1 or "\n" in text[i:j]:
            return None
        i = j + 1
    else:
        depth = 0
        while i < n:
            c = text[i]
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c in " \t\n":
                break
            if c == "(":
                depth += 1
            elif c == ")":
                if depth == 0:
                    break
                depth -= 1
            elif ord(c) < 0x20:
                return None
            i += 1
        if depth:
            return None
    # an optional title, then the closing paren
    j = _skip_ws(text, i)
    if j < n and j > i and text[j] in "\"'(":
        closer = ")" if text[j] == "(" else text[j]
        k = text.find(closer, j + 1)
        if k == -1:
            return None
        j = _skip_ws(text, k + 1)
    if j < n and text[j] == ")":
        return j + 1
    if j < n and j == i:
        return None
    # whitespace before the closing paren is allowed: "[a](b )"
    k = _skip_ws(text, i)
    return k + 1 if k < n and text[k] == ")" else None


def _skip_ws(text: str, i: int) -> int:
    while i < len(text) and text[i] in " \t\n":
        i += 1
    return i


def _scan_link_or_image(text: str, bracket: int, defined: frozenset, out: list[str], scan: "_Scan",
                        depth: int) -> int | None:
    """Link or image starting at '[' (`bracket`). Appends the visible label and returns the index past it, or None
    when this is not link syntax (the caller then shows the '[' as written)."""
    close = scan.match.get(bracket)
    if close is None or close - bracket > MAX_LABEL:
        return None
    label = text[bracket + 1:close]
    after = close + 1
    # inline link: `(` must follow the label *immediately* (cmark: "[a] (x)" is not a link)
    if after < len(text) and text[after] == "(":
        end = _destination(text, after + 1)
        if end is not None:
            if _contains_link(label, defined, depth):
                return None  # links may not contain links: the outer bracket is literal, the inner one wins
            out.append(render_inline(label, defined, depth + 1))
            return end
    # full or collapsed reference: `[label][ref]` / `[label][]`, again immediately
    if after < len(text) and text[after] == "[":
        ref_close = scan.match.get(after)
        if ref_close is not None and ref_close - after <= MAX_LABEL:
            ref = text[after + 1:ref_close]
            if ref_label(ref or label) in defined:
                if _contains_link(label, defined, depth):
                    return None
                out.append(render_inline(label, defined, depth + 1))
                return ref_close + 1
    # shortcut reference: the label itself is defined
    if ref_label(label) in defined:
        if _contains_link(label, defined, depth):
            return None
        out.append(render_inline(label, defined, depth + 1))
        return after
    return None


def _contains_link(label: str, defined: frozenset, depth: int) -> bool:
    """Whether a label holds another link (which would make the outer one literal, per the spec's nested-link rule)."""
    sub = _Scan(label)
    for start in sorted(sub.match):
        if start > 0 and label[start - 1] == "!":
            continue  # an image may sit inside a link label
        after = sub.match[start] + 1
        if after < len(label) and label[after] == "(" and _destination(label, after + 1) is not None:
            return True
        if ref_label(label[start + 1:sub.match[start]]) in defined:
            return True
    return False


def render_inline(text: str, defined: frozenset = frozenset(), depth: int = 0) -> str:
    """The visible words of one block of inline Markdown. Emphasis marks are dropped, so `do **not** merge` reads
    `do not merge`; an escaped mark is dropped too (`do \\*not\\* merge` still reads as a hold, the fail-closed #35
    choice); code span contents are kept literal; link and image labels stay and destinations go."""
    if depth >= MAX_DEPTH:
        return re.sub(r"[*_~`]", "", text)
    out: list[str] = []
    scan = _Scan(text)
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            if i + 1 < n and text[i + 1] == "\n":
                out.append("\n")  # a backslash at the end of a line is a hard line break, not an escape
                i += 2
                continue
            if i + 1 < n and text[i + 1] in ESCAPABLE:
                # An escaped character shows as itself, so `\<WIP>` displays `<WIP>` and `\`` is a literal backtick
                # (which is what stops it pairing with a real code-span opener). Escaped emphasis marks are the one
                # deliberate exception kept from #35: they are dropped, because joining the words can only reveal a
                # hold ("do \*not\* merge" still reads as "do not merge"), never hide one.
                if text[i + 1] not in EMPHASIS:
                    out.append(text[i + 1])
                i += 2
                continue
            out.append(c)  # a backslash before anything else is literal
            i += 1
            continue
        if c == "`":
            span = _code_span(text, i, scan)
            if span is None:
                while i < n and text[i] == "`":
                    i += 1  # unmatched backticks are delimiters that render nothing
                continue
            out.append(span[0])
            i = span[1]
            continue
        if c == "<":
            if text.startswith("<!--", i):
                end = scan.comment_end(i)
                if end is not None:
                    i = end
                    continue
            tag = (HTML_CDATA.match(text, i) or HTML_PI.match(text, i) or HTML_DECL.match(text, i)
                   or OPEN_TAG.match(text, i) or CLOSE_TAG.match(text, i))
            if tag:
                name = re.match(r"</?([A-Za-z][A-Za-z0-9-]*)", tag.group(0))
                out.append(" " if name and BLOCK_TAG.fullmatch(name.group(1)) else "")
                i = tag.end()
                continue
            auto = AUTOLINK_URI.match(text, i + 1) or AUTOLINK_EMAIL.match(text, i + 1)
            if auto and auto.end() < n and text[auto.end()] == ">":
                out.append(auto.group(0))
                i = auto.end() + 1
                continue
            out.append(c)
            i += 1
            continue
        if c == "!" and i + 1 < n and text[i + 1] == "[":
            if i + 1 in scan.match:
                consumed = _scan_link_or_image(text, i + 1, defined, out, scan, depth)
                if consumed is not None:
                    i = consumed
                    continue
            out.append(c)
            i += 1
            continue
        if c == "[":
            if i in scan.match:
                consumed = _scan_link_or_image(text, i, defined, out, scan, depth)
                if consumed is not None:
                    i = consumed
                    continue
            out.append(c)
            i += 1
            continue
        if c == "&":
            m = ENTITY.match(text, i)
            decoded = _decode_entity(m.group(0)) if m else None
            if decoded is not None:
                out.append(decoded)
                i = m.end()
                continue
            out.append(c)
            i += 1
            continue
        if c in EMPHASIS:
            i += 1  # emphasis and strikethrough marks are invisible
            continue
        out.append(c)
        i += 1
    return "".join(out)


def render(text: str, defined: frozenset = frozenset()) -> str:
    """The visible words of one prose block, with runs of spaces collapsed to one."""
    return re.sub(r"[ \t]+", " ", render_inline(text, defined))
