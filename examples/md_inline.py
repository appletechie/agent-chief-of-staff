#!/usr/bin/env python3
"""One small CommonMark **inline** pass, standard library only (#36).

The merge gate reads hold markers and finding words in the words a reviewer or owner would *see*. Rendering those
words with a stack of regexes kept interacting badly (an escaped backtick swallowing a code span's opener let a
visible `<WIP>` disappear), so this walks the text once, in the spec's precedence order, and returns the visible
text:

    backslash escapes -> code spans -> autolinks, raw HTML -> links and images -> entities -> emphasis

It deliberately renders **inline** content only; block structure stays with the caller (`merge_gate.rendered`),
which already splits fences and indented code and collects link reference definitions from prose. Fail-closed
choices from #35 are kept: an escaped punctuation character shows as itself, a code span keeps its contents
literal, and a raw HTML tag or comment renders as nothing (a block-level tag as a space).
"""
from __future__ import annotations

import html
import html.entities
import re
from dataclasses import dataclass, field

# CommonMark: a backslash escape exists only before ASCII punctuation.
ESCAPABLE = set("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")
# An autolink is an absolute URI or an email address; a tag is an open/close HTML tag.
AUTOLINK_URI = re.compile(r"[A-Za-z][A-Za-z0-9+.-]{1,31}:[^<>\x00-\x20]*")
AUTOLINK_EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                            r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*")
# A tag name, then any attributes that contain no ">" (or a quoted string that may contain one).
HTML_TAG = re.compile(r"</?[A-Za-z][A-Za-z0-9-]*(?:\s+[A-Za-z_:][A-Za-z0-9_.:-]*(?:\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s\"'=<>`]+))?)*\s*/?>")
HTML_COMMENT = re.compile(r"<!-->|<!--->|<!--(?:[^-]|-(?!->))*-->", re.S)
HTML_DECL = re.compile(r"<![A-Za-z]+\s[^>]*>")
HTML_PI = re.compile(r"<\?.*?\?>", re.S)
HTML_CDATA = re.compile(r"<!\[CDATA\[.*?\]\]>", re.S)
# Block-level tags separate what they enclose, so they read as a space; inline tags read as nothing.
HTML_BLOCK = re.compile(r"(?:address|article|aside|blockquote|br|caption|dd|details|dialog|div|dl|dt|fieldset|figcaption"
                        r"|figure|footer|form|h[1-6]|header|hr|li|main|nav|ol|p|pre|section|summary|table|tbody|td|tfoot"
                        r"|th|thead|tr|ul)", re.I)
ENTITY = re.compile(r"&(?:#[0-9]{1,7}|#[xX][0-9A-Fa-f]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});")


def _decode_entity(token: str) -> str | None:
    """Strict CommonMark entity decoding: a named entity must be in the HTML5 table (with its semicolon), and a
    numeric one must be a valid code point. `html.unescape` alone is lenient and would read `&notanentity;` as
    "¬anententity;", inventing words the reader never saw."""
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
# An inline link's destination may be bare (no spaces or parentheses) or wrapped in < >; then an optional title.
# A bare destination may contain backslash-escaped parentheses, and may not contain unescaped spaces or parens.
DEST = r"(?:<[^<>\n]*>|(?:[^\s\x00-\x20()\\]|\\.)*)(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^()]*\)))?"
OPEN_LABEL = re.compile(r"!?\[")
LINK_AFTER_LABEL = re.compile(r"\(\s*" + DEST + r"\s*\)")
REF_AFTER_LABEL = re.compile(r"\[([^\]]*)\]")
# emphasis delimiters, per the spec's left/right-flanking rules
PUNCT = set("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")


class _Scan:
    """Per-string lookup tables, so an unmatched bracket or a long run of ticks cannot make rendering quadratic.
    A PR body is attacker-controlled text; the gate must not be stallable by 2000 open brackets (found by fuzzing)."""

    def __init__(self, text: str):
        self.text = text
        self.last_bracket = text.rfind("]")
        # match[i] is the index of the ']' that closes the '[' at i, pairing brackets the way CommonMark nests
        # them (escaped brackets are literal). One pass, so rendering stays linear on hostile input.
        self.match: dict[int, int] = {}
        stack: list[int] = []
        i = 0
        while i < len(text):
            c = text[i]
            if c == "\\" and i + 1 < len(text):
                i += 2
                continue
            if c == "[":
                stack.append(i)
            elif c == "]" and stack:
                self.match[stack.pop()] = i
            i += 1
        self.runs: dict[int, list[int]] = {}
        i = 0
        while (i := text.find("`", i)) != -1:
            j = i
            while j < len(text) and text[j] == "`":
                j += 1
            self.runs.setdefault(j - i, []).append(i)
            i = j

    def first_run_at_least(self, length: int, start: int) -> int | None:
        import bisect
        positions = self.runs.get(length)
        if not positions:
            return None
        k = bisect.bisect_left(positions, start)
        return positions[k] if k < len(positions) else None


@dataclass
class _Ctx:
    defined: frozenset = field(default_factory=frozenset)
    """Normalized reference labels defined in this document's prose (a definition inside code defines nothing)."""


def ref_label(label: str) -> str:
    """A link reference label, compared the way CommonMark compares them: case-folded, inner whitespace collapsed."""
    return " ".join(label.split()).casefold()


def _scan_backtick_run(text: str, i: int) -> int:
    j = i
    while j < len(text) and text[j] == "`":
        j += 1
    return j


def _unclosed_backtick_run(text: str, i: int) -> int:
    """Per the spec an unmatched run is literal text; consume it so its backticks are not read as delimiters again."""
    return _scan_backtick_run(text, i)


def _find_code_close(text: str, i: int, run_len: int) -> int | None:
    """The start of a closing run: the same length, and (per the spec) not part of a longer run."""
    j = i
    while j < len(text):
        if text[j] == "`":
            start = j
            end = _scan_backtick_run(text, j)
            if end - start == run_len:
                return start
            j = end
        else:
            j += 1
    return None


def _scan_code_span(text: str, i: int, out: list[str], scan: "_Scan | None" = None) -> int | None:
    """A code span: `run` + content + `run`, contents literal, one leading/trailing space stripped per the spec."""
    run_end = _scan_backtick_run(text, i)
    run_len = run_end - i
    close = scan.first_run_at_least(run_len, run_end) if scan is not None else _find_code_close(text, run_end, run_len)
    if close is None:
        return None
    content = text[run_end:close]
    if len(content) >= 2 and content[0] == " " and content[-1] == " " and content.strip(" "):
        content = content[1:-1]
    content = content.replace("\n", " ")
    out.append(content)
    return close + run_len


def _scan_link_label(text: str, i: int, last_bracket: int = -1, close: int | None = None) -> tuple[str, int] | None:
    """A bracketed label: returns its content and the index after the ']'. A precomputed `close` (the paired ']')
    makes this O(1); without one it scans, handling nesting and escapes."""
    if close is not None:
        return text[i + 1:close], close + 1
    if last_bracket >= 0 and last_bracket < i + 1:
        return None  # no closing bracket at all: this '[' is literal, and rescans are what make it quadratic
    depth, j, buf = 0, i + 1, []
    while j < len(text):
        c = text[j]
        if c == "\\" and j + 1 < len(text) and text[j + 1] in ESCAPABLE:
            buf += [text[j + 1]]
            j += 2
            continue
        if c == "[":
            depth += 1
            buf.append(c)
        elif c == "]":
            if depth == 0:
                return "".join(buf), j + 1
            depth -= 1
            buf.append(c)
        else:
            buf.append(c)
        j += 1
    return None


def _skip_space(text: str, i: int) -> int:
    while i < len(text) and text[i] in " \t\n":
        i += 1
    return i


def _label_span(text: str, start: int, last_bracket: int = -1, close: int | None = None) -> tuple[int, int] | None:
    """The [start, end) of a bracketed label beginning at `start` ('[' or '![' handled by the caller)."""
    label = _scan_link_label(text, start, last_bracket, close)
    if label is None:
        return None
    return start, label[1]


# CommonMark inline nesting is bounded in practice; a crafted body of thousands of nested labels must not make
# the gate quadratic. Beyond this depth the remaining marks are simply dropped (fail-closed: words still show).
MAX_DEPTH = 24

# A link label longer than this is not link syntax in any practical sense, and rendering thousands of long labels
# is what makes a crafted body quadratic. Beyond it the brackets are literal text (words still show).
MAX_LABEL = 1024


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
                out.append("\n")  # a backslash at the end of a line is a hard line break (not an escape)
                i += 2
                continue
            if i + 1 < n and text[i + 1] in ESCAPABLE:
                # An escaped character shows as itself, so `\<WIP>` displays `<WIP>` and `\`` is a literal backtick
                # (which is what stops it pairing with a real code-span opener). Escaped emphasis marks are the
                # one deliberate exception kept from #35: they are dropped, because joining the words can only
                # reveal a hold ("do \*not\* merge" still reads as "do not merge"), never hide one.
                if text[i + 1] not in "*_~":
                    out.append(text[i + 1])
                i += 2
            else:
                out.append(c)  # a backslash before anything else is literal
                i += 1
            continue
        if c == "`":
            after = _scan_code_span(text, i, out, scan)
            if after is None:
                i = _unclosed_backtick_run(text, i)
                out.append(text[i - (i - i):i] if False else "")  # unmatched backticks are not visible words
                continue
            i = after
            continue
        if c == "<":
            tag = HTML_COMMENT.match(text, i) or HTML_CDATA.match(text, i) or HTML_PI.match(text, i) \
                or HTML_DECL.match(text, i) or HTML_TAG.match(text, i)
            if tag:
                name = re.match(r"</?([A-Za-z][A-Za-z0-9-]*)", tag.group(0))
                out.append(" " if name and HTML_BLOCK.fullmatch(name.group(1)) else "")
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
            if i + 1 not in scan.match:
                out.append(c)  # "!" before an unmatched "[" is literal
                i += 1
                continue
            consumed = _scan_link_or_ref(text, i + 1, defined, out, image=True, last_bracket=scan.last_bracket,
                                         close=scan.match[i + 1], depth=depth)
            if consumed is None:
                out.append("!")
                i += 1
                continue
            i = consumed
            continue
        if c == "[":
            if i not in scan.match:
                out.append(c)  # unmatched "[" is literal; scanning to the end of the text for it is quadratic
                i += 1
                continue
            consumed = _scan_link_or_ref(text, i, defined, out, image=False, last_bracket=scan.last_bracket,
                                         close=scan.match[i], depth=depth)
            if consumed is None:
                out.append(c)
                i += 1
                continue
            i = consumed
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
        # emphasis and strikethrough marks are invisible; every other character is shown as written
        if c in "*_~":
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _scan_link_or_ref(text: str, bracket: int, defined: frozenset, out: list[str], image: bool,
                      last_bracket: int = -1, close: int | None = None, depth: int = 0) -> int | None:
    """A link/image starting at '[' (bracket index). Appends the visible label and returns the end, or None when
    this is not link syntax (then the caller shows the '[' as written)."""
    span = _label_span(text, bracket, last_bracket, close)
    if span is None or span[1] - span[0] > MAX_LABEL:
        return None
    _, after_label = span
    label_text = text[bracket + 1:after_label - 1]
    inline = LINK_AFTER_LABEL.match(text, _skip_space(text, after_label))
    if inline:
        out.append(render_inline(label_text, defined, depth + 1))
        return inline.end()
    # a reference link: [label][ref], [label][], or a shortcut [label] when the label is defined
    ref_m = REF_AFTER_LABEL.match(text, after_label)
    if ref_m:
        ref = ref_m.group(1) or label_text
        if ref_label(ref) in defined:
            out.append(render_inline(label_text, defined, depth + 1))
            return ref_m.end()
    if ref_label(label_text) in defined:
        out.append(render_inline(label_text, defined, depth + 1))
        return after_label
    return None


def render(text: str, defined: frozenset = frozenset()) -> str:
    """The visible words of ``text`` (one prose block), with runs of spaces collapsed to one."""
    return re.sub(r"[ \t]+", " ", render_inline(text, defined))
