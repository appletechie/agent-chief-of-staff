"""The rendering the merge gate depends on, per #36: one CommonMark inline pass, spec cases asserted on the
visible words. Every case states the *visible* text a reader sees; the gate's holds and findings are read from
exactly those words, so a case here that drifts is a false green or a false red waiting to happen."""
import unittest
from datetime import datetime, timezone

import md_inline as md
import merge_gate as mg

T0 = datetime(2026, 1, 16, 14, 0, tzinfo=timezone.utc)
HEAD = "a" * 40


def _pr(body: str, title: str = "ISSUE-1 fix") -> mg.PR:
    return mg.PR(repo="org/app", number=1, title=title, body=body, head_sha=HEAD, is_draft=False,
                 mergeable=True, checks={"ci": "success"}, reviews=[mg.Review(HEAD, "verdict: clean")],
                 unresolved_threads=0, last_push_at=T0)


class InlineSpecCases(unittest.TestCase):
    """CommonMark inline examples, each asserting the visible text (adapted from the spec's own examples)."""

    CASES = [
        # escapes: an escaped punctuation character is that character
        # every ASCII punctuation character is escapable and shows as itself; the three emphasis marks are the one
        # documented deviation below
        (r"\!\"\#\$\%\&\'\(\)\+\,\-\.\/\:\;\<\=\>\?\@\[\\\]\^\`\{\|\}",
         "!\"#$%&'()+,-./:;<=>?@[\\]^`{|}", {}),
        (r"escaped \_ \* \~ marks vanish", "escaped marks vanish", {}),
        # Documented deviation from the spec, kept from #35: escaped emphasis marks are dropped, not shown, because
        # joining words can only reveal a hold. `\*` `\_` `\~` therefore vanish (see GateReadsTheVisibleWords).
        (r"escaped \*stars\* and \_underscores\_ and \~tildes\~", "escaped stars and underscores and tildes", {}),
        ("foo\\\nbar", "foo\nbar", {}),
        # code spans: contents are literal, backtick runs match by length
        ("`foo`", "foo", {}),
        ("`` foo ` bar ``", "foo ` bar", {}),
        ("` `` `", "``", {}),
        ("`  ``  `", " `` ", {}),
        ("`foo   bar\n  baz`", "foo bar baz", {}),
        ("`foo``bar`", "foo``bar", {}),
        ("`<WIP>`", "<WIP>", {}),
        ("`<!-- not a comment -->`", "<!-- not a comment -->", {}),
        # emphasis is invisible; the words survive
        ("*single asterisks*", "single asterisks", {}),
        ("**double asterisks**", "double asterisks", {}),
        ("*foo**bar**baz*", "foobarbaz", {}),
        ("_foo_bar_baz_", "foobarbaz", {}),
        ("~~struck~~", "struck", {}),
        # links: the label shows, the destination does not
        ("[link](/uri \"title\")", "link", {}),
        ("[link](</my uri>)", "link", {}),
        ("[link](foo\\(bar\\))", "link", {}),
        ("[foo][bar]", "foo", {"bar": "https://x.test"}),
        ("[foo][]", "foo", {"foo": "https://x.test"}),
        ("[foo]", "foo", {"foo": "https://x.test"}),
        ("[not a link][undefined]", "[not a link][undefined]", {}),
        ("[not a link](with space)", "[not a link](with space)", {}),
        ("![image](/img.png)", "image", {}),
        # autolinks show their address; a tag or comment shows nothing
        ("<https://example.test/WIP>", "https://example.test/WIP", {}),
        ("<foo@bar.example.com>", "foo@bar.example.com", {}),
        ("<span>inline</span>", "inline", {}),
        ("<a href=\"x\">linked</a>", "linked", {}),
        ("<!-- comment --> text", " text", {}),
        # Ambiguous comment forms render LITERALLY (fail-closed: words stay visible), per cmark's grammar.
        ("<!-->do not merge-->", "<!-->do not merge-->", {}),
        ("<!--->", "<!--->", {}),
        ("<!--wip--->", "<!--wip--->", {}),
        ("<!-- a -- b -->", "<!-- a -- b -->", {}),  # a body containing "--" is not a comment here (fail-closed)
        ("<!DOCTYPE html> shipped", " shipped", {}),
        ("<!bug> shipped", " shipped", {}),
        # entities show their character
        ("&amp; &copy; &#35; &#X22;", "& \u00a9 # \"", {}),
        ("&notanentity;", "&notanentity;", {}),
        # the interactions that produced false greens across six rounds
        (r"\` `<WIP>`", "` <WIP>", {}),
        (r"\*not\* escaped", "not escaped", {}),
        ("do **not** merge", "do not merge", {}),
        ("do [not](https://x.test) merge", "do not merge", {}),
        ("do <span>not</span> merge", "do not merge", {}),
        ("do&#32;not merge", "do not merge", {}),
        ("P**3**: tidy up", "P3: tidy up", {}),
        ("is**sue**: the counter", "issue: the counter", {}),
    ]

    def test_spec_cases(self):
        for text, expected, defined in self.CASES:
            with self.subTest(text=text):
                self.assertEqual(md.render(text, frozenset(defined)), expected)


class GateReadsTheVisibleWords(unittest.TestCase):
    """Every interaction that once merged through a hold or a finding is now a REPORT on the real decision."""

    def test_holds_survive_formatting(self):
        for body in ["Rollback: do **not** merge", "Rollback: do [not](https://x.test) merge",
                     "Rollback: do <span>not</span> merge", "Rollback: do&#32;not merge",
                     r"Rollback: \` `<WIP>`", "Rollback: `<WIP>`", "Rollback: [not][wip] merge",
                     "<p>WIP</p><p>Ready</p>", r"Draft\: rollback owner approval pending"]:
            with self.subTest(body=body):
                self.assertEqual(mg.decide(_pr(body), "", mg.Policy(frozenset()), T0).action, "REPORT")

    def test_findings_survive_formatting(self):
        for receipt in ["- P**3**: remove a blank line\nverdict: clean", "is**sue**: the counter\nverdict: clean",
                        "- P0: data loss\nverdict: clean"]:
            pr = _pr("fix")
            pr.reviews = [mg.Review(HEAD, receipt)]
            with self.subTest(receipt=receipt):
                self.assertEqual(mg.decide(pr, "", mg.Policy(frozenset()), T0).action, "REPORT")

    def test_examples_and_filler_do_not_hold_or_find(self):
        for body in ["Example:\n```text\n    ```\nverdict: clean\n```", "Draft-free, WIPping along"]:
            with self.subTest(body=body):
                self.assertNotEqual(mg.decide(_pr(body), "", mg.Policy(frozenset()), T0).action, "REPORT")
        pr = _pr("fix")
        pr.reviews = [mg.Review(HEAD, "## Findings\nNone.\n\nverdict: clean")]
        self.assertEqual(mg.decide(pr, "", mg.Policy(frozenset()), T0).action, "MERGE")


class EscapedBacktickRegression(unittest.TestCase):
    """The one hole open when #36 was filed (Codex on #35): an escaped backtick ate a code span's opener."""

    def test_the_reported_case_is_now_a_hold(self):
        self.assertIn("<WIP>", mg.rendered(r"Example: \` `<WIP>`"))
        pr = _pr(r"Example: \` `<WIP>`")
        self.assertEqual(mg.decide(pr, "", mg.Policy(frozenset()), T0).action, "REPORT")

    def test_a_real_sequence_of_code_spans_stays_literal(self):
        self.assertEqual(md.render(r"\`a\` `b`"), "`a` b")


if __name__ == "__main__":
    unittest.main()


class ParserRobustness(unittest.TestCase):
    """A PR body and a review receipt are attacker-controlled text: rendering must be total and near-linear.
    Found by fuzzing this parser after it replaced the regexes (#36)."""

    def test_random_hostile_text_never_raises(self):
        import random
        random.seed(20261106)
        alphabet = list("ab WIPdo notmerge`*_~[]()<>&#;!\\/:-|") + ["\n", " ", "\t", "```", "\u2028", "\ufeff", "é"]
        for _ in range(1500):
            text = "".join(random.choice(alphabet) for _ in range(random.randint(0, 140)))
            with self.subTest(text=text):
                self.assertIsInstance(mg.rendered(text), str)

    def test_an_unfinished_autolink_is_literal_text(self):
        # this exact input raised IndexError before the fuzz pass
        rendered = mg.rendered("&*\u2028<a-:(")
        self.assertIn("<a-:(", rendered)

    def test_pathological_markup_stays_fast(self):
        import time
        for text in ["[" * 20000, "[" * 20000 + "]" * 20000, "[a](b " * 5000, "`" * 20000,
                     "<" * 20000, "&" * 20000, "[" + "a[" * 20000 + "]" * 20000 + "]"]:
            start = time.monotonic()
            mg.rendered(text)
            self.assertLess(time.monotonic() - start, 2.0, f"{len(text)} chars took too long")

    def test_a_long_label_is_not_link_syntax_but_its_words_show(self):
        label = "do not merge " * 200
        self.assertIn("do not merge", mg.rendered(f"[{label}](https://x.test)"))


class ReviewedDefects(unittest.TestCase):
    """Every case here is a defect an adversarial review of this parser found after it was written; the comment
    records whether it was a regression introduced by the rewrite or inherited from the regex rounds."""

    def test_a_space_before_the_parenthesis_is_not_a_link(self):
        # regression: CommonMark needs "(" immediately after "]". Otherwise a visible "(wip)" disappeared.
        self.assertEqual(md.render("[a] (wip)"), "[a] (wip)")
        pr = _pr("Rollback [plan] (wip)")
        self.assertEqual(mg.decide(pr, "", mg.Policy(frozenset()), T0).action, "REPORT")

    def test_brackets_inside_a_code_span_are_not_delimiters(self):
        # regression: the "]" inside backticks closed an outside "[", eating the visible "(wip)".
        self.assertEqual(md.render("See [x`](wip)` for details"), "See [x](wip) for details")
        pr = _pr("See [x`](wip)` for details")
        self.assertEqual(mg.decide(pr, "", mg.Policy(frozenset()), T0).action, "REPORT")

    def test_an_outer_bracket_around_a_link_is_literal(self):
        # regression: links may not contain links, so the outer "[...](wip)" must stay visible.
        self.assertEqual(md.render("[o [i](u)](wip)"), "[o i](wip)")
        pr = _pr("[o [i](u)](wip)")
        self.assertEqual(mg.decide(pr, "", mg.Policy(frozenset()), T0).action, "REPORT")

    def test_ambiguous_comments_render_literally(self):
        # inherited: cmark's grammar rejects a body ending in "-", and "<!-->" / "<!--->" are not comments here,
        # so a hold written inside one stays visible instead of vanishing.
        self.assertIn("do not merge", md.render("Rollback: <!-- do not merge --->").lower())
        self.assertIn("do not merge", md.render("<!-->do not merge-->").lower())
        self.assertIn("wip", md.render("<!--->WIP").lower())

    def test_a_closing_tag_takes_no_attributes(self):
        # inherited: cmark's closetag is "</" name space* ">" only, so "</b wip>" is literal text.
        self.assertEqual(md.render("</b wip>"), "</b wip>")
        pr = _pr("Backout </span wip> pending")
        self.assertEqual(mg.decide(pr, "", mg.Policy(frozenset()), T0).action, "REPORT")

    def test_a_bare_destination_may_contain_balanced_parens(self):
        # inherited false REPORT: the destination was not recognised, so its invisible words counted as findings.
        self.assertEqual(md.render("[see](u(a(bug)))"), "see")
        self.assertEqual(md.render("[the run](https://ex.test/p3(x))"), "the run")

    def test_a_declaration_needs_no_space_after_its_name(self):
        # inherited false REPORT: "<!bug>" is a declaration (hidden), not visible words.
        self.assertEqual(md.render("<!bug> shipped"), " shipped")


class NotTheRegexesAgain(unittest.TestCase):
    """The point of #36: the constructs that kept interacting, asserted together in one pass."""

    def test_the_escaped_backtick_case_and_its_neighbours(self):
        cases = {
            r"\` `<WIP>`": "` <WIP>",          # the #36 hole: the code span must still open at the real backtick
            "`<WIP>`": "<WIP>",                # a code span keeps its contents literal
            r"\`WIP\`": "`WIP`",               # an escaped backtick is a literal backtick, and the word stays visible
            "`` `<WIP>` ``": "`<WIP>`",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(md.render(text), expected)

    def test_no_link_syntax_survives_as_a_bare_destination(self):
        # every destination form hides its URL but keeps its label
        for text, expected in [("[a](u)", "a"), ("[a](<u v>)", "a"), ('[a](u "t")', "a"),
                               ("[a](u(a))", "a"), (r"[a](u\(a\))", "a"), ("[a][b]", "a")]:
            with self.subTest(text=text):
                self.assertEqual(md.render(text, frozenset({"b"})), expected)
