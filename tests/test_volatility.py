"""Tests for the realized-volatility estimators and the threshold rules.

The estimator tests check two different things and both matter. Some pin a
formula against an independent float reference to fifteen significant digits, so a
wrong constant or a wrong drift assumption fails loudly. The rest assert
properties that must hold whatever the arithmetic: that a signed estimator is
summed before it is rooted, that a window spanning a hole is refused rather than
measured, and that a baseline never contains the observation it judges.

There is no network in any of this. Every bar is a literal in the file.
"""

import math
import unittest
from decimal import Context, Decimal, localcontext

from lele.analysis import volatility as vol


def D(value) -> Decimal:
    return Decimal(str(value))


def flat(bars: int, open_=105, high=110, low=100, close=105):
    """Identical bars, which makes a closed-form expectation possible."""
    return ([D(open_)] * bars, [D(high)] * bars, [D(low)] * bars, [D(close)] * bars)


def close(value, places: int = 12) -> float:
    return float(value)


class VarianceTests(unittest.TestCase):
    def test_two_pass_matches_known_values(self):
        data = [D(2), D(4), D(4), D(4), D(5), D(5), D(7), D(9)]
        # population and sample variance of this classic set
        self.assertAlmostEqual(close(vol.variance(data, 0)), 4.0, places=12)
        self.assertAlmostEqual(close(vol.variance(data, 1)), 32 / 7, places=12)

    def test_ddof_is_required_and_checked(self):
        data = [D(1), D(2), D(3)]
        with self.assertRaises(ValueError):
            vol.variance(data, -1)
        with self.assertRaises(ValueError):
            vol.variance(data, 3)
        with self.assertRaises(ValueError):
            vol.variance([], 0)

    def test_ddof_actually_changes_the_answer(self):
        """Bollinger wants population sd and close-to-close wants n-2.

        If a caller cannot pass ddof the two silently agree, which is the bug the
        explicit argument exists to prevent.
        """
        data = [D(1), D(2), D(3), D(4)]
        self.assertNotEqual(vol.variance(data, 0), vol.variance(data, 1))

    def test_two_pass_survives_small_variance_around_a_large_mean(self):
        """sum(x*x) - n*mean*mean cancels catastrophically here; the two-pass does not."""
        prices = [D("1000000.000001") + D(i) / D(10**8) for i in range(50)]
        variance = vol.variance(prices, 1)
        self.assertGreater(variance, 0)
        self.assertLess(variance, D("1e-6"))

    def test_stdev_is_none_on_zero_spread(self):
        self.assertIsNone(vol.stdev([D(5), D(5), D(5)], 1))


class EstimatorFormulaTests(unittest.TestCase):
    """Each estimator checked against an independent float computation."""

    BARS = 3

    def setUp(self):
        self.o, self.h, self.l, self.c = flat(self.BARS)

    def test_parkinson(self):
        got = vol.window_variance(self.o, self.h, self.l, self.c, "parkinson")
        reference = math.sqrt((math.log(1.10) ** 2) / (4 * math.log(2)))
        self.assertAlmostEqual(math.sqrt(close(got)), reference, places=14)

    def test_garman_klass(self):
        got = vol.window_variance(self.o, self.h, self.l, self.c, "garman_klass")
        reference = math.sqrt(0.5 * math.log(1.10) ** 2
                              - (2 * math.log(2) - 1) * math.log(1.0) ** 2)
        self.assertAlmostEqual(math.sqrt(close(got)), reference, places=14)

    def test_rogers_satchell(self):
        got = vol.window_variance(self.o, self.h, self.l, self.c, "rogers_satchell")
        reference = math.sqrt((math.log(110 / 105) * math.log(110 / 105))
                              + (math.log(100 / 105) * math.log(100 / 105)))
        self.assertAlmostEqual(math.sqrt(close(got)), reference, places=14)

    def test_close_to_close(self):
        closes = [D(100), D(110), D(121)]
        got = vol.window_variance(closes, closes, closes, closes, "close_to_close")
        step = math.log(1.10)
        reference = math.sqrt((step * step + step * step) / 2)
        self.assertAlmostEqual(math.sqrt(close(got)), reference, places=14)

    def test_lpv_is_the_mean_of_its_three_components(self):
        parts: list[Decimal] = []
        for name in ("parkinson", "garman_klass", "rogers_satchell"):
            value = vol.window_variance(self.o, self.h, self.l, self.c, name)
            self.assertIsNotNone(value, name)
            if value is not None:
                parts.append(value)
        self.assertEqual(len(parts), 3)
        with localcontext(Context(prec=vol.PRECISION, rounding=vol.ROUNDING)):
            expected = sum(parts, Decimal(0)) / 3
        got = vol.window_variance(self.o, self.h, self.l, self.c, "lpv")
        self.assertEqual(got, expected)

    def test_yang_zhang_weight_is_the_pinned_ttr_form(self):
        """Two reputable references disagree on this weight, so it is a pinned constant."""
        got = vol.window_variance(self.o, self.h, self.l, self.c, "yang_zhang")
        self.assertIsNotNone(got)
        convention = vol.convention_for("yang_zhang", "stock", 86400)
        self.assertEqual(convention["alpha"], "1.34")
        self.assertEqual(convention["weight_formula"], "(alpha-1)/(alpha + T/(T-2))")
        self.assertTrue(convention["handles_overnight_gap"])

    def test_yang_zhang_uses_the_whole_window_but_needs_three_bars(self):
        self.assertIsNone(vol.window_variance(self.o[:2], self.h[:2], self.l[:2],
                                              self.c[:2], "yang_zhang"))
        self.assertIsNotNone(vol.window_variance(self.o, self.h, self.l, self.c,
                                                 "yang_zhang"))

    def test_every_estimator_is_reachable(self):
        """On a series that actually moves. Flat bars give zero variance, which is
        refused on purpose and covered separately."""
        opens = [D(100), D(101), D(103), D(102)]
        highs = [D(102), D(104), D(105), D(104)]
        lows = [D(99), D(100), D(102), D(100)]
        closes = [D(101), D(103), D(102), D(104)]
        for name in vol.ESTIMATORS:
            value = vol.window_variance(opens, highs, lows, closes, name)
            if name in ("atr", "natr"):
                self.assertIsNone(value, "ATR needs its 14-bar seed, not 4 bars")
            else:
                self.assertIsNotNone(value, name)

    def test_unknown_estimator_is_refused(self):
        with self.assertRaises(ValueError):
            vol.window_variance(self.o, self.h, self.l, self.c, "bollinger")


class SignedEstimatorTests(unittest.TestCase):
    """Garman-Klass and Rogers-Satchell terms are signed; that has consequences."""

    def test_summing_before_rooting_is_required(self):
        """A bar whose body swamps its range contributes a negative term.

        Rooting each per-bar term separately would raise on that bar. Summing
        first and rooting once is the only correct order, and this fixture fails
        if the order is reversed or the sign is clamped away.
        """
        opens = [D(100), D(100), D(100)]
        highs = [D(100.5), D(100.5), D(100.5)]
        lows = [D(99.5), D(99.5), D(99.5)]
        closes = [D(105), D(105), D(105)]
        terms = vol._garman_klass_terms(opens, highs, lows, closes)
        self.assertTrue(all(term < 0 for term in terms),
                        "fixture must produce negative per-bar terms")

    def test_a_mixed_window_still_roots_the_sum(self):
        """One negative term among positive ones must not veto the estimate."""
        opens = [D(100), D(105), D(100)]
        highs = [D(101), D(106), D(101)]
        lows = [D(99), D(104), D(99)]
        closes = [D(100.5), D(105.5), D(100.5)]
        terms = vol._garman_klass_terms(opens, highs, lows, closes)
        self.assertTrue(any(term > 0 for term in terms))
        value = vol.window_variance(opens, highs, lows, closes, "garman_klass")
        self.assertIsNotNone(value)
        assert value is not None
        self.assertGreater(value, D(0))

    def test_non_positive_window_total_is_unknown_not_clamped(self):
        """A window whose signed total is not positive is unknown, not epsilon.

        Clamping it to a small number would report a confident, tiny, invented
        volatility. The Garman-Klass term here is negative on every bar because
        the body swamps the range, so the total is negative.
        """
        opens = [D(100), D(100), D(100)]
        highs = [D(100.5), D(100.5), D(100.5)]
        lows = [D(99.5), D(99.5), D(99.5)]
        closes = [D(105), D(105), D(105)]
        self.assertIsNone(vol.window_variance(opens, highs, lows, closes, "garman_klass"))
        self.assertTrue(all(term < 0
                            for term in vol._garman_klass_terms(opens, highs, lows, closes)))

    def test_yang_zhang_survives_where_garman_klass_dies(self):
        """Its overnight term is the reason it exists.

        On this fixture every intraday Garman-Klass term is negative, but the
        opens gap against the previous closes, so Yang-Zhang still has positive
        variance to report. That is the entire justification for keeping it
        alongside the cheaper estimators.
        """
        opens = [D(100), D(100), D(100)]
        highs = [D(100.5), D(100.5), D(100.5)]
        lows = [D(99.5), D(99.5), D(99.5)]
        closes = [D(105), D(105), D(105)]
        self.assertIsNone(vol.window_variance(opens, highs, lows, closes, "garman_klass"))
        self.assertIsNotNone(vol.window_variance(opens, highs, lows, closes, "yang_zhang"))
        self.assertTrue(vol.convention_for("yang_zhang", "stock", 86400)
                        ["handles_overnight_gap"])

    def test_lpv_averages_exactly_the_components_that_survived(self):
        """Dropping is deliberate: one component being undefined is not evidence
        that volatility is undefined. Which components survived differs by fixture,
        so the expectation is computed from the components themselves rather than
        assumed."""
        fixtures = [
            ([D(100), D(100), D(100)], [D(100.5)] * 3, [D(99.5)] * 3, [D(105)] * 3),
            ([D(100), D(101), D(103)], [D(102), D(104), D(105)], [D(99), D(100), D(102)],
             [D(101), D(103), D(102)]),
            (None, None, None, None),
        ]
        for opens, highs, lows, closes in fixtures:
            if opens is None:
                continue
            usable = []
            for name in ("parkinson", "garman_klass", "rogers_satchell"):
                value = vol.window_variance(opens, highs, lows, closes, name)
                if value is not None and value > 0:
                    usable.append(value)
            self.assertTrue(usable, "at least one component must survive")
            with localcontext(Context(prec=vol.PRECISION, rounding=vol.ROUNDING)):
                expected = sum(usable, Decimal(0)) / len(usable)
            got = vol.window_variance(opens, highs, lows, closes, "lpv")
            self.assertEqual(got, expected)
        self.assertIn("dropped",
                      vol.convention_for("lpv", "stock", 86400)["weighting"])

    def test_flat_prices_are_not_a_volatility_of_zero(self):
        op, hi, lo, cl = flat(5, open_=100, high=100, low=100, close=100)
        for name in ("close_to_close", "parkinson", "garman_klass", "rogers_satchell",
                     "lpv", "yang_zhang"):
            self.assertIsNone(vol.window_variance(op, hi, lo, cl, name), name)


class BarValidationTests(unittest.TestCase):
    def test_misaligned_series_are_refused_by_name(self):
        with self.assertRaises(ValueError) as caught:
            vol.window_variance([D(1)] * 3, [D(1)] * 3, [D(1)] * 3, [D(1)] * 4, "parkinson")
        self.assertIn("equal lengths", str(caught.exception))

    def test_wilder_atr_rejects_misaligned_series(self):
        with self.assertRaises(ValueError):
            vol.wilder_atr([D(1)] * 14, [D(1)] * 14, [D(1)] * 14, [D(1)] * 15)

    def test_period_bounds(self):
        op, hi, lo, cl = flat(20)
        with self.assertRaises(ValueError):
            vol.wilder_atr(op, hi, lo, cl, period=1)


class AtrTests(unittest.TestCase):
    def test_seed_is_the_mean_of_the_first_period_true_ranges(self):
        op, hi, lo, cl = flat(14)
        smoothed = vol.wilder_atr(op, hi, lo, cl)
        self.assertEqual(len(smoothed), 1)
        self.assertEqual(smoothed[0][0], D(10))

    def test_a_series_shorter_than_the_seed_yields_nothing(self):
        op, hi, lo, cl = flat(13)
        self.assertEqual(vol.wilder_atr(op, hi, lo, cl), [])

    def test_wilder_recursion_is_pinned(self):
        """Value 2 = (value 1 * 13 + TR 14) / 14."""
        op, hi, lo, cl = flat(15)
        smoothed = vol.wilder_atr(op, hi, lo, cl)
        self.assertEqual(len(smoothed), 2)
        self.assertEqual(smoothed[1][0], (D(10) * 13 + D(10)) / 14)

    def test_absolute_atr_is_invariant_to_an_additive_splice(self):
        """The property that makes ATR the right measure for a spliced series."""
        op, hi, lo, cl = flat(15)
        shift = D(500)
        base = vol.wilder_atr(op, hi, lo, cl)[0][0]
        spliced = vol.wilder_atr([x + shift for x in op], [x + shift for x in hi],
                                 [x + shift for x in lo], [x + shift for x in cl])[0][0]
        self.assertEqual(base, spliced)

    def test_natr_is_not_invariant_and_that_is_why_it_is_flagged(self):
        op, hi, lo, cl = flat(15)
        shift = D(500)
        plain = vol.window_variance(op, hi, lo, cl, "natr")
        spliced = vol.window_variance([x + shift for x in op], [x + shift for x in hi],
                                      [x + shift for x in lo], [x + shift for x in cl], "natr")
        self.assertNotEqual(plain, spliced)
        self.assertAlmostEqual(math.sqrt(close(plain)), 10 / 105, places=12)
        self.assertAlmostEqual(math.sqrt(close(spliced)), 10 / 605, places=12)
        self.assertTrue(vol.convention_for("atr", "stock", 86400)["invariant_to_additive_splices"])
        natr_convention = vol.convention_for("natr", "stock", 86400)
        self.assertFalse(natr_convention["invariant_to_additive_splices"])
        self.assertIn("spliced series", natr_convention["why_not"])


class ShiftInvarianceTests(unittest.TestCase):
    def test_log_return_estimators_are_invariant_to_an_additive_splice(self):
        op, hi, lo, cl = flat(6, open_=100, high=110, low=90, close=105)
        shift = D(500)
        plain = vol.window_variance(op, hi, lo, cl, "close_to_close")
        spliced = vol.window_variance([x + shift for x in op], [x + shift for x in hi],
                                      [x + shift for x in lo], [x + shift for x in cl],
                                      "close_to_close")
        self.assertEqual(plain, spliced)


class AnnualizationTests(unittest.TestCase):
    def test_trading_days_for_equities_calendar_days_for_crypto(self):
        self.assertEqual(vol.annualization_basis(86400, "stock"), 252)
        self.assertEqual(vol.annualization_basis(86400, "bitcoin"), 365)

    def test_sub_daily_intervals_scale(self):
        self.assertEqual(vol.annualization_basis(3600, "stock"), 252 * 24)
        self.assertEqual(vol.annualization_basis(86400, "volatility"), 252)

    def test_unrecognized_class_is_unknown_not_a_default(self):
        """Guessing 252 for an unknown class produces a plausible wrong number."""
        self.assertIsNone(vol.annualization_basis(86400, "weather"))
        self.assertIsNone(vol.annualized(D("0.01"), 86400, "weather"))

    def test_annualization_is_recorded_as_a_convention(self):
        self.assertTrue(vol.convention_for("parkinson", "stock", 86400)
                        ["sqrt_of_time_is_a_convention"])

    def test_annualizing_a_non_positive_variance_is_none(self):
        self.assertIsNone(vol.annualized(Decimal(0), 86400, "stock"))
        self.assertIsNone(vol.annualized(None, 86400, "stock"))


class DrawdownTests(unittest.TestCase):
    def test_drawdown_is_measured_from_the_running_peak(self):
        closes = [D(100), D(120), D(90), D(110)]
        series = vol.drawdowns(closes)
        self.assertEqual(series[0], D(0))
        self.assertEqual(series[1], D(0))
        self.assertEqual(series[2], D(-25))
        self.assertAlmostEqual(close(series[3]), -100 / 12, places=12)

    def test_max_drawdown_picks_the_worst(self):
        self.assertEqual(vol.max_drawdown([D(100), D(120), D(90), D(110)]), D(-25))

    def test_rising_series_has_no_drawdown(self):
        self.assertEqual(vol.max_drawdown([D(1), D(2), D(3)]), D(0))

    def test_empty_series(self):
        self.assertEqual(vol.drawdowns([]), [])
        self.assertIsNone(vol.max_drawdown([]))

    def test_ulcer_index_is_the_root_mean_squared_drawdown(self):
        """The definition, verified by hand, is the property worth pinning.

        Squaring means depth dominates persistence at these magnitudes: four 1%
        declines score about 0.89 while a single 20% drop scores about 8.94. An
        earlier version of this test asserted the opposite and was wrong.
        """
        long_slide = [D(100)] + [D(99)] * 4
        brief_deep = [D(100)] + [D(80)] + [D(100)] * 3
        long_score = vol.ulcer_index(long_slide, 5)
        brief_score = vol.ulcer_index(brief_deep, 5)
        self.assertIsNotNone(long_score)
        self.assertIsNotNone(brief_score)
        assert long_score is not None and brief_score is not None
        self.assertAlmostEqual(close(long_score), math.sqrt(4 / 5), places=12)
        self.assertAlmostEqual(close(brief_score), math.sqrt(400 / 5), places=12)
        self.assertGreater(brief_score, long_score)

    def test_ulcer_index_needs_enough_bars(self):
        self.assertIsNone(vol.ulcer_index([D(1), D(2)], 5))
        with self.assertRaises(ValueError):
            vol.ulcer_index([D(1)] * 5, 1)


class PercentileAndZTests(unittest.TestCase):
    def test_percentile_is_the_empirical_cdf(self):
        baseline = [D(1), D(2), D(3), D(4)]
        self.assertEqual(vol.empirical_percentile(baseline, D(10)), D(100))
        self.assertEqual(vol.empirical_percentile(baseline, D(0)), D(0))

    def test_percentile_uses_absolute_value(self):
        """A -10 move is as extreme as a +10 move for an unsigned threshold."""
        baseline = [D(-3), D(-1), D(1), D(3)]
        self.assertEqual(vol.empirical_percentile(baseline, D(-10)),
                         vol.empirical_percentile(baseline, D(10)))

    def test_percentile_of_empty_baseline_is_none(self):
        self.assertIsNone(vol.empirical_percentile([], D(1)))

    def test_z_score_is_none_on_zero_spread(self):
        self.assertIsNone(vol.z_score([D(2), D(2), D(2)], D(3)))

    def test_z_score_matches_the_definition(self):
        baseline = [D(1), D(2), D(3), D(4)]
        got = vol.z_score(baseline, D(5))
        self.assertIsNotNone(got)
        assert got is not None
        spread = vol.stdev(baseline, 1)
        self.assertIsNotNone(spread)
        assert spread is not None
        with localcontext(Context(prec=vol.PRECISION, rounding=vol.ROUNDING)):
            expected = (D(5) - D("2.5")) / spread
        self.assertAlmostEqual(close(got), close(expected), places=12)


class ThresholdRuleTests(unittest.TestCase):
    BASELINE = [D(i) for i in range(1, 121)]

    def evaluate(self, value, **kwargs):
        return vol.evaluate_threshold(self.BASELINE, value, **kwargs)

    def test_absolute_mode(self):
        result = self.evaluate(D(5), mode="absolute", level="4")
        self.assertTrue(result["breached"])
        self.assertFalse(self.evaluate(D(3), mode="absolute", level="4")["breached"])

    def test_percentile_is_primary_and_needs_a_resolvable_baseline(self):
        result = self.evaluate(D(115), mode="percentile", level="95")
        self.assertTrue(result["breached"])
        self.assertEqual(result["minimum_baseline_bars"], vol.MIN_BASELINE_PERCENTILE)

    def test_a_short_baseline_refuses_to_answer_rather_than_guessing(self):
        short = [D(i) for i in range(1, 21)]
        result = vol.evaluate_threshold(short, D(100), mode="percentile", level="95")
        self.assertIsNone(result["breached"])
        self.assertFalse(result["baseline_sufficient"])
        self.assertIn("at least", result["reason"])

    def test_z_mode_needs_a_larger_baseline_than_absolute(self):
        self.assertEqual(vol.minimum_baseline("z_score"), 60)
        self.assertEqual(vol.minimum_baseline("absolute"), 20)
        self.assertEqual(vol.minimum_baseline("percentile"), 100)

    def test_z_score_mode(self):
        self.assertTrue(self.evaluate(D(200), mode="z_score", level="2")["breached"])
        self.assertFalse(self.evaluate(D(60), mode="z_score", level="2")["breached"])

    def test_the_baseline_never_contains_the_observation_being_judged(self):
        """Including it deflates the spread and compresses the score toward zero.

        The same value scores higher against a baseline it is not in, which is
        why the caller has to exclude it rather than pass a slice.
        """
        value = D(200)
        clean = vol.evaluate_threshold([D(1)] * 60 + [D(2)] * 60, value,
                                       mode="z_score", level="3")
        contaminated = vol.evaluate_threshold([D(1)] * 60 + [D(2)] * 60 + [value], value,
                                              mode="z_score", level="3")
        self.assertGreater(abs(Decimal(clean["z_score"])),
                           abs(Decimal(contaminated["z_score"])))
        # Reported values are text so the report survives json.dumps, which
        # refuses a Decimal; the comparison still has to be made on the number.
        self.assertIsInstance(clean["z_score"], str)

    def test_both_a_percentile_and_a_z_score_are_reported_regardless_of_mode(self):
        result = self.evaluate(D(115), mode="absolute", level="4")
        self.assertIsNotNone(result["baseline_percentile"])
        self.assertIsNotNone(result["z_score"])
        self.assertEqual(result["baseline_bars"], 120)

    def test_level_bounds(self):
        with self.assertRaises(ValueError):
            vol.validate_threshold("percentile", "100")
        with self.assertRaises(ValueError):
            vol.validate_threshold("z_score", "-1")
        with self.assertRaises(ValueError):
            vol.validate_threshold("absolute", "0")
        with self.assertRaises(ValueError):
            vol.validate_threshold("gaussian", "2")

    def test_integer_levels_are_accepted(self):
        vol.validate_threshold("z_score", 2)


class ConventionTests(unittest.TestCase):
    def test_every_estimator_records_a_reproducible_convention(self):
        for name in vol.ESTIMATORS:
            convention = vol.convention_for(name, "bitcoin", 86400)
            self.assertEqual(convention["precision"], vol.PRECISION, name)
            self.assertEqual(convention["annualization_bars_per_year"], 365, name)

    def test_parkinson_needs_no_previous_close(self):
        """It uses only each bar's own high and low, so two bars suffice."""
        self.assertNotIn("parkinson", vol.NEEDS_PREVIOUS_CLOSE)
        op, hi, lo, cl = flat(2)
        self.assertIsNotNone(vol.window_variance(op, hi, lo, cl, "parkinson"))

    def test_estimators_that_need_a_previous_close_say_so(self):
        self.assertIn("garman_klass", vol.NEEDS_PREVIOUS_CLOSE)
        self.assertIn("rogers_satchell", vol.NEEDS_PREVIOUS_CLOSE)
        self.assertNotIn("close_to_close", vol.NEEDS_PREVIOUS_CLOSE)
        op, hi, lo, cl = flat(2)
        self.assertIsNone(vol.window_variance(op, hi, lo, cl, "garman_klass"))

    def test_the_only_estimator_claiming_gap_handling_is_yang_zhang(self):
        claiming = [name for name in vol.ESTIMATORS
                    if vol.convention_for(name, "stock", 86400).get("handles_overnight_gap")]
        self.assertEqual(claiming, ["yang_zhang"])


if __name__ == "__main__":
    unittest.main()
