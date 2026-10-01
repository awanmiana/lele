"""Tests for the framework reference layer.

This module is unusual in the project: it holds no measurement and drives nothing.
Its tests are therefore about *discipline* rather than arithmetic -- that every
entry carries a source, a grade and a stated limit, that no excluded claim leaks
back in, and that no number that would read as advice appears anywhere.

The last of those is enforced mechanically. A framework note that quotes a return,
a Sharpe ratio or a price target would turn a reference into a recommendation, and
a regex over the source is the cheapest way to make that impossible to do quietly.
"""

import re
import unittest

from lele.analysis import framework_notes as fn


class StructureTests(unittest.TestCase):
    def test_every_entry_carries_a_source_a_grade_and_a_limit(self):
        for note in fn.NOTES:
            with self.subTest(note=note["key"]):
                self.assertTrue(note["title"].strip())
                self.assertTrue(note["attribution"].strip())
                self.assertTrue(note["source"].strip())
                self.assertTrue(note["grade"] in fn.GRADES)
                self.assertTrue(note["source_kind"].strip())
                self.assertTrue(note["finding"].strip())
                self.assertTrue(note["what_it_does_not_establish"].strip())
                self.assertTrue(note["bears_on"])
                self.assertTrue(note["url"] or note["identifier"])

    def test_keys_are_unique(self):
        keys = [note["key"] for note in fn.NOTES]
        self.assertEqual(len(keys), len(set(keys)))

    def test_every_bears_on_topic_is_lowercase_and_underscored(self):
        for note in fn.NOTES:
            for topic in note["bears_on"]:
                with self.subTest(note=note["key"], topic=topic):
                    self.assertRegex(topic, r"^[a-z0-9_]+$")

    def test_the_replication_entry_points_at_what_it_supersedes(self):
        original = fn.get_note("volatility_managed_portfolios")
        replication = fn.get_note("volatility_managed_replication_failure")
        self.assertEqual(original["superseded_by"], "volatility_managed_replication_failure")
        self.assertEqual(replication["supersedes"], "volatility_managed_portfolios")

    def test_a_superseding_note_cannot_point_at_something_absent(self):
        for note in fn.NOTES:
            for field in ("supersedes", "superseded_by"):
                target = note.get(field)
                if target:
                    with self.subTest(note=note["key"], field=field):
                        self.assertIsNotNone(fn.get_note(target))

    def test_the_vendor_entry_names_the_interested_party(self):
        """A vendor claim has to say whose product it sells, or the grade is decorative."""
        vendor = [note for note in fn.NOTES if note["grade"] == "vendor"]
        self.assertTrue(vendor)
        for note in vendor:
            text = " ".join((note["source_kind"], note["finding"],
                             note["what_it_does_not_establish"])).lower()
            with self.subTest(note=note["key"]):
                self.assertTrue("commercial terms" in text or "conflict" in text
                                or "largest index provider" in text,
                                "a vendor entry must name the interest")


class ExclusionTests(unittest.TestCase):
    def test_every_exclusion_states_a_reason_and_a_source_of_circulation(self):
        for item in fn.EXCLUDED:
            with self.subTest(claim=item["claim"][:40]):
                self.assertTrue(item["claim"].strip())
                self.assertTrue(item["reason"].strip())
                self.assertTrue(item["where_it_circulates"].strip())

    def test_an_excluded_claim_is_not_also_asserted_as_a_note(self):
        """The failure mode is citing something as excluded and using it anyway."""
        asserted = " ".join((note["finding"] + " " + note["title"]).lower()
                            for note in fn.NOTES)
        for item in fn.EXCLUDED:
            probe = item["claim"].split("(")[0].strip().lower()
            head = " ".join(probe.split()[:4])
            with self.subTest(claim=item["claim"][:40]):
                self.assertNotIn(head, asserted)

    def test_the_unverifiable_specific_numbers_are_named(self):
        """These are the numbers that circulate with a citation attached."""
        claims = " ".join(item["claim"].lower() for item in fn.EXCLUDED)
        for phrase in ("1/5/6", "doubled the sharpe", "jay cooke", "bogle",
                       "greenwald", "magic formula", "wash-sale"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, claims)


class NoAdviceTests(unittest.TestCase):
    """Mechanically enforced: nothing here may read as advice."""

    #: A quoted or implied performance figure. Percentages and multiples are the
    #: ones that matter; a journal volume number like 129(3) is not a return.
    #: Directives only. The bare word "recommendation" is excluded deliberately:
    #: this module's own disclaimers have to be able to deny making one.
    FORBIDDEN = re.compile(
        r"(?:expected return of|expected sharpe of|target price|price target|"
        r"you (?:should|must|ought to) (?:buy|sell|trade|invest)|we recommend|we advise|"
        r"worth investing|is a good (?:buy|sell)|attractive entry)", re.I)
    #: A bare "x% per year" or "Sharpe of x" claim.
    PERFORMANCE_CLAIM = re.compile(
        r"sharpe (?:ratio )?(?:of|was) [\d.]+|[returns] [\d.]+ ?% per (?:year|annum)", re.I)

    def scanned_text(self):
        """The asserted content, excluding this module's own disclaimers.

        The limitations and `NOT_A_SIGNAL` blocks necessarily contain the words
        "recommendation" and "price target" in order to deny making one, so
        scanning raw source would fail on the very text that keeps the promise. A
        directive hiding in the disclaimer block is caught by
        `test_the_disclaimers_themselves_contain_no_directive` instead.
        """
        parts = []
        for note in fn.NOTES:
            parts.extend([note["title"], note["finding"], note["what_it_does_not_establish"]])
        parts.extend(fn.METHODOLOGICAL_NOTES)
        for item in fn.EXCLUDED:
            parts.extend([item["claim"], item["reason"]])
        return " ".join(parts)

    def test_no_recommendation_language_in_any_asserted_claim(self):
        hit = self.FORBIDDEN.search(self.scanned_text())
        self.assertIsNone(hit, f"directive found: {hit.group(0) if hit else ''}")

    def test_the_disclaimers_themselves_contain_no_directive(self):
        """A negation is not a directive.

        Clauses that deny a figure or a recommendation are the point of a
        disclaimer, so they are allowed to name the thing they rule out.
        """
        joined = " ".join(fn.NOT_A_SIGNAL)
        joined += " " + " ".join(fn.report()["limitations"])
        for clause in joined.split("."):
            text = clause.strip()
            if not text:
                continue
            negated = any(word in text.lower() for word in
                          ("no ", "not ", "never", "nor ", "nothing"))
            with self.subTest(clause=text[:60]):
                self.assertTrue(negated or self.FORBIDDEN.search(text) is None,
                                "a directive outside a negation: " + text[:80])

    def test_no_sharpe_or_return_claim_in_any_entry(self):
        for note in fn.NOTES:
            for field in ("title", "finding", "what_it_does_not_establish"):
                with self.subTest(note=note["key"], field=field):
                    hit = self.PERFORMANCE_CLAIM.search(note[field])
                    self.assertIsNone(hit, f"{note['key']}.{field}: {hit.group(0) if hit else ''}")

    def test_the_published_figures_that_may_be_quoted_are_the_documented_ones(self):
        """The two research figures that are legitimately quotable, each with its
        identifier present, so a reader can check them."""
        concentration = fn.get_note("wealth_concentration")
        self.assertIn("4%", concentration["finding"])
        self.assertTrue(concentration["identifier"])
        trading = fn.get_note("trading_is_hazardous")
        self.assertIn("11.4", trading["finding"])
        self.assertIn("17.9", trading["finding"])
        self.assertTrue(trading["identifier"])

    def test_the_not_a_signal_contract_is_explicit(self):
        self.assertGreaterEqual(len(fn.NOT_A_SIGNAL), 4)
        joined = " ".join(fn.NOT_A_SIGNAL).lower()
        for phrase in ("no signal", "investment", "advice", "not a validated technique"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, joined)

    def test_the_methodological_notes_keep_the_target_as_an_aspiration(self):
        joined = " ".join(fn.METHODOLOGICAL_NOTES).lower()
        self.assertIn("0.9", joined)
        self.assertIn("aspiration", joined)
        self.assertIn("lowered", joined)


class QueryTests(unittest.TestCase):
    def test_report_shape(self):
        report = fn.report()
        self.assertEqual(report["method"], fn.METHOD)
        self.assertEqual(report["note_count"], len(fn.NOTES))
        self.assertEqual(report["excluded_count"], len(fn.EXCLUDED))
        self.assertEqual(sum(report["by_grade"].values()), len(fn.NOTES))
        self.assertTrue(report["limitations"])

    def test_filter_by_grade(self):
        primary = fn.list_notes(grade="primary")
        self.assertTrue(primary)
        self.assertTrue(all(note["grade"] == "primary" for note in primary))
        self.assertLess(len(primary), len(fn.NOTES))

    def test_filter_by_topic(self):
        notes = fn.list_notes(topic="backtesting")
        self.assertTrue(notes)
        self.assertTrue(all("backtesting" in note["bears_on"] for note in notes))

    def test_combined_filters(self):
        notes = fn.list_notes(grade="primary", topic="backtesting")
        self.assertTrue(notes)
        for note in notes:
            self.assertEqual(note["grade"], "primary")
            self.assertIn("backtesting", note["bears_on"])

    def test_no_match_is_empty_not_an_error(self):
        self.assertEqual(fn.list_notes(topic="no_such_topic"), [])

    def test_bad_grade_is_refused(self):
        with self.assertRaises(ValueError):
            fn.list_notes(grade="excellent")

    def test_unknown_key_is_none(self):
        self.assertIsNone(fn.get_note("nope"))
        self.assertIsNotNone(fn.get_note(fn.NOTES[0]["key"]))

    def test_returned_dicts_are_copies(self):
        first = fn.get_note(fn.NOTES[0]["key"])
        first["title"] = "mutated"
        self.assertNotEqual(fn.get_note(fn.NOTES[0]["key"])["title"], "mutated")


if __name__ == "__main__":
    unittest.main()
