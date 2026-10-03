"""Offline tests for the enrichment statistics: control design, permutation, correction.

These pin the properties that stop the profile from manufacturing findings. Each
test states the way a plausible implementation would produce a false positive.
"""
import random
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, UTC

from lele.analysis import causes

KEY = "binance:BTCUSDT"
BASE = datetime(2026, 1, 1, tzinfo=UTC)


class BenjaminiHochbergTests(unittest.TestCase):

    def test_a_strong_signal_survives_and_weak_ones_do_not(self):
        adjusted = causes.benjamini_hochberg([0.001, 0.30, 0.60, 0.80, 0.95])
        self.assertLess(adjusted[0], causes.ALPHA)
        for value in adjusted[1:]:
            self.assertGreater(value, causes.ALPHA)

    def test_a_borderline_value_is_corrected_away_only_in_a_family(self):
        alone = causes.benjamini_hochberg([0.04])
        self.assertEqual(alone, [0.04], "one test needs no correction")
        crowded = causes.benjamini_hochberg([0.04] + [0.9] * 9)[0]
        self.assertGreater(crowded, causes.ALPHA)

    def test_adjusted_values_are_never_below_raw_and_stay_monotone(self):
        generator = random.Random(11)
        for _ in range(200):
            total = generator.randint(2, 20)
            raw = [round(generator.random(), 4) for _ in range(total)]
            adjusted = causes.benjamini_hochberg(raw)
            ordered = sorted(zip(raw, adjusted))
            for index in range(len(ordered) - 1):
                self.assertLessEqual(ordered[index][1], ordered[index + 1][1] + 1e-9)
            for value, adjusted_value in ordered:
                self.assertGreaterEqual(adjusted_value, value - 1e-9)
                self.assertTrue(0.0 <= adjusted_value <= 1.0)

    def test_missing_p_values_pass_through_as_none(self):
        self.assertEqual(causes.benjamini_hochberg([None, None]), [None, None])
        mixed = causes.benjamini_hochberg([0.01, None])
        self.assertIsNone(mixed[1])
        self.assertLess(mixed[0], causes.ALPHA)


class PermutationTests(unittest.TestCase):

    def test_the_test_is_reproducible_for_a_seeded_generator(self):
        first = causes._permutation([1, 1, 0, 0], [0, 0, 0, 0], 500, random.Random(7))
        second = causes._permutation([1, 1, 0, 0], [0, 0, 0, 0], 500, random.Random(7))
        self.assertEqual(first, second)
        self.assertGreater(first, 0.0)

    def test_a_real_difference_is_detected_and_equality_is_not(self):
        separated = causes._permutation([1] * 10, [0] * 10, 1000, random.Random(1))
        identical = causes._permutation([1, 0] * 10, [1, 0] * 10, 1000, random.Random(1))
        self.assertLess(separated, 0.05)
        self.assertEqual(identical, 1.0, "identical groups cannot produce a small p-value")

    def test_a_single_window_on_either_side_cannot_be_tested(self):
        self.assertIsNone(causes._permutation([1], [0, 0], 200, random.Random(1)))


class ControlDesignTests(unittest.TestCase):

    def test_controls_spread_across_the_whole_span_not_the_oldest_prefix(self):
        bars = [{"open_time": (BASE + timedelta(days=index)).isoformat()}
                for index in range(400)]
        windows, eligible = causes._control_windows(bars, [], 24, 10, KEY, 14400, BASE)
        self.assertEqual(len(windows), 10)
        self.assertEqual(eligible, 399)
        starts = [datetime.fromisoformat(window["end_exclusive"]) for window in windows]
        self.assertGreater(max(starts) - min(starts), timedelta(days=300),
                           "controls must span the series, not cluster at its start")

    def test_controls_never_overlap_a_move_window(self):
        bars = [{"open_time": (BASE + timedelta(days=index)).isoformat()}
                for index in range(60)]
        moves_at = [{"start_time": (BASE + timedelta(days=20)).isoformat(),
                     "end_time": (BASE + timedelta(days=25)).isoformat()}]
        windows, _ = causes._control_windows(bars, moves_at, 24, 30, KEY, 14400, BASE)
        for window in windows:
            start = datetime.fromisoformat(window["start_inclusive"])
            end = datetime.fromisoformat(window["end_exclusive"])
            self.assertFalse(start < datetime.fromisoformat(moves_at[0]["end_time"])
                             and end > datetime.fromisoformat(moves_at[0]["start_time"]),
                             "a control window must not sit inside a move window")

    def test_a_zero_control_request_returns_nothing(self):
        bars = [{"open_time": (BASE + timedelta(days=index)).isoformat()}
                for index in range(20)]
        windows, eligible = causes._control_windows(bars, [], 24, 0, KEY, 14400, BASE)
        self.assertEqual(windows, [])
        self.assertEqual(eligible, 19)

    def test_the_eligible_count_is_reported_so_a_small_sample_is_visible(self):
        """A thin control group must look thin in the report, not just in the data.

        Requesting ten controls out of several hundred eligible ones is a choice,
        and a reader comparing the group sizes needs to be able to see it.
        """
        bars = [{"open_time": (BASE + timedelta(days=index)).isoformat()}
                for index in range(400)]
        _, eligible = causes._control_windows(bars, [], 24, 10, KEY, 14400, BASE)
        self.assertGreater(eligible, 100)
        self.assertLess(10, eligible)


class StoredContextProfileTests(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        registry_init(self.conn)
        self.addCleanup(self.conn.close)
        self.entity_id = registry_entity(self.conn)

    def _bar(self, index, close=100.0):
        from lele.core import registry
        moment = BASE + timedelta(days=index)
        registry.add_price_bar(
            conn=self.conn, instrument_key=KEY, interval_seconds=86400,
            open_time=moment.isoformat(), close_time=(moment + timedelta(days=1)).isoformat(),
            open=str(close), high=str(close), low=str(close), close=str(close),
            volume="10", source="binance", retrieved_at=moment.isoformat())

    def _observation(self, kind, day, instrument=""):
        from lele.core import registry
        moment = BASE + timedelta(days=day)
        registry.add_observation(
            conn=self.conn, source="test", external_id=f"{kind}-{day}", kind=kind,
            description=f"{kind} on day {day}", instrument_key=instrument, amount="1",
            unit="usd", occurred_at=moment.isoformat(), observed_at=moment.isoformat(),
            available_at=(moment + timedelta(days=1)).isoformat(),
            source_url="https://example.com/a")

    def _move(self, day, tier="p10"):
        from lele.core import registry
        return registry.add_move_event(
            conn=self.conn, instrument_key=KEY, interval_seconds=86400, move_hours=24,
            tier=tier, threshold_percent=str(int(tier[1:])), direction="down",
            start_time=(BASE + timedelta(days=day)).isoformat(),
            end_time=(BASE + timedelta(days=day + 1)).isoformat(), start_price="100",
            end_price="80", change_percent="-20", terminal_bar_range_percent="25",
            baseline_mean_percent="0", baseline_std_percent="2", z_score="-10",
            detected_at="2026-06-01T00:00:00+00:00",
            available_at="2026-06-01T00:00:00+00:00")

    def test_a_condition_present_everywhere_carries_no_signal(self):
        for day in range(60):
            self._bar(day)
            self._observation("stablecoin_supply", day)
        for day in (10, 20, 30, 40, 50):
            self._move(day)
        self.conn.commit()
        result = causes.profile_context(self.conn, KEY, 86400, move_hours=24, pre_hours=24,
                                        permutations=400, controls=20)
        self.assertEqual(result["status"], "ok")
        row = next(item for item in result["kinds"] if item["name"] == "stablecoin_supply")
        self.assertTrue(row["tested"])
        self.assertEqual(row["move_share"], 1.0)
        self.assertEqual(row["control_share"], 1.0)
        self.assertEqual(row["permutation_p"], 1.0,
                         "a condition present in every window cannot separate the groups")
        self.assertFalse(row["significant_after_correction"])

    def test_a_short_series_does_not_make_older_windows_look_enriched(self):
        """The confound this guards: a series stored only for recent days.

        Moves spread over the whole period would each have the recent series
        present, while stride-sampled controls from old periods could never have
        it, and the naive comparison reads as a strong finding.
        """
        for day in range(60):
            self._bar(day)
            self._observation("stablecoin_supply", day)
        for day in (5, 50, 55):
            self._observation("market_activity", day, instrument=KEY)
        for day in (10, 20, 30, 40, 50):
            self._move(day)
        self.conn.commit()
        result = causes.profile_context(self.conn, KEY, 86400, move_hours=24, pre_hours=24,
                                        permutations=400, controls=30)
        row = next(item for item in result["kinds"] if item["name"] == "market_activity")
        if row["tested"]:
            self.assertGreater(row["control_windows"], 0,
                               "a control pair must exist inside the kind's own coverage")
            self.assertFalse(row["significant_after_correction"],
                             "coverage must not be able to present itself as a finding")
        else:
            self.assertIn("coverage", row["reason"])

    def test_no_stored_records_reports_insufficient_windows(self):
        for day in range(60):
            self._bar(day)
        self._move(10)
        self.conn.commit()
        result = causes.profile_context(self.conn, KEY, 86400, move_hours=24, pre_hours=24,
                                        permutations=400, controls=10)
        self.assertEqual(result["status"], "insufficient_windows")
        self.assertEqual(result["move_windows"], 0)
        self.assertFalse(result["inference_status"]["usable"])
        self.assertIn("8", result["inference_status"]["reason"])

    def test_inference_refuses_below_the_minimum_window_count(self):
        status = causes._inference_status(3, 20, [0.001], causes.ALPHA)
        self.assertFalse(status["usable"])
        self.assertEqual(status["categories_surviving_correction"], 1,
                         "the count is still reported even when the verdict is withheld")
        allowed = causes._inference_status(30, 30, [0.001], causes.ALPHA)
        self.assertTrue(allowed["usable"])


class TierLabelTests(unittest.TestCase):

    def test_tier_labels_round_trip(self):
        from lele.core import registry
        for percent in (1, 3, 5, 7, 11, 99, 1000):
            self.assertEqual(registry.move_tier_percent(registry.move_tier(percent)), percent)

    def test_non_percent_labels_are_refused(self):
        from lele.core import registry
        for bad in ("medium", "big", "px", "p0", "p1.5", "p", 7, None, ""):
            self.assertIsNone(registry.move_tier_percent(bad), bad)
        for bad in (0, 1001, "5", 3.0, True):
            with self.assertRaises(ValueError):
                registry.move_tier(bad)


class MagnitudeAndTrendTests(unittest.TestCase):
    """The saturation problem and the drift confound, both measured."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        registry_init(self.conn)
        self.addCleanup(self.conn.close)
        self.entity_id = registry_entity(self.conn)

    def _series(self, kind, days, value):
        from lele.core import registry
        for day in range(days):
            moment = BASE + timedelta(days=day)
            registry.add_observation(
                conn=self.conn, source="test", external_id=f"{kind}-{day}", kind=kind,
                description=f"{kind} day {day}", amount=str(value(day)),
                unit="usd", occurred_at=moment.isoformat(), observed_at=moment.isoformat(),
                available_at=(moment + timedelta(days=1)).isoformat(),
                source_url="https://example.com/a")

    def _move(self, day, tier="p3"):
        from lele.core import registry
        return registry.add_move_event(
            conn=self.conn, instrument_key=KEY, interval_seconds=86400, move_hours=24,
            tier=tier, threshold_percent=tier[1:], direction="down",
            start_time=(BASE + timedelta(days=day)).isoformat(),
            end_time=(BASE + timedelta(days=day + 1)).isoformat(), start_price="100",
            end_price="97", change_percent="-3", terminal_bar_range_percent="3",
            baseline_mean_percent="0", baseline_std_percent="1", z_score="-3",
            detected_at="2026-06-01T00:00:00+00:00",
            available_at="2026-06-01T00:00:00+00:00")

    def test_a_drifting_level_is_reported_as_confounded_not_as_a_finding(self):
        """A level that rises with time must not read as a difference between groups.

        The same kind carries a large, highly significant difference in the raw
        test; flagging the drift is what stops that being reported.
        """
        from lele.core import registry
        for day in range(120):
            registry.add_price_bar(
                conn=self.conn, instrument_key=KEY, interval_seconds=86400,
                open_time=(BASE + timedelta(days=day)).isoformat(),
                close_time=(BASE + timedelta(days=day + 1)).isoformat(), open="100",
                high="101", low="99", close="100", volume="10", source="binance",
                retrieved_at=(BASE + timedelta(days=day)).isoformat())
        self._series("stablecoin_supply", 120, lambda day: 1_000_000 + day * 10_000_000)
        for day in range(2, 116, 4):
            self._move(day)
        self.conn.commit()
        result = causes.profile_context(self.conn, KEY, 86400, move_hours=24, pre_hours=24,
                                        permutations=1000, controls=40)
        self.assertEqual(result["status"], "ok")
        row = next(item for item in result["kinds"] if item["name"] == "stablecoin_supply")
        self.assertTrue(row["tested"])
        self.assertEqual(row["test"], "difference_in_measured_mean")
        self.assertTrue(row["confounded_by_time_trend"],
                        "a level drifting by a standard deviation is confounded with time")
        self.assertFalse(row["significant_after_correction"],
                         "a confounded comparison must not be reported as standing out")
        self.assertGreaterEqual(row["trend_detail"]["drift_in_standard_deviations"], 1.0)
        self.assertEqual(result["inference_status"]["categories_surviving_correction"], 0)

    def test_a_flat_level_produces_no_false_trend_flag(self):
        from lele.core import registry
        for day in range(80):
            registry.add_price_bar(
                conn=self.conn, instrument_key=KEY, interval_seconds=86400,
                open_time=(BASE + timedelta(days=day)).isoformat(),
                close_time=(BASE + timedelta(days=day + 1)).isoformat(), open="100",
                high="101", low="99", close="100", volume="10", source="binance",
                retrieved_at=(BASE + timedelta(days=day)).isoformat())
        self._series("stablecoin_supply", 80, lambda day: 50_000_000)
        for day in range(2, 76, 4):
            self._move(day)
        self.conn.commit()
        result = causes.profile_context(self.conn, KEY, 86400, move_hours=24, pre_hours=24,
                                        permutations=400, controls=20)
        row = next(item for item in result["kinds"] if item["name"] == "stablecoin_supply")
        self.assertFalse(row["confounded_by_time_trend"])
        self.assertEqual(row["mean_difference"], 0.0)

    def test_a_measured_difference_is_detected_where_there_is_no_drift(self):
        from lele.core import registry
        for day in range(90):
            registry.add_price_bar(
                conn=self.conn, instrument_key=KEY, interval_seconds=86400,
                open_time=(BASE + timedelta(days=day)).isoformat(),
                close_time=(BASE + timedelta(days=day + 1)).isoformat(), open="100",
                high="101", low="99", close="100", volume="10", source="binance",
                retrieved_at=(BASE + timedelta(days=day)).isoformat())
        # value is flat over time but alternates by parity, so groups differ
        self._series("social_sentiment", 90, lambda day: 70 if day % 2 == 0 else 30)
        for day in range(0, 88, 8):
            self._move(day)
        self.conn.commit()
        result = causes.profile_context(self.conn, KEY, 86400, move_hours=24, pre_hours=24,
                                        permutations=1000, controls=30)
        row = next(item for item in result["kinds"] if item["name"] == "social_sentiment")
        self.assertEqual(row["test"], "difference_in_measured_mean")
        self.assertIsNotNone(row["magnitude_p"])
        self.assertFalse(row["confounded_by_time_trend"])

    def test_a_one_window_group_yields_no_magnitude_test(self):
        values = [1.0]
        self.assertIsNone(causes._permutation_means(values, [2.0, 3.0, 4.0], 200,
                                                   __import__("random").Random(1)))
        self.assertIsNotNone(causes._permutation_means([1.0, 2.0], [3.0, 4.0, 5.0], 200,
                                                      __import__("random").Random(1)))


class TierLadderTests(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        registry_init(self.conn)
        self.addCleanup(self.conn.close)

    def test_a_move_is_recorded_at_every_threshold_it_clears(self):
        from lele.analysis import moves
        closes = [100.0] * 40
        for index in range(40, 70):
            closes.append(closes[-1] * 0.99)
        closes[-1] = closes[-1] * 0.94
        for index, close in enumerate(closes):
            moment = BASE + timedelta(days=index)
            registry_add_bar(self.conn, moment, close)
        result = moves.detect(self.conn, KEY, 86400, move_hours=24, thresholds=(3, 5, 7, 11),
                              baseline_bars=30, limit=100, store=True)
        self.assertTrue(result["moves"])
        for item in result["moves"]:
            magnitude = abs(float(item["change_percent"]))
            expected = [f"p{value}" for value in (3, 5, 7, 11) if magnitude >= value]
            self.assertEqual(item["tiers"], expected)
        rows = registry_tiers(self.conn)
        self.assertTrue(rows)
        for tier, magnitude, _ in rows:
            self.assertGreaterEqual(abs(magnitude), int(tier[1:]))
        counts = result["summary"]["by_tier_direction"]
        self.assertEqual(sum(counts[f"p{value}"]["total"] for value in (3, 5, 7, 11)),
                         result["stored"])

    def test_a_lower_threshold_never_yields_fewer_instances(self):
        from lele.analysis import moves
        closes = [100.0] * 40
        for index in range(40, 90):
            closes.append(closes[-1] * 0.985)
        for index, close in enumerate(closes):
            registry_add_bar(self.conn, BASE + timedelta(days=index), close)
        result = moves.detect(self.conn, KEY, 86400, move_hours=24, thresholds=(3, 5, 7, 11),
                              baseline_bars=30, limit=200, store=False)
        counts = result["summary"]["by_tier_direction"]
        self.assertGreaterEqual(counts["p3"]["total"], counts["p5"]["total"])
        self.assertGreaterEqual(counts["p5"]["total"], counts["p7"]["total"])
        self.assertGreaterEqual(counts["p7"]["total"], counts["p11"]["total"])

    def test_a_ladder_of_one_threshold_behaves_like_a_single_threshold(self):
        from lele.analysis import moves
        closes = [100.0] * 40
        for index in range(40, 60):
            closes.append(closes[-1] * 0.98)
        for index, close in enumerate(closes):
            registry_add_bar(self.conn, BASE + timedelta(days=index), close)
        wide = moves.detect(self.conn, KEY, 86400, move_hours=24, thresholds=(3, 5, 7, 11),
                            baseline_bars=30, limit=100, store=False)
        narrow = moves.detect(self.conn, KEY, 86400, move_hours=24, thresholds=(3,),
                              baseline_bars=30, limit=100, store=False)
        self.assertEqual(wide["coverage"]["selected"], narrow["coverage"]["selected"])
        self.assertEqual(wide["summary"]["by_tier_direction"]["p3"],
                         narrow["summary"]["by_tier_direction"]["p3"])


def test_the_profile_reports_how_many_controls_were_available(self):
        """The defect this measures: 500 move windows against 10 controls.

        The permutation test can only resolve a difference the smaller group can
        express, so a thin control group must be visible as a thin control
        group rather than read as a property of the market.
        """
        for day in range(120):
            self._bar(day)
            self._observation("social_sentiment", day)
        for day in range(5, 115, 2):
            self._move(day)
        self.conn.commit()
        thin = causes.profile_context(self.conn, KEY, 86400, move_hours=24, pre_hours=24,
                                      permutations=200, controls=10)
        self.assertEqual(thin["control_sampling"]["requested"], 10)
        self.assertGreater(thin["control_sampling"]["eligible"], thin["move_windows"],
                           "far more non-move timestamps were available than were used")
        self.assertEqual(thin["control_sampling"]["used"], 10)
        self.assertGreater(thin["control_sampling"]["move_to_control_ratio"], 5.0)
        self.assertIn("requested sample", " ".join(thin["limitations"]))
        wide = causes.profile_context(self.conn, KEY, 86400, move_hours=24, pre_hours=24,
                                      permutations=200, controls=100)
        self.assertGreater(wide["control_sampling"]["used"], thin["control_sampling"]["used"])
        self.assertEqual(wide["control_sampling"]["eligible"],
                         thin["control_sampling"]["eligible"],
                         "eligibility is a property of the stored data, not the request")


class StoredWindowVerificationTests(unittest.TestCase):
    """A stored move row is a claim, and the bars are the only evidence for it.

    `detect` refuses a candidate whose window spans a hole or whose elapsed time
    is not the requested horizon. A row written before that check existed carries
    no such guarantee, so a reader that trusts it attributes a result to a window
    the detector would refuse. These tests build exactly the row a superseded
    detector would have left behind and require it to be refused.
    """

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        registry_init(self.conn)
        self.addCleanup(self.conn.close)
        registry_entity(self.conn)
        for day in range(30):
            registry_add_bar(self.conn, BASE + timedelta(days=day), 100.0)

    def _row(self, start_day, end_day, move_hours=24, tier="p10", identifier=1):
        return {"id": identifier, "tier": tier, "direction": "down",
                "start_time": (BASE + timedelta(days=start_day)).isoformat(),
                "end_time": (BASE + timedelta(days=end_day)).isoformat(),
                "move_hours": move_hours, "change_percent": "-20"}

    def test_a_window_with_no_hole_and_the_right_length_is_kept(self):
        from lele.analysis import moves
        kept, refused = moves.verify_stored(self.conn, KEY, 86400, [self._row(10, 11)])
        self.assertEqual([row["id"] for row in kept], [1])
        self.assertEqual(refused, [])

    def test_a_window_spanning_a_missing_bar_is_refused(self):
        from lele.analysis import moves
        self.conn.execute("DELETE FROM price_bars WHERE open_time=?",
                          ((BASE + timedelta(days=15)).isoformat(),))
        self.conn.commit()
        kept, refused = moves.verify_stored(self.conn, KEY, 86400, [self._row(10, 16)])
        self.assertEqual(kept, [])
        self.assertEqual(len(refused), 1)
        self.assertIn("a bar is missing inside", refused[0]["reason"])
        self.assertEqual(refused[0]["move_event_id"], 1)

    def test_a_window_that_does_not_cover_its_recorded_move_length_is_refused(self):
        from lele.analysis import moves
        kept, refused = moves.verify_stored(self.conn, KEY, 86400,
                                           [self._row(10, 18, move_hours=24)])
        self.assertEqual(kept, [])
        self.assertIn("does not cover the recorded move length", refused[0]["reason"])

    def test_a_window_whose_bars_are_gone_is_refused(self):
        from lele.analysis import moves
        self.conn.execute("DELETE FROM price_bars")
        self.conn.commit()
        kept, refused = moves.verify_stored(self.conn, KEY, 86400, [self._row(10, 11)])
        self.assertEqual(kept, [])
        self.assertIn("no stored bars to verify", refused[0]["reason"])

    def test_a_backwards_window_is_refused(self):
        from lele.analysis import moves
        kept, refused = moves.verify_stored(self.conn, KEY, 86400, [self._row(20, 15)])
        self.assertEqual(kept, [])
        self.assertIn("does not run forwards", refused[0]["reason"])

    def test_the_same_rules_the_detector_applies_are_what_verifies_a_row(self):
        """Every window detect retains must verify, or the readers disagree with it."""
        from lele.analysis import moves
        closes = [100.0] * 40
        for index in range(40, 75):
            closes.append(closes[-1] * 0.985)
        for index, close in enumerate(closes):
            registry_add_bar(self.conn, BASE + timedelta(days=index), close)
        detected = moves.detect(self.conn, KEY, 86400, move_hours=24, thresholds=(1,),
                                baseline_bars=30, limit=200, store=True)
        self.assertTrue(detected["moves"])
        rows = [dict(row) for row in self.conn.execute("SELECT * FROM move_events")]
        kept, refused = moves.verify_stored(self.conn, KEY, 86400, rows)
        self.assertEqual(refused, [])
        self.assertEqual(len(kept), len(rows))

    def test_the_stored_context_profile_leaves_a_stale_window_out_and_says_so(self):
        """The case this whole change exists for.

        A superseded detector stored a row whose window covers four days while
        labelling it a 24-hour move. The profile used to read that row, so its
        pre-window was anchored to the wrong date and the row counted as a move
        window it never was.
        """
        for day in range(30):
            registry_add_bar(self.conn, BASE + timedelta(days=day), 100.0)
            registry_add_observation(self.conn, "stablecoin_supply", day)
        windows = [(6, 7), (12, 13), (18, 19), (24, 25), (9, 13)]
        for start_day, end_day in windows:
            self.conn.execute(
                """INSERT INTO move_events(instrument_key, interval_seconds, move_hours, tier,
                     threshold_percent, direction, start_time, end_time, start_price, end_price,
                     change_percent, terminal_bar_range_percent, baseline_mean_percent,
                     baseline_std_percent, z_score, baseline_bars, detected_at, available_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (KEY, 86400, 24, "p3", "3", "down",
                 (BASE + timedelta(days=start_day)).isoformat(),
                 (BASE + timedelta(days=end_day)).isoformat(), "100", "90", "-10", "5",
                 "0", "2", "-5", 30, "2026-06-01T00:00:00+00:00", "2026-06-01T00:00:00+00:00"))
        self.conn.commit()
        result = causes.profile_context(self.conn, KEY, 86400, move_hours=24, pre_hours=24,
                                        permutations=200, controls=10)
        self.assertEqual(result["unverified_move_windows"], 1,
                         "the mislabelled window must be counted, not used")
        self.assertEqual(result["move_windows"], 4)
        reasons = " ".join(item["reason"] for item in result["uncovered_sample"])
        self.assertIn("detector would refuse", reasons)
        self.assertIn("does not cover the recorded move length", reasons)

    def test_the_window_report_counts_only_verified_moves(self):
        from lele.analysis import timeline
        registry_entity(self.conn)
        for start_day, end_day in ((25, 26), (22, 26)):
            self.conn.execute(
                """INSERT INTO move_events(instrument_key, interval_seconds, move_hours, tier,
                     threshold_percent, direction, start_time, end_time, start_price, end_price,
                     change_percent, terminal_bar_range_percent, baseline_mean_percent,
                     baseline_std_percent, z_score, baseline_bars, detected_at, available_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (KEY, 86400, 24, "p3", "3", "down",
                 (BASE + timedelta(days=start_day)).isoformat(),
                 (BASE + timedelta(days=end_day)).isoformat(),
                 "100", "90", "-10", "5", "0", "2", "-5", 30,
                 "2026-06-01T00:00:00+00:00", "2026-06-01T00:00:00+00:00"))
        self.conn.commit()
        report = timeline.explain(
            self.conn, 1, (BASE + timedelta(days=25)).isoformat(),
            (BASE + timedelta(days=29)).isoformat(), move_hours=24, pre_hours=24,
            thresholds=(3,))
        self.assertEqual(report["moves"]["unverified_rows"], 1)
        self.assertEqual(report["moves"]["total_rows"], 1)


def registry_add_observation(conn, kind, day):
    from lele.core import registry
    moment = BASE + timedelta(days=day)
    registry.add_observation(
        conn=conn, source="test", external_id=f"{kind}-{day}", kind=kind,
        description=f"{kind} on day {day}", amount="1", unit="usd",
        occurred_at=moment.isoformat(), observed_at=moment.isoformat(),
        available_at=(moment + timedelta(days=1)).isoformat(),
        source_url="https://example.com/a")


def registry_add_bar(conn, moment, close):
    from lele.core import registry
    registry.add_price_bar(conn=conn, instrument_key=KEY, interval_seconds=86400,
                           open_time=moment.isoformat(),
                           close_time=(moment + timedelta(days=1)).isoformat(),
                           open=str(close), high=str(close * 1.01), low=str(close * 0.99),
                           close=str(close), volume="10", source="binance",
                           retrieved_at=moment.isoformat())


def registry_tiers(conn):
    return [(row["tier"], float(row["change_percent"]), row["start_time"])
            for row in conn.execute(
                "SELECT tier, change_percent, start_time FROM move_events")]


def registry_init(conn):
    from lele.core import registry
    registry._initialize(conn)


def registry_entity(conn):
    from lele.core import registry
    return registry.upsert_entity(conn, kind="instrument", name="Bitcoin", key=KEY)


if __name__ == "__main__":
    unittest.main()
