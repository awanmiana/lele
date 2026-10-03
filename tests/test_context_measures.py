"""Offline tests for stationary context quantities: what is derived, and what is refused.

The failure mode these pin is a number that looks like a measurement and is not.
A difference across a hole, a z-score against a baseline that includes the point
being scored, a cadence taken from a label rather than from the stored timestamps,
or a second reading of the world derived from the first and listed beside it as if
both had been observed.
"""
import io
import json
import sqlite3
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, UTC
from decimal import Decimal

from lele.analysis import causes, events, signals, stationarity, volatility
from lele.core import registry

KEY = "binance:BTCUSDT"
BASE = datetime(2026, 1, 1, tzinfo=UTC)
FIXED = BASE + timedelta(days=900)


def _spread_level(day):
    """A level whose trailing spread is thirty orders below the level itself.

    Built as text rather than as Decimal arithmetic, because the default context
    carries twenty-eight significant digits and would round the eighteenth decimal
    away before anything read it. The baseline points differ only there, so the
    point being scored is a hundred away from a spread of about 2e-18: a score
    larger than the registry can hold, from a spread it can.
    """
    if day < 69:
        return "1000000000000." + "0" * 17 + str(day % 5)
    return "1000000000100"


def _raw_row(conn, tag, day, amount, *, offset="+00:00"):
    """One observation row written past the validating writer.

    `add_observation` refuses a non-decimal amount and normalises every timestamp
    to UTC, so the two refusals that guard against such a row cannot be reached
    through it. They still guard against a row this project did not write.
    """
    moment = BASE + timedelta(days=day)
    local = moment.strftime("%Y-%m-%dT%H:%M:%S") + offset
    conn.execute(
        "INSERT INTO observations(source, external_id, kind, description, amount, unit,"
        " occurred_at, observed_at, available_at, source_url)"
        " VALUES('foreign', ?, 'stablecoin_supply', 'written outside lele', ?, 'usd',"
        " ?, ?, ?, 'https://example.com/a')",
        (tag, amount, local, local, moment.strftime("%Y-%m-%dT%H:%M:%S+00:00")))


def level(conn, kind, day, amount, *, instrument_key="", unit="usd",
          available_days=1, source="test", tag=""):
    moment = BASE + timedelta(days=day)
    registry.add_observation(
        conn=conn, source=source,
        external_id=f"{kind}-{instrument_key or 'world'}-{day}{tag}",
        kind=kind, description=f"{kind} day {day}", amount=str(amount), unit=unit,
        instrument_key=instrument_key, occurred_at=moment.isoformat(),
        observed_at=moment.isoformat(),
        available_at=(moment + timedelta(days=available_days)).isoformat(),
        source_url="https://example.com/a")


class SeriesReadingTests(unittest.TestCase):
    """What `stored_series` accepts, and what it refuses by name."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        registry._initialize(self.conn)
        self.addCleanup(self.conn.close)

    def test_cadence_is_measured_from_the_stored_timestamps_not_assumed(self):
        for day in range(10):
            level(self.conn, "stablecoin_supply", day * 3, 100 + day)
        series = stationarity.stored_series(self.conn, "stablecoin_supply")
        self.assertEqual(series["cadence_seconds"], 3 * 86400)
        self.assertEqual(series["holes"], set())
        self.assertEqual(len(series["points"]), 10)

    def test_a_hole_is_found_where_the_step_exceeds_the_tolerance(self):
        for day in list(range(10)) + [12, 13, 14]:
            level(self.conn, "stablecoin_supply", day, 100 + day)
        series = stationarity.stored_series(self.conn, "stablecoin_supply")
        self.assertEqual(series["cadence_seconds"], 86400)
        self.assertEqual(series["holes"], {10}, "the two-day step at index 10 is the hole")

    def test_one_point_is_not_a_series_and_says_so(self):
        level(self.conn, "stablecoin_supply", 0, 100)
        series = stationarity.stored_series(self.conn, "stablecoin_supply")
        self.assertEqual(series["cadence_seconds"], 0)
        self.assertEqual(series["refused"]["too_short_to_measure_cadence"], 1)
        self.assertIn("interval", series["note"])

    def test_a_row_without_a_measurement_is_counted_not_read_as_zero(self):
        moment = BASE
        registry.add_observation(
            conn=self.conn, source="test", external_id="empty", kind="stablecoin_supply",
            description="no amount", amount=None, unit="",
            observed_at=moment.isoformat(), available_at=moment.isoformat(),
            source_url="https://example.com/a")
        for day in range(3):
            level(self.conn, "stablecoin_supply", day, 100 + day)
        series = stationarity.stored_series(self.conn, "stablecoin_supply")
        self.assertEqual(series["refused"]["no_stored_level"], 1)
        self.assertEqual(len(series["points"]), 3)

    def test_a_repeated_timestamp_is_refused_rather_than_differenced(self):
        level(self.conn, "stablecoin_supply", 0, 100)
        level(self.conn, "stablecoin_supply", 0, 200, tag="-second")
        series = stationarity.stored_series(self.conn, "stablecoin_supply")
        self.assertEqual(series["points"], [])
        self.assertEqual(series["refused"]["repeated_timestamp"], 1)
        self.assertIn("share a timestamp", series["note"])

    def test_a_series_that_does_not_advance_once_normalised_is_refused(self):
        """ISO text sorts, instants do not, and only the second is the series.

        `add_observation` normalises every timestamp to UTC, so this needs a row
        written past it. A foreign writer that keeps its offset would otherwise
        put a later instant first, and a difference across it would run
        backwards without anything looking wrong.
        """
        level(self.conn, "stablecoin_supply", 0, 100)
        level(self.conn, "stablecoin_supply", 5, 300)
        _raw_row(self.conn, "offset", 5, 200, offset="+14:00")
        series = stationarity.stored_series(self.conn, "stablecoin_supply")
        self.assertEqual(series["points"], [])
        self.assertEqual(series["refused"]["not_increasing"], 1)

    def test_an_empty_registry_is_absent_not_zero(self):
        series = stationarity.stored_series(self.conn, "stablecoin_supply")
        self.assertEqual(series["points"], [])
        self.assertEqual(series["first"], None)
        self.assertIsNotNone(series["note"])


class ChangeMeasureTests(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        registry._initialize(self.conn)
        self.addCleanup(self.conn.close)

    def test_a_change_is_the_difference_from_the_previous_stored_value(self):
        for day, amount in ((0, 100), (1, 130), (2, 90)):
            level(self.conn, "stablecoin_supply", day, amount)
        derived = stationarity.derive(self.conn, "stablecoin_supply", measure="change",
                                      now=FIXED)
        self.assertEqual(derived["status"], "ok")
        self.assertEqual([Decimal(row["value"]) for row in derived["rows"]],
                         [Decimal(30), Decimal(-40)])
        self.assertEqual([row["prior_observed_at"] for row in derived["rows"]],
                         [(BASE + timedelta(days=day)).isoformat() for day in (0, 1)])
        self.assertEqual(derived["refused"]["no_prior_point"], 1)

    def test_the_unit_names_the_interval_the_difference_spans(self):
        for day in range(4):
            level(self.conn, "stablecoin_supply", day, 100 + day)
        derived = stationarity.derive(self.conn, "stablecoin_supply", measure="change",
                                      now=FIXED)
        self.assertEqual(derived["unit"], "usd_change_per_86400s")
        self.assertEqual(derived["rows"][0]["unit"], "usd_change_per_86400s")

    def test_a_difference_across_a_hole_is_refused_not_computed(self):
        for day in list(range(4)) + [7]:
            level(self.conn, "stablecoin_supply", day, 100 + day)
        derived = stationarity.derive(self.conn, "stablecoin_supply", measure="change",
                                      now=FIXED)
        self.assertEqual([Decimal(row["value"]) for row in derived["rows"]],
                         [Decimal(1), Decimal(1), Decimal(1)])
        self.assertEqual(derived["refused"]["interrupted"], 1,
                         "day 7 minus day 3 is a four-day change, not a daily one")
        self.assertEqual(derived["holes"], 1)

    def test_a_change_between_two_units_is_refused(self):
        for day in range(2):
            level(self.conn, "stablecoin_supply", day, 100 + day)
        level(self.conn, "stablecoin_supply", 2, 500, unit="usd_thousands")
        derived = stationarity.derive(self.conn, "stablecoin_supply", measure="change",
                                      now=FIXED)
        self.assertEqual(derived["refused"]["unit_changed"], 1)
        self.assertEqual(len(derived["rows"]), 1)

    def test_a_rising_level_becomes_a_flat_change(self):
        for day in range(120):
            level(self.conn, "stablecoin_supply", day, 1_000_000 + day * 10_000_000)
        derived = stationarity.derive(self.conn, "stablecoin_supply", measure="change",
                                      now=FIXED)
        self.assertEqual({Decimal(row["value"]) for row in derived["rows"]},
                         {Decimal(10_000_000)},
                         "a perfectly linear rise is a constant change")

    def test_a_change_is_not_a_purchase_and_does_not_say_it_is(self):
        for day in range(3):
            level(self.conn, "stablecoin_supply", day, 100 + day)
        derived = stationarity.derive(self.conn, "stablecoin_supply", measure="change",
                                      now=FIXED)
        row = derived["rows"][0]
        self.assertIn("not a purchase", json.loads(row["evidence"])["semantics"])
        self.assertEqual(row["source"], "lele-derived:change")
        self.assertTrue(any("not a new reading" in item for item in
                            derived["limitations"]))

    def test_the_same_stored_rows_derive_the_same_bytes(self):
        for day in range(10):
            level(self.conn, "stablecoin_supply", day, 100 + day * 3)
        first = stationarity.derive(self.conn, "stablecoin_supply", measure="change",
                                    now=FIXED)
        second = stationarity.derive(self.conn, "stablecoin_supply", measure="change",
                                     now=FIXED)
        self.assertEqual([row["value"] for row in first["rows"]],
                         [row["value"] for row in second["rows"]])
        self.assertEqual([row["evidence"] for row in first["rows"]],
                         [row["evidence"] for row in second["rows"]])


class ZScoreMeasureTests(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        registry._initialize(self.conn)
        self.addCleanup(self.conn.close)

    def _spread(self, days=100):
        for day in range(days):
            level(self.conn, "stablecoin_supply", day, 100 + (day % 7) * 10)

    def test_a_score_needs_the_minimum_baseline_and_starts_after_it(self):
        self._spread()
        derived = stationarity.derive(self.conn, "stablecoin_supply", measure="zscore",
                                      now=FIXED)
        self.assertEqual(derived["status"], "ok")
        self.assertEqual(derived["derived"], 100 - stationarity.MINIMUM_BASELINE)
        self.assertEqual(derived["refused"]["baseline_short"],
                         stationarity.MINIMUM_BASELINE - 1)
        self.assertEqual(derived["rows"][0]["baseline_observations"],
                         stationarity.MINIMUM_BASELINE)
        self.assertEqual(derived["unit"], "sigma")

    def test_the_baseline_excludes_the_point_being_scored(self):
        self._spread()
        derived = stationarity.derive(self.conn, "stablecoin_supply", measure="zscore",
                                      now=FIXED)
        points = [100 + (day % 7) * 10 for day in range(100)]
        index = stationarity.MINIMUM_BASELINE
        expected = volatility.z_score([Decimal(str(value)) for value in points[:index]],
                                      Decimal(str(points[index])))
        self.assertEqual(Decimal(derived["rows"][0]["value"]),
                         expected.quantize(Decimal("0.00000001")))

    def test_the_stored_spread_and_degrees_of_freedom_are_on_every_row(self):
        self._spread()
        derived = stationarity.derive(self.conn, "stablecoin_supply", measure="zscore",
                                      now=FIXED)
        row = derived["rows"][0]
        self.assertEqual(row["ddof"], 1)
        self.assertNotEqual(row["baseline_deviation"], "")
        evidence = json.loads(row["evidence"])
        self.assertEqual(evidence["baseline_observations"], stationarity.MINIMUM_BASELINE)
        self.assertEqual(evidence["ddof"], 1)
        self.assertEqual(evidence["prior_value"], "130",
                         "the previous point, not the first one in the series")
        self.assertEqual(evidence["parameter_digits"], stationarity.PARAMETER_DIGITS)
        self.assertEqual(evidence["baseline_mean"], row["baseline_mean"])
        self.assertEqual(evidence["baseline_deviation"], row["baseline_deviation"])

    def test_a_flat_series_has_no_score_rather_than_a_divided_by_zero(self):
        for day in range(100):
            level(self.conn, "social_sentiment", day, 50)
        derived = stationarity.derive(self.conn, "social_sentiment", measure="zscore",
                                      now=FIXED)
        self.assertEqual(derived["status"], "nothing_derivable")
        self.assertEqual(derived["refused"]["flat_baseline"],
                         100 - stationarity.MINIMUM_BASELINE)

    def test_a_baseline_shorter_than_the_minimum_is_refused_as_input(self):
        self._spread()
        with self.assertRaises(ValueError):
            stationarity.derive(self.conn, "stablecoin_supply", measure="zscore",
                                baseline=stationarity.MINIMUM_BASELINE - 1, now=FIXED)


class VocabularyTests(unittest.TestCase):
    """The taxonomy cannot rot, and a deliberate omission cannot read as a gap."""

    def test_every_level_series_kind_is_a_kind_the_rest_of_the_project_knows(self):
        for kind in stationarity.LEVEL_SERIES_KINDS:
            with self.subTest(kind=kind):
                self.assertIn(kind, events.KINDS)
                self.assertIn(kind, signals.OBSERVATION_FAMILY)

    def test_every_level_series_kind_states_why_it_is_one(self):
        for kind, reason in stationarity.LEVEL_SERIES_KINDS.items():
            with self.subTest(kind=kind):
                self.assertGreater(len(reason), 40,
                                   "listing a kind as a cadenced level without saying why is "
                                   "the part a reviewer has to take on trust")

    def test_an_event_shaped_kind_is_not_eligible_however_many_rows_it_has(self):
        self.assertNotIn("insider_trade", stationarity.LEVEL_SERIES_KINDS)
        self.assertNotIn("fund_flow", stationarity.LEVEL_SERIES_KINDS)

    def test_the_measures_the_registry_accepts_are_the_measures_derived(self):
        self.assertEqual(set(registry.CONTEXT_MEASURES), set(stationarity.MEASURES))

    def test_a_transform_not_offered_says_why_rather_than_disappearing(self):
        self.assertEqual(set(stationarity.NOT_OFFERED), {"rate", "ratio"})
        for name, reason in stationarity.NOT_OFFERED.items():
            with self.subTest(name=name):
                self.assertGreater(len(reason), 60)
        self.assertEqual(set(stationarity.NOT_OFFERED) & set(stationarity.MEASURES), set())

    def _registry(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        registry._initialize(conn)
        return conn

    def test_every_named_refusal_is_reachable_from_the_derivation(self):
        """A reason no input can trigger is one a reader cannot rely on being told."""
        reachable = set()
        cases = []

        nothing = self._registry()
        cases.append((nothing, "stablecoin_supply", "change"))

        one_point = self._registry()
        level(one_point, "stablecoin_supply", 0, 100)
        cases.append((one_point, "stablecoin_supply", "change"))

        unmeasured = self._registry()
        registry.add_observation(
            conn=unmeasured, source="test", external_id="none", kind="stablecoin_supply",
            description="no amount", amount=None, unit="",
            observed_at=BASE.isoformat(), available_at=BASE.isoformat(),
            source_url="https://example.com/a")
        for day in range(3):
            level(unmeasured, "stablecoin_supply", day, 100 + day)
        cases.append((unmeasured, "stablecoin_supply", "change"))

        repeated = self._registry()
        level(repeated, "stablecoin_supply", 0, 100)
        level(repeated, "stablecoin_supply", 0, 200, tag="-second")
        cases.append((repeated, "stablecoin_supply", "change"))

        backwards = self._registry()
        level(backwards, "stablecoin_supply", 0, 100)
        level(backwards, "stablecoin_supply", 5, 300)
        _raw_row(backwards, "offset", 5, 200, offset="+14:00")
        cases.append((backwards, "stablecoin_supply", "change"))

        messy = self._registry()
        level(messy, "stablecoin_supply", 0, 100)
        level(messy, "stablecoin_supply", 1, 110)
        level(messy, "stablecoin_supply", 4, 130)
        level(messy, "stablecoin_supply", 5, 140, unit="index")
        for day in range(6, 200):
            level(messy, "stablecoin_supply", day, 100)
        cases.append((messy, "stablecoin_supply", "change"))
        cases.append((messy, "stablecoin_supply", "zscore"))

        foreign = self._registry()
        level(foreign, "stablecoin_supply", 0, 100)
        level(foreign, "stablecoin_supply", 1, 110)
        _raw_row(foreign, "text", 2, "not-a-number")
        cases.append((foreign, "stablecoin_supply", "change"))

        tiny = self._registry()
        for day in range(70):
            level(tiny, "stablecoin_supply", day, str(_spread_level(day)))
        cases.append((tiny, "stablecoin_supply", "zscore"))

        for conn, kind, measure in cases:
            derived = stationarity.derive(conn, kind, measure=measure, now=FIXED)
            reachable |= set(derived["refused"])
        self.assertEqual(set(stationarity.REFUSALS) - reachable, set())

    def test_a_value_the_registry_cannot_hold_is_refused_rather_than_rounded(self):
        """A score larger than the registry can store is counted, not truncated.

        The registry's own amount bounds mean this needs a baseline whose spread
        is eighteen orders of magnitude below the level it sits at, so the point
        being scored is a large multiple of a spread small enough to store itself.
        Rounding the score into range would report a number of zero as though it
        had been measured.
        """
        conn = self._registry()
        for day in range(70):
            level(conn, "stablecoin_supply", day, _spread_level(day))
        derived = stationarity.derive(conn, "stablecoin_supply", measure="zscore", now=FIXED)
        self.assertEqual(derived["refused"]["value_not_representable"], 1)
        self.assertEqual(derived["derived"], 9)
        stored = stationarity.store(conn, derived)
        self.assertEqual(stored["written"], 9)
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM context_measures WHERE observed_at = ?",
            ((BASE + timedelta(days=69)).isoformat(),)).fetchone()[0], 0,
            "the one score that cannot be written is not written as zero")


class StoredRowTests(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        registry._initialize(self.conn)
        self.addCleanup(self.conn.close)

    def _row(self, **overrides):
        row = {"source_kind": "stablecoin_supply", "measure": "change", "value": "5",
               "observed_at": BASE.isoformat(),
               "available_at": (BASE + timedelta(days=1)).isoformat(),
               "unit": "usd_change_per_86400s",
               "prior_observed_at": (BASE - timedelta(days=1)).isoformat(),
               "cadence_seconds": 86400}
        row.update(overrides)
        return row

    def test_a_derived_row_is_not_an_observation_row(self):
        registry.add_context_measure(conn=self.conn, **self._row())
        self.assertEqual(self.conn.execute("SELECT count(*) FROM observations").fetchone()[0],
                         0)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM context_measures").fetchone()[0], 1)

    def test_storing_twice_rewrites_the_row_rather_than_duplicating_it(self):
        first = registry.add_context_measure(conn=self.conn, **self._row())
        second = registry.add_context_measure(conn=self.conn, **self._row(value="7"))
        self.assertEqual(first, second)
        row = self.conn.execute("SELECT value FROM context_measures").fetchone()
        self.assertEqual(row["value"], "7")

    def test_two_baselines_of_one_series_do_not_overwrite_each_other(self):
        registry.add_context_measure(conn=self.conn, **self._row())
        registry.add_context_measure(conn=self.conn, **self._row(
            measure="zscore", value="1.25", baseline_observations=60, ddof=1,
            unit="sigma"))
        registry.add_context_measure(conn=self.conn, **self._row(
            measure="zscore", value="0.40", baseline_observations=120, ddof=1,
            unit="sigma"))
        series = registry.context_measure_series(self.conn)
        self.assertEqual(len(series), 3)
        self.assertEqual(sorted(stationarity.baselines_in_use(self.conn)[
            ("stablecoin_supply", "", "zscore")]), [60, 120])

    def test_a_difference_may_not_claim_a_baseline_length(self):
        with self.assertRaises(ValueError):
            registry.add_context_measure(conn=self.conn, **self._row(
                baseline_observations=60))

    def test_a_score_with_one_baseline_point_is_refused(self):
        with self.assertRaises(ValueError):
            registry.add_context_measure(conn=self.conn, **self._row(
                measure="zscore", baseline_observations=1, ddof=1))

    def test_a_measure_outside_the_vocabulary_is_refused(self):
        with self.assertRaises(ValueError):
            registry.add_context_measure(conn=self.conn, **self._row(measure="ratio"))

    def test_a_value_that_is_not_a_finite_decimal_is_refused(self):
        for value in ("abc", "1e9", "NaN", "inf"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                registry.add_context_measure(conn=self.conn, **self._row(value=value))

    def test_availability_may_not_precede_the_observation(self):
        with self.assertRaises(ValueError):
            registry.add_context_measure(conn=self.conn, **self._row(
                available_at=(BASE - timedelta(days=1)).isoformat()))

    def test_a_measure_needs_a_value(self):
        with self.assertRaises(ValueError):
            registry.add_context_measure(conn=self.conn, **self._row(value=""))


class WindowReadingTests(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        registry._initialize(self.conn)
        self.addCleanup(self.conn.close)
        for day in range(5):
            registry.add_context_measure(
                conn=self.conn, source_kind="stablecoin_supply", measure="change",
                value=str(day * 10), observed_at=(BASE + timedelta(days=day)).isoformat(),
                available_at=(BASE + timedelta(days=day, hours=6)).isoformat(),
                cadence_seconds=86400, unit="usd_change_per_86400s")
        self.points = stationarity.stored_points(self.conn, "stablecoin_supply",
                                                 "change", "")

    def test_a_window_holds_the_points_inside_it_and_averages_them(self):
        value, withheld = stationarity.window_value(
            self.points, BASE + timedelta(days=1), BASE + timedelta(days=3))
        self.assertEqual(value, 15.0)
        self.assertEqual(withheld, 0)

    def test_the_window_is_start_inclusive_and_end_exclusive(self):
        value, _ = stationarity.window_value(self.points, BASE, BASE + timedelta(days=2))
        self.assertEqual(value, 5.0)

    def test_an_empty_window_is_absent_rather_than_zero(self):
        value, _ = stationarity.window_value(
            self.points, BASE + timedelta(days=90), BASE + timedelta(days=91))
        self.assertIsNone(value)

    def test_a_point_newer_than_the_window_is_withheld_and_counted(self):
        value, withheld = stationarity.window_value(
            self.points, BASE + timedelta(days=4), BASE + timedelta(days=5))
        self.assertEqual(value, 40.0)
        self.assertEqual(withheld, 0)
        value, withheld = stationarity.window_value(
            self.points, BASE + timedelta(days=3), BASE + timedelta(days=3, hours=1))
        self.assertIsNone(value)
        self.assertEqual(withheld, 1,
                         "the day-3 point falls inside by observation but was recorded "
                         "available six hours after the window closed")
        value, withheld = stationarity.window_value(
            self.points, BASE + timedelta(days=4), BASE + timedelta(days=4, hours=6))
        self.assertEqual(value, 40.0)
        self.assertEqual(withheld, 0, "available exactly at the window end is inside it")

    def test_coverage_is_the_span_over_which_a_full_window_could_hold_one(self):
        span = stationarity.coverage(self.conn, "stablecoin_supply", "change", "",
                                     timedelta(hours=24))
        self.assertIsNotNone(span)
        self.assertEqual(span[0], BASE - timedelta(hours=24))
        self.assertEqual(span[1], BASE + timedelta(days=4, hours=6))

    def test_a_series_shorter_than_one_window_has_no_coverage(self):
        span = stationarity.coverage(self.conn, "stablecoin_supply", "zscore", "",
                                     timedelta(days=30))
        self.assertIsNone(span, "a series that cannot fill a window has no span to compare")


class ReportTests(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        registry._initialize(self.conn)
        self.addCleanup(self.conn.close)

    def test_the_report_states_what_is_derivable_what_is_stored_and_what_was_refused(self):
        for day in range(10):
            level(self.conn, "stablecoin_supply", day, 100 + day)
        report = stationarity.report(self.conn, "stablecoin_supply", now=FIXED)
        self.assertTrue(report["read_only"])
        self.assertEqual([item["measure"] for item in report["series"]],
                         list(stationarity.MEASURES))
        by_measure = {item["measure"]: item for item in report["series"]}
        self.assertEqual(by_measure["change"]["derivable"], 9)
        self.assertEqual(by_measure["change"]["stored"], 0)
        self.assertEqual(by_measure["zscore"]["status"], "nothing_derivable")
        self.assertEqual(by_measure["zscore"]["refused"].get("baseline_short"), 9)
        self.assertEqual(sorted(report["not_offered"]), ["rate", "ratio"])
        self.assertEqual(report["minimum_baseline"], volatility.MIN_BASELINE_Z)

    def test_a_kind_that_is_not_a_level_series_is_refused_rather_than_differenced(self):
        with self.assertRaises(ValueError):
            stationarity.report(self.conn, "insider_trade", now=FIXED)


class ProfileIntegrationTests(unittest.TestCase):
    """The measure comparison is the same test as the level one, beside it."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        registry._initialize(self.conn)
        self.addCleanup(self.conn.close)
        registry.upsert_entity(conn=self.conn, kind="instrument", name="Bitcoin", key=KEY)
        for day in range(140):
            registry.add_price_bar(
                conn=self.conn, instrument_key=KEY, interval_seconds=86400,
                open_time=(BASE + timedelta(days=day)).isoformat(),
                close_time=(BASE + timedelta(days=day + 1)).isoformat(), open="100",
                high="101", low="99", close="100", volume="10", source="binance",
                retrieved_at=(BASE + timedelta(days=day)).isoformat())
            level(self.conn, "stablecoin_supply", day, 1_000_000 + day * 10_000_000)
        for day in range(2, 136, 4):
            registry.add_move_event(
                conn=self.conn, instrument_key=KEY, interval_seconds=86400, move_hours=24,
                tier="p3", threshold_percent="3", direction="down",
                start_time=(BASE + timedelta(days=day)).isoformat(),
                end_time=(BASE + timedelta(days=day + 1)).isoformat(), start_price="100",
                end_price="97", change_percent="-3", terminal_bar_range_percent="3",
                baseline_mean_percent="0", baseline_std_percent="1", z_score="-3",
                detected_at=FIXED.isoformat(), available_at=FIXED.isoformat())
        self.conn.commit()

    def _profile(self, **overrides):
        options = {"move_hours": 24, "pre_hours": 24, "permutations": 1000, "controls": 40}
        options.update(overrides)
        return causes.profile_context(self.conn, KEY, 86400, **options)

    def test_the_level_section_is_unchanged_when_no_measure_is_asked_for(self):
        without = self._profile()
        self.assertNotIn("measures", without)
        with_measure = self._profile(measure=("change",))
        self.assertEqual(without["kinds"], with_measure["kinds"])
        self.assertEqual(without["families"], with_measure["families"])
        self.assertEqual(without["inference_status"], with_measure["inference_status"])
        self.assertNotIn("measures", without["limitations"][0])

    def test_the_drift_that_confounds_the_level_does_not_confound_its_change(self):
        for day in range(140):
            level(self.conn, "stablecoin_supply", day,
                  1_000_000 + day * 10_000_000 + (day % 7) * 1_000_000)
        self.conn.commit()
        stationarity.store(self.conn, stationarity.derive(
            self.conn, "stablecoin_supply", measure="change", now=FIXED))
        self.conn.commit()
        result = self._profile(measure=("change",))
        level_row = next(item for item in result["kinds"]
                         if item["name"] == "stablecoin_supply")
        measure_row = next(item for item in result["measures"]["series"]
                           if item["name"] == "stablecoin_supply.change")
        self.assertTrue(level_row["confounded_by_time_trend"])
        self.assertGreaterEqual(level_row["trend_detail"]["drift_in_standard_deviations"], 1.0)
        self.assertFalse(measure_row["confounded_by_time_trend"],
                         "a daily change of a rising series does not drift with the calendar")
        self.assertLess(measure_row["trend_detail"]["drift_in_standard_deviations"], 1.0)
        self.assertNotEqual(measure_row["mean_difference"], 0.0)

    def test_the_measure_section_reports_its_own_coverage_and_inference(self):
        stationarity.store(self.conn, stationarity.derive(
            self.conn, "stablecoin_supply", measure="change", now=FIXED))
        self.conn.commit()
        result = self._profile(measure=("change",))
        section = result["measures"]
        self.assertEqual(section["method"], "stored_context_measure_profile_v1")
        self.assertEqual(section["measures_requested"], ["change"])
        self.assertEqual(section["series_tested"], 1)
        self.assertEqual(section["inference_status"]["categories_surviving_correction"], 0)
        self.assertIn("stablecoin_supply.change", section["coverage_days"])
        self.assertEqual(sorted(section["measures_off"]), ["rate", "ratio"])
        self.assertIn("selecting on the outcome", section["multiple_testing"]["note"])

    def test_a_measure_that_was_never_derived_is_reported_not_silently_absent(self):
        result = self._profile(measure=("change",))
        self.assertEqual(result["measures"]["series"], [])
        self.assertEqual([note["status"] for note in result["measures"]["not_compared"]],
                         ["not_compared"])
        self.assertIn("lele context derive",
                      result["measures"]["not_compared"][0]["reason"])

    def test_a_stationary_measure_can_still_fail_the_correction(self):
        """Stationary is not a licence to publish a finding."""
        self.conn.execute("DELETE FROM observations WHERE external_id LIKE 'stablecoin-%'")
        for day in range(140):
            level(self.conn, "social_sentiment", day, 50 + (day % 3), unit="index_0_100",
                  available_days=0)
        self.conn.commit()
        stationarity.store(self.conn, stationarity.derive(
            self.conn, "social_sentiment", measure="change", now=FIXED))
        self.conn.commit()
        result = self._profile(measure=("change",))
        row = next(item for item in result["measures"]["series"]
                   if item["name"] == "social_sentiment.change")
        self.assertEqual(row["test"], "difference_in_measured_mean")
        self.assertIsNotNone(row["magnitude_p"])
        self.assertGreater(row["magnitude_p"], causes.ALPHA,
                           "a change series unrelated to move timing carries no signal")
        self.assertFalse(row["significant_after_correction"])
        self.assertEqual(result["measures"]["inference_status"][
            "categories_surviving_correction"], 0)

    def test_an_unknown_measure_name_is_refused_rather_than_ignored(self):
        with self.assertRaises(ValueError):
            self._profile(measure=("ratio",))
        with self.assertRaises(ValueError):
            self._profile(measure="change")

    def test_a_series_with_two_baselines_is_reported_rather_than_resolved(self):
        for day in range(120):
            level(self.conn, "social_sentiment", day, 50 + (day % 5) * 3, unit="index_0_100")
        self.conn.commit()
        for baseline in (60, 90):
            stationarity.store(self.conn, stationarity.derive(
                self.conn, "social_sentiment", measure="zscore", baseline=baseline,
                now=FIXED))
        self.conn.commit()
        result = self._profile(measure=("zscore",))
        notes = result["measures"]["not_compared"]
        self.assertEqual([note["baselines"] for note in notes], [[60, 90]])
        self.assertIn("--baseline", notes[0]["reason"])
        self.assertEqual(result["measures"]["series"], [])


class CliTests(unittest.TestCase):
    """The flag reaches the command that can use it and nowhere else."""

    def _run(self, argv):
        from lele.cli.main import main
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = main(argv)
        return status, err.getvalue()

    def test_a_measure_on_an_action_that_cannot_use_it_is_refused_not_ignored(self):
        status, message = self._run(["causes", "attribute", "1", "--measure", "change"])
        self.assertEqual(status, 2)
        self.assertIn("accepted and ignored", message)
        status, message = self._run(["causes", "profile", "1", "--measure", "zscore"])
        self.assertEqual(status, 2)
        self.assertIn("causes context", message)

    def test_a_measure_the_vocabulary_does_not_hold_is_refused_by_the_parser(self):
        status, message = self._run(["causes", "context", "1", "--measure", "ratio"])
        self.assertEqual(status, 2)
        self.assertIn("invalid choice", message)


if __name__ == "__main__":
    unittest.main()
