"""Offline tests for retention cuts: what a cut would remove, and what it refuses.

The failure modes these pin are the ones this project keeps finding in its own
reports. A cut that says it removed 300 rows when the foreign key removed 301. A
cut that puts its own boundary in the wrong place because the series stores
timestamps in two conventions and the comparison was on text. A cut that leaves a
series too short for anything to detect and reports success. A cut that removes
history and leaves the registry unable to say the history was ever there, so a
later reader meets an empty window and cannot tell an absence of records from an
absence of events.
"""
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import patch

from lele.analysis import moves, retention, timeline
from lele.cli.main import main
from lele.core import db, registry

KEY = "binance:BTCUSDT"
OTHER = "binance:ETHUSDT"
DAY = 86400
BASE = datetime(2026, 1, 1, tzinfo=UTC)
NOW = datetime(2026, 10, 2, tzinfo=UTC)
CUT = BASE + timedelta(days=60)


def iso(moment):
    return moment.isoformat()


def digest(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def seed_bars(conn, key=KEY, count=90, *, start=BASE, interval=DAY, step_days=1.0,
              raw_open=None):
    """A contiguous run of bars, each closing one interval after it opens.

    `raw_open` writes the opening timestamp past the validating writer, which is
    how a timestamp this project did not write reaches the table.
    """
    for index in range(count):
        opened = start + timedelta(days=step_days * index)
        closed = opened + timedelta(seconds=interval)
        render = raw_open or iso
        values = (key, interval, render(opened), render(closed), "100", "110", "99", "105",
                  "1", "binance", iso(NOW))
        if raw_open:
            conn.execute(
                "INSERT INTO price_bars(instrument_key, interval_seconds, open_time, close_time,"
                " open, high, low, close, volume, source, retrieved_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)", values)
            continue
        registry.add_price_bar(
            conn=conn, instrument_key=key, interval_seconds=interval, open_time=values[2],
            close_time=values[3], open=values[4], high=values[5], low=values[6], close=values[7],
            volume=values[8], source=values[9], retrieved_at=values[10],
            source_url="https://api.binance.test/klines")
    return count


def seed_move(conn, key=KEY, *, start=BASE + timedelta(days=10), hours=1):
    return registry.add_move_event(
        conn=conn, instrument_key=key, interval_seconds=DAY, move_hours=hours, tier="p3",
        threshold_percent="3", direction="up", start_time=iso(start),
        end_time=iso(start + timedelta(hours=hours)), start_price="100", end_price="105",
        change_percent="5", terminal_bar_range_percent="11", baseline_mean_percent="1",
        baseline_std_percent="1", z_score="4", baseline_percentile="99", baseline_bars=30,
        detected_at=iso(NOW), available_at=iso(NOW))


def seed_estimate(conn, key=KEY, *, as_of=BASE + timedelta(days=5), window_days=10):
    return registry.add_volatility_estimate(
        conn=conn, instrument_key=key, interval_seconds=DAY, estimator="close_to_close",
        window_bars=10, as_of=iso(as_of), window_start=iso(as_of - timedelta(days=window_days)),
        volatility_percent="1.5", annualized_percent="38", variance="0.0002",
        observed_at=iso(NOW), available_at=iso(NOW))


def seed_scan(conn, key=KEY, *, as_of=BASE + timedelta(days=20)):
    return registry.add_cause_scan(
        conn=conn, instrument_key=key, interval_seconds=DAY, move_hours=24, as_of=iso(as_of),
        change_percent="4", observed_at=iso(NOW), available_at=iso(NOW))


def seed_anomaly(conn, key=KEY, *, occurred=BASE + timedelta(days=5)):
    return conn.execute(
        "INSERT INTO price_anomalies(instrument_key, anomaly_type, observed_at, occurred_at,"
        " severity, score, description) VALUES(?,?,?,?,?,?,?)",
        (key, "return_zscore", iso(occurred), iso(occurred), "low", 1.0, "test")).lastrowid


def seed_article(conn, key=KEY, *, observed=BASE + timedelta(days=20), move_event_id=None,
                 control_start=None):
    """One stored article: attached to a move, or belonging to a control window."""
    if move_event_id is not None:
        return registry.add_move_cause(
            conn=conn, move_event_id=move_event_id, role="move", category="money_movement",
            article_id=f"m{move_event_id}", observed_at=iso(observed), headline="flow",
            source="gdelt", retrieved_at=iso(NOW))
    return registry.add_move_cause(
        conn=conn, role="control", control_key=f"{key}|{DAY}|{iso(control_start)}",
        category="policy_decision", article_id=f"c{iso(observed)}", observed_at=iso(observed),
        headline="policy", source="gdelt", retrieved_at=iso(NOW))


def seed_foreign_bar(conn, key=KEY, *, open_text="2026-01-01T00:00:00+05:00"):
    """A bar whose opening timestamp carries a non-UTC offset.

    Every writer in this project canonicalizes to UTC, so this row can only come
    from a foreign writer -- which is the point: the cut has to notice.
    """
    return conn.execute(
        "INSERT INTO price_bars(instrument_key, interval_seconds, open_time, close_time, open,"
        " high, low, close, volume, source, retrieved_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (key, DAY, open_text, "2026-01-01T00:00:00+00:00", "100", "110", "99", "105", "1",
         "foreign", iso(NOW))).lastrowid


def counts(conn):
    return {table: int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            for table, _, _, _ in retention.PLANNED}


class RegistryCase(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        registry._initialize(self.conn)
        self.addCleanup(self.conn.close)

    def plan(self, cut=CUT, **kwargs):
        kwargs.setdefault("now", NOW)
        kwargs.setdefault("keep_bars", 30)
        return retention.plan(self.conn, iso(cut), **kwargs)


class PlanIsReadOnly(RegistryCase):

    def test_a_plan_counts_without_deleting(self):
        seed_bars(self.conn)
        seed_move(self.conn)
        before = counts(self.conn)
        report = self.plan()
        self.assertEqual(counts(self.conn), before)
        self.assertFalse(report["applied"])
        self.assertGreater(report["delete_total"], 0,
                           "a plan that counts nothing cannot be shown to a reader")
        self.assertIn("nothing has been removed", report["note"])

    def test_a_plan_runs_through_a_read_only_connection(self):
        """The point of separating the plan: it works where a write could not."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = os.path.join(directory.name, "ro.db")
        db.init_db(path)
        with registry.get_conn(path) as conn:
            seed_bars(conn, count=90)
        os.chmod(path, 0o444)
        self.addCleanup(os.chmod, path, 0o644)
        with registry.get_read_conn(path) as conn:
            report = retention.plan(conn, iso(CUT), keep_bars=30, now=NOW)
        entry = report["series"][0]
        self.assertEqual(entry["bars_to_delete"], 59)
        self.assertEqual(entry["bars_remaining"], 31)


class Refusals(RegistryCase):

    def test_a_cut_at_or_after_the_present_is_refused_by_name(self):
        seed_bars(self.conn, count=60)
        report = self.plan(cut=NOW)
        self.assertTrue(report["refused"])
        self.assertIn("cut_not_in_the_past", report["refusals"])
        self.assertEqual(report["refusals"]["cut_not_in_the_past"][0]["now"], iso(NOW))

    def test_a_scope_with_no_stored_bars_is_refused_rather_than_reported_as_zero(self):
        report = self.plan(instrument_key="binance:NOSUCH")
        self.assertTrue(report["refused"])
        self.assertIn("scope_has_no_stored_bars", report["refusals"])
        self.assertEqual(report["series"], [])

    def test_an_empty_registry_is_refused_rather_than_applied_to_nothing(self):
        report = retention.apply(self.conn, iso(CUT), reason="nothing to cut",
                                 keep_bars=22, now=NOW)
        self.assertTrue(report["refused"])
        self.assertIn("scope_has_no_stored_bars", report["refusals"])
        self.assertEqual(report["delete_total"], 0)
        self.assertEqual(retention.history(self.conn)["runs"], 0)

    def test_the_floor_is_the_number_the_detector_needs_not_a_rounder_one(self):
        self.assertEqual(retention.KEEP_BARS_FLOOR, moves.MIN_BASELINE_BARS + 2)
        seed_bars(self.conn, count=retention.KEEP_BARS_FLOOR)
        report = self.plan(cut=NOW - timedelta(seconds=1), keep_bars=1)
        self.assertTrue(report["refused"])
        detail = report["refusals"]["series_below_floor"][0]
        self.assertEqual(detail["bars_remaining"], 0)
        self.assertEqual(detail["floor_bars"], retention.KEEP_BARS_FLOOR)
        self.assertEqual(report["keep_bars"]["effective"], retention.KEEP_BARS_FLOOR,
                         "a request below the floor is raised to it rather than honoured")

    def test_a_series_keeps_its_own_longest_stored_window(self):
        """A series whose stored moves need a longer window gets a longer floor.

        The floor is not one number for the registry: a forty-day move on daily bars
        needs 41 bars, and a cut leaving 30 would leave a series with no detectable
        window at all while still looking like it kept history.
        """
        seed_bars(self.conn, count=120)
        seed_move(self.conn, start=BASE + timedelta(days=1), hours=24 * 40)
        report = self.plan()
        self.assertEqual(report["series"][0]["longest_stored_window_bars"], 41)
        self.assertEqual(report["series"][0]["floor_bars"], 42)
        self.assertFalse(report["refused"])

    def test_a_series_left_below_its_own_floor_is_named(self):
        seed_bars(self.conn, count=120)
        seed_move(self.conn, start=BASE + timedelta(days=1), hours=24 * 40)
        report = self.plan(cut=BASE + timedelta(days=80))
        self.assertTrue(report["refused"])
        detail = report["refusals"]["series_below_floor"][0]
        self.assertEqual(detail["instrument_key"], KEY)
        self.assertEqual(detail["bars_remaining"], 41)
        self.assertIn("41 bars", detail["reason"])

    def test_a_series_stored_in_another_offset_is_refused_rather_than_miscounted(self):
        """Text order is instant order only while the offset and precision agree."""
        seed_bars(self.conn, count=30)
        seed_foreign_bar(self.conn)
        report = self.plan(instrument_key=KEY)
        self.assertTrue(report["refused"])
        self.assertIn("timestamps_not_canonical", report["refusals"])
        self.assertEqual(report["series"], [],
                         "a series whose text order is not its instant order is not counted")

    def test_an_unscoped_cut_needs_one_comparison_text(self):
        """Two series stored in two UTC conventions cannot share one comparison.

        Each series is internally consistent, so neither is refused on its own; the
        unscoped cut is, because one `DELETE` would need one text and the two would
        place the boundary a day apart.
        """
        seed_bars(self.conn, count=30)
        seed_bars(self.conn, key=OTHER, count=30,
                  raw_open=lambda moment: moment.astimezone(
                      timezone(timedelta(hours=5))).strftime("%Y-%m-%dT%H:%M:%S+05:00"))
        report = self.plan()
        self.assertTrue(report["refused"])
        detail = report["refusals"]["timestamps_not_canonical"][-1]
        self.assertIn("one comparison text", detail["reason"])
        self.assertEqual(len(detail["stored_forms"]), 2,
                         "both stored forms are named, not just the first one found")
        self.assertEqual([entry["instrument_key"] for entry in report["series"]],
                         [KEY, OTHER], "each series was measured on its own terms first")

    def test_a_scoped_cut_accepts_a_series_in_a_non_UTC_offset(self):
        """Narrowing the cut is the way through the two-convention refusal."""
        seed_bars(self.conn, count=30)
        seed_bars(self.conn, key=OTHER, count=90,
                  raw_open=lambda moment: moment.astimezone(
                      timezone(timedelta(hours=5))).strftime("%Y-%m-%dT%H:%M:%S+05:00"))
        report = self.plan(instrument_key=OTHER)
        self.assertFalse(report["refused"], report["refusals"])
        self.assertEqual(report["series"][0]["instrument_key"], OTHER)
        self.assertEqual(report["series"][0]["bars_to_delete"], 59)

    def test_a_sub_second_cut_on_a_whole_second_series_is_refused(self):
        seed_bars(self.conn, count=40)
        report = self.plan(cut=CUT.replace(microsecond=500000), keep_bars=22)
        self.assertTrue(report["refused"])
        self.assertIn("cut_precision_not_stored", report["refusals"])

    def test_too_many_series_is_refused_by_name_and_nothing_is_counted(self):
        seed_bars(self.conn, count=30)
        seed_bars(self.conn, key=OTHER, count=30)
        with patch.object(retention, "MAX_SERIES", 1):
            report = self.plan()
        self.assertTrue(report["refused"])
        self.assertIn("too_many_series", report["refusals"])
        self.assertEqual(report["delete_total"], 0,
                         "a refused walk counts nothing rather than counting part of it")

    def test_an_apply_without_a_reason_is_refused_and_the_name_is_one_of_them(self):
        seed_bars(self.conn, count=40)
        before = counts(self.conn)
        report = retention.apply(self.conn, iso(CUT), reason="  ", keep_bars=22, now=NOW)
        self.assertTrue(report["refused"])
        self.assertIn("unstated_reason", report["refusals"])
        self.assertEqual(counts(self.conn), before)

    def test_every_refusal_name_is_reachable_from_some_input(self):
        """A refusal nothing can trigger is one a reader cannot rely on being told."""
        triggered = set()
        seed_bars(self.conn, count=30)
        seed_bars(self.conn, key=OTHER, count=30)
        seed_foreign_bar(self.conn)
        seed_move(self.conn, hours=24 * 40)
        for report in (
                retention.plan(self.conn, iso(NOW), keep_bars=22, now=NOW),
                retention.plan(self.conn, iso(CUT), keep_bars=22, now=NOW),
                retention.plan(self.conn, iso(CUT), keep_bars=22, now=NOW,
                               instrument_key="binance:NOSUCH"),
                retention.plan(self.conn, iso(CUT.replace(microsecond=1)), keep_bars=22,
                               instrument_key=OTHER, now=NOW),
                retention.plan(self.conn, iso(NOW - timedelta(seconds=1)), keep_bars=1,
                               now=NOW),
                retention.apply(self.conn, iso(CUT), reason="", keep_bars=22, now=NOW)):
            triggered.update(report["refusals"])
        with patch.object(retention, "MAX_SERIES", 0):
            triggered.update(
                retention.plan(self.conn, iso(CUT), keep_bars=22, now=NOW)["refusals"])
        self.assertEqual(set(retention.REFUSALS) - triggered, set(),
                         "every named refusal has to be reachable or it is decoration")


class WhatIsCounted(RegistryCase):

    def test_a_row_landing_exactly_on_the_cut_is_kept(self):
        seed_bars(self.conn, count=30, start=CUT)
        report = self.plan(instrument_key=KEY)
        self.assertEqual(report["series"][0]["bars_to_delete"], 0,
                         "the cut is exclusive, so the bar opening on it is history")
        self.assertEqual(report["refusals"], {})

    def test_every_bar_that_survives_lies_wholly_at_or_after_the_cut(self):
        """A bar is dated by its close.

        A two-hour bar opening before the cut and closing after it is kept -- and
        reported, because the retained history now starts inside it. The bar before
        it closed before the cut, so it goes, and what is left is a series whose
        first interval is the one the report named.
        """
        seed_bars(self.conn, count=30, interval=7200, start=CUT - timedelta(hours=3),
                  step_days=2 / 24)
        report = self.plan(instrument_key=KEY)
        self.assertEqual(report["series"][0]["bars_to_delete"], 1)
        self.assertEqual(report["tables"]["price_bars"]["spans_cut"], 1)
        retention.apply(self.conn, iso(CUT), reason="boundary", keep_bars=22,
                        instrument_key=KEY, now=NOW)
        remaining = registry.list_price_bars(self.conn, KEY, 7200, limit=100)
        self.assertEqual(remaining[0]["open_time"], iso(CUT - timedelta(hours=1)))
        self.assertEqual(int(self.conn.execute(
            "SELECT count(*) FROM price_bars WHERE close_time < ?", (iso(CUT),)).fetchone()[0]), 0,
            "no surviving bar closed before the cut")

    def test_a_move_that_straddles_the_cut_is_kept_and_counted(self):
        seed_bars(self.conn, count=90)
        seed_move(self.conn, start=CUT - timedelta(hours=12), hours=24)
        seed_move(self.conn, start=BASE + timedelta(days=2), hours=1)
        report = self.plan()
        self.assertEqual(report["tables"]["move_events"]["delete"], 1)
        self.assertEqual(report["tables"]["move_events"]["spans_cut"], 1)

    def test_an_estimate_whose_window_opens_before_the_cut_is_kept_and_counted(self):
        seed_bars(self.conn, count=90)
        seed_estimate(self.conn, as_of=CUT + timedelta(days=2), window_days=10)
        seed_estimate(self.conn, as_of=BASE + timedelta(days=5), window_days=10)
        report = self.plan()
        self.assertEqual(report["tables"]["volatility_estimates"]["delete"], 1)
        self.assertEqual(report["tables"]["volatility_estimates"]["spans_cut"], 1)

    def test_a_control_article_whose_window_opened_before_the_cut_is_kept_and_counted(self):
        seed_bars(self.conn, count=90)
        seed_article(self.conn, control_start=CUT - timedelta(hours=2),
                     observed=CUT + timedelta(hours=3))
        seed_article(self.conn, control_start=BASE, observed=BASE + timedelta(hours=3))
        report = self.plan()
        self.assertEqual(report["tables"]["move_causes"]["delete"], 1)
        self.assertEqual(report["tables"]["move_causes"]["spans_cut"], 1)

    def test_an_article_a_cascade_removes_is_counted_before_it_goes(self):
        """A move's article is removed by the foreign key, not by this command.

        It is dated after the cut, so a count that looked only at dates would
        report nothing removed and then remove a row: the number and the rows would
        disagree, which is the shape of report this project distrusts.
        """
        seed_bars(self.conn, count=90)
        move_id = seed_move(self.conn, start=BASE + timedelta(days=2))
        seed_article(self.conn, move_event_id=move_id, observed=CUT + timedelta(days=3))
        causes = self.plan()["tables"]["move_causes"]
        self.assertEqual(causes["aged"], 0, "the article postdates the cut")
        self.assertEqual(causes["attached"], 1)
        self.assertEqual(causes["delete"], 1, "the union is what the delete set is")
        applied = retention.apply(self.conn, iso(CUT), reason="cascade", keep_bars=30, now=NOW)
        self.assertEqual(applied["deleted_by_table"]["move_causes"], 1)
        self.assertEqual(counts(self.conn)["move_causes"], 0)

    def test_an_interval_scoped_cut_does_not_prune_a_table_with_no_interval(self):
        """`price_anomalies` names no interval, so an interval-scoped cut skips it.

        Widening the selection would delete another interval's rows on request, and
        the skip is reported rather than counted as zero rows found. A cut scoped to
        the instrument alone does include them, and says that it covers every
        interval while doing it.
        """
        seed_bars(self.conn, count=90)
        seed_anomaly(self.conn)
        report = self.plan(instrument_key=KEY, interval_seconds=DAY)
        self.assertEqual(report["tables"]["price_anomalies"]["delete"], 0)
        self.assertIn("skipped", report["tables"]["price_anomalies"]["interval_note"])
        self.assertIn("--interval", report["tables"]["price_anomalies"]["interval_note"])
        retention.apply(self.conn, iso(CUT), reason="scoped", keep_bars=30, instrument_key=KEY,
                        interval_seconds=DAY, now=NOW)
        self.assertEqual(counts(self.conn)["price_anomalies"], 1)

    def test_an_unscoped_cut_counts_each_table_once_and_not_once_per_series(self):
        seed_bars(self.conn, count=90)
        seed_bars(self.conn, key=OTHER, count=90)
        seed_move(self.conn)
        seed_move(self.conn, key=OTHER)
        report = self.plan()
        self.assertEqual(len(report["series"]), 2)
        self.assertEqual(report["tables"]["move_events"]["delete"], 2,
                         "two moves in total, not two per series")
        self.assertEqual(report["bars_to_delete"], 118)

    def test_a_scoped_cut_leaves_another_series_alone(self):
        seed_bars(self.conn, count=90)
        seed_bars(self.conn, key=OTHER, count=90)
        seed_move(self.conn)
        seed_move(self.conn, key=OTHER)
        self.assertEqual(self.plan(instrument_key=KEY)["tables"]["move_events"]["delete"], 1)
        applied = retention.apply(self.conn, iso(CUT), reason="scoped", keep_bars=30,
                                  instrument_key=KEY, now=NOW)
        self.assertTrue(applied["applied"])
        self.assertEqual(int(self.conn.execute(
            "SELECT count(*) FROM price_bars WHERE instrument_key=?", (OTHER,)).fetchone()[0]), 90)
        self.assertEqual(int(self.conn.execute(
            "SELECT count(*) FROM move_events WHERE instrument_key=?", (OTHER,)).fetchone()[0]), 1)

    def test_a_scoped_apply_removes_the_derived_rows_of_that_series_too(self):
        """A cut scoped to one series still takes that series' derived rows with it.

        The bars are the only table the plan counts per series by its own rule; the
        moves, estimates, scans, anomalies and articles of a scoped cut have to come
        from the same per-series block, or the apply silently keeps everything except
        the bars and reports a cut that did not happen.
        """
        for key in (KEY, OTHER):
            seed_bars(self.conn, key=key, count=90)
            seed_move(self.conn, key=key)
            seed_estimate(self.conn, key=key)
            seed_scan(self.conn, key=key)
            seed_anomaly(self.conn, key=key)
            seed_article(self.conn, key=key, control_start=BASE,
                         observed=BASE + timedelta(hours=3))
        report = self.plan(instrument_key=KEY)
        self.assertEqual(report["tables"]["move_events"]["delete"], 1)
        self.assertIn("every interval", report["series"][0]["tables"]
                      ["price_anomalies"]["interval_note"])
        applied = retention.apply(self.conn, iso(CUT), reason="scoped", keep_bars=30,
                                  instrument_key=KEY, now=NOW)
        self.assertTrue(applied["applied"])
        self.assertEqual(sum(applied["deleted_by_table"].values()), report["delete_total"])
        for table in ("move_events", "volatility_estimates", "cause_scans", "price_anomalies"):
            self.assertEqual(int(self.conn.execute(
                f"SELECT count(*) FROM {table} WHERE instrument_key=?", (KEY,)).fetchone()[0]), 0,
                f"{table} rows for the scoped series were left behind")
            self.assertEqual(int(self.conn.execute(
                f"SELECT count(*) FROM {table} WHERE instrument_key=?", (OTHER,)).fetchone()[0]),
                1, f"{table} rows for the other series were removed")
        self.assertEqual(int(self.conn.execute(
            "SELECT count(*) FROM move_causes").fetchone()[0]), 1)

    def test_a_control_key_containing_a_wildcard_does_not_select_another_series(self):
        odd = "binance:BTC/USDT_1"
        seed_bars(self.conn, key=odd, count=90)
        seed_bars(self.conn, key=OTHER, count=90)
        seed_article(self.conn, key=OTHER, control_start=BASE,
                     observed=BASE + timedelta(hours=3))
        report = self.plan(instrument_key=odd)
        self.assertEqual(report["tables"]["move_causes"]["delete"], 0,
                         "`%` and `_` in an instrument key are escaped, not wildcards")


class Applying(RegistryCase):

    def test_apply_removes_exactly_what_the_plan_counted(self):
        seed_bars(self.conn, count=90)
        move_id = seed_move(self.conn, start=BASE + timedelta(days=2))
        seed_article(self.conn, move_event_id=move_id, observed=BASE + timedelta(days=3))
        seed_article(self.conn, control_start=BASE, observed=BASE + timedelta(hours=3))
        seed_estimate(self.conn)
        seed_scan(self.conn)
        seed_anomaly(self.conn)
        planned = self.plan()
        applied = retention.apply(self.conn, iso(CUT), reason="removal", keep_bars=30, now=NOW)
        self.assertTrue(applied["applied"])
        self.assertEqual(applied["tables"], planned["tables"],
                         "the applied report reports the plan's counts, not a second count")
        for table, row in planned["tables"].items():
            expected = int(self.conn.execute(
                f"SELECT count(*) FROM {table} WHERE 1=1").fetchone()[0])
            self.assertLessEqual(expected, row["delete"],
                                 f"{table} kept rows the plan did not account for")
        self.assertEqual(int(self.conn.execute(
            "SELECT count(*) FROM price_bars WHERE close_time < ?", (iso(CUT),)).fetchone()[0]), 0)
        self.assertEqual(int(self.conn.execute(
            "SELECT count(*) FROM price_bars WHERE close_time = ?", (iso(CUT),)).fetchone()[0]), 1,
            "the bar closing exactly on the cut is kept, and the plan said so")
        self.assertEqual(int(self.conn.execute(
            "SELECT count(*) FROM move_events WHERE end_time < ?", (iso(CUT),)).fetchone()[0]), 0)
        self.assertEqual(int(self.conn.execute(
            "SELECT count(*) FROM volatility_estimates WHERE as_of < ?",
            (iso(CUT),)).fetchone()[0]), 0)
        self.assertEqual(int(self.conn.execute(
            "SELECT count(*) FROM cause_scans WHERE as_of < ?", (iso(CUT),)).fetchone()[0]), 0)
        self.assertEqual(int(self.conn.execute(
            "SELECT count(*) FROM move_causes").fetchone()[0]), 0)

    def test_a_refused_cut_changes_nothing(self):
        seed_bars(self.conn, count=40)
        before = counts(self.conn)
        applied = retention.apply(self.conn, iso(NOW), reason="future", keep_bars=22, now=NOW)
        self.assertFalse(applied["applied"])
        self.assertEqual(counts(self.conn), before)
        self.assertEqual(retention.history(self.conn)["runs"], 0,
                         "a cut that did not happen is not recorded as one")

    def test_the_record_states_what_went_and_not_what_it_contained(self):
        seed_bars(self.conn, count=90)
        applied = retention.apply(self.conn, iso(CUT), reason="storage pressure",
                                  keep_bars=30, now=NOW)
        run = retention.history(self.conn)["recorded"][0]
        self.assertEqual(run["id"], applied["prune_run_id"])
        self.assertEqual(run["cut"], iso(CUT))
        self.assertEqual(run["reason"], "storage pressure")
        self.assertEqual(run["deleted"]["price_bars"], 59)
        self.assertEqual(run["series"][0]["bars_deleted"], 59)
        self.assertEqual(run["series"][0]["last_deleted_bar"], iso(CUT - timedelta(days=2)))
        self.assertEqual(run["spans_cut"]["price_bars"], 1)
        self.assertIn("only a backup holds them", applied["note"])

    def test_two_cuts_are_two_records_and_the_earlier_one_is_still_readable(self):
        seed_bars(self.conn, count=90)
        retention.apply(self.conn, iso(CUT), reason="first", keep_bars=30, now=NOW)
        retention.apply(self.conn, iso(CUT + timedelta(days=5)), reason="second",
                        keep_bars=22, now=NOW)
        self.assertEqual([run["reason"] for run in retention.history(self.conn)["recorded"]],
                         ["second", "first"])

    def test_the_report_survives_the_json_encoder(self):
        seed_bars(self.conn, count=90)
        seed_article(self.conn, control_start=CUT, observed=CUT)
        for report in (self.plan(),
                       retention.apply(self.conn, iso(CUT), reason="serial", keep_bars=30,
                                       now=NOW),
                       retention.history(self.conn)):
            json.dumps(report, allow_nan=False)

    def test_the_report_names_what_it_will_not_prune(self):
        report = self.plan()
        self.assertEqual(set(report["not_pruned"]), set(retention.NOT_PRUNED))
        for table, reason in retention.NOT_PRUNED.items():
            self.assertTrue(reason.strip(), f"{table} is named without a reason")
            self.assertIn(table, report["not_pruned"])


class ReadersAfterACut(RegistryCase):

    def test_the_stored_span_names_the_cut_that_removed_the_history(self):
        seed_bars(self.conn, count=90)
        self.assertIsNone(registry.stored_bar_span(self.conn, KEY, DAY)["history_removed_before"])
        retention.apply(self.conn, iso(CUT), reason="storage", keep_bars=30, now=NOW)
        span = registry.stored_bar_span(self.conn, KEY, DAY)
        self.assertEqual(span["history_removed_before"], iso(CUT))
        self.assertEqual(span["history_removed_reason"], "storage")
        self.assertEqual(span["bars"], 31)

    def test_a_span_with_no_bars_still_reports_a_recorded_cut(self):
        """An empty series is refused by the floor, so a registry cannot reach this
        state by pruning; one that got there another way must still say the history
        was removed rather than never fetched."""
        seed_bars(self.conn, count=90)
        retention.apply(self.conn, iso(CUT), reason="storage", keep_bars=30, now=NOW)
        self.conn.execute("DELETE FROM price_bars")
        span = registry.stored_bar_span(self.conn, KEY, DAY)
        self.assertEqual(span["bars"], 0)
        self.assertEqual(span["history_removed_before"], iso(CUT))
        self.assertEqual(span["history_removed_reason"], "storage")

    def test_a_registry_written_before_retention_existed_reports_no_recorded_cut(self):
        """A read-only open never migrates, so a registry without `prune_runs` is
        read by an older reader that knows nothing was cut there."""
        seed_bars(self.conn, count=40)
        self.conn.execute("DROP TABLE prune_runs")
        self.assertIsNone(registry.stored_bar_span(self.conn, KEY, DAY)
                          ["history_removed_before"])

    def test_a_move_that_survives_the_cut_is_refused_by_the_reader_naming_the_bar(self):
        seed_bars(self.conn, count=90)
        seed_move(self.conn, start=CUT - timedelta(hours=12), hours=24)
        retention.apply(self.conn, iso(CUT), reason="cut", keep_bars=30, now=NOW)
        rows = registry.list_move_events(self.conn, KEY, DAY, 24)
        kept, refused = moves.verify_stored(self.conn, KEY, DAY, rows)
        self.assertEqual(kept, [])
        self.assertEqual(len(refused), 1)
        self.assertIn("not in stored history", refused[0]["reason"])

    def test_explain_says_history_was_removed_rather_than_never_fetched(self):
        entity_id = registry.upsert_entity(self.conn, "instrument", "Bitcoin spot", key=KEY)
        registry.add_instrument(conn=self.conn, entity_id=entity_id, symbol="BTCUSDT",
                                asset_class="bitcoin", venue="binance", quote_currency="USDT")
        seed_bars(self.conn, count=90)
        retention.apply(self.conn, iso(CUT), reason="storage", keep_bars=30, now=NOW)
        report = timeline.explain(self.conn, entity_id, iso(BASE + timedelta(days=10)),
                                  iso(BASE + timedelta(days=12)))
        channel = {row["channel"]: row for row in report["coverage"]["channels"]}
        note = channel["stored_bars"]["note"]
        self.assertIn(iso(CUT), note, "the window reader names the cut that applies")
        self.assertIn("removed", note)
        self.assertIn("prune run", note)
        self.assertIn(iso(CUT), note)
        self.assertNotIn("gap(s)", note, "the series is contiguous and says so separately")


class CommandLine(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = os.path.join(directory.name, "cli.db")
        db.init_db(self.path)
        with registry.get_conn(self.path) as conn:
            seed_bars(conn, count=90)
            seed_move(conn)

    def run_cli(self, *argv):
        return main(["--db", self.path, "--json", "prune", *argv])

    def test_plan_through_the_cli_writes_nothing(self):
        before = digest(self.path)
        self.assertEqual(self.run_cli("plan", "--before", iso(CUT)), 0)
        self.assertEqual(digest(self.path), before)

    def test_apply_through_the_cli_records_the_cut(self):
        self.assertEqual(self.run_cli("apply", "--before", iso(CUT),
                                      "--reason", "storage pressure"), 0)
        with registry.get_read_conn(self.path) as conn:
            self.assertEqual(retention.history(conn)["recorded"][0]["reason"],
                             "storage pressure")

    def test_apply_without_a_reason_is_refused_before_anything_is_opened(self):
        before = digest(self.path)
        self.assertEqual(self.run_cli("apply", "--before", iso(CUT)), 2)
        self.assertEqual(digest(self.path), before)

    def test_a_keep_bars_below_the_floor_is_refused(self):
        self.assertEqual(self.run_cli("plan", "--before", iso(CUT), "--keep-bars", "1"), 2)

    def test_a_cut_without_an_instant_is_refused(self):
        self.assertEqual(self.run_cli("plan"), 2)

    def test_runs_lists_the_recorded_cuts(self):
        self.assertEqual(self.run_cli("apply", "--before", iso(CUT), "--reason", "x"), 0)
        self.assertEqual(self.run_cli("runs"), 0)

    def test_runs_takes_no_cut(self):
        self.assertEqual(self.run_cli("runs", "--before", iso(CUT)), 2)


if __name__ == "__main__":
    unittest.main()
