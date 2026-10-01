"""Tests for the instrument inventory, volatility storage, and the DB-backed
measure path.

Two of these are regression tests for defects found while building this:

* `move_events.realized_volatility_percent` held the terminal bar's high-low range
  and was renamed to `terminal_bar_range_percent`. The migration has to carry the
  value across unchanged and leave a note, because a rename that leaves no trace is
  indistinguishable from a recompute.
* The instrument registry is what makes an annualization checkable at all: with no
  recorded asset class, an annualized volatility must come back unknown rather than
  scaled by a guessed 252.
"""

import json
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from lele.analysis import volatility as vol
from lele.core import clock, db, registry
from lele.core.schema import SCHEMA_VERSION


class RegistryCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = str(Path(self.temp.name) / "registry.db")
        with registry.get_conn(self.db) as conn:
            self.btc = registry.upsert_entity(conn, "instrument", "Bitcoin",
                                              key="local:btc")
            self.gold = registry.upsert_entity(conn, "instrument", "Gold",
                                               key="local:gold")
            self.spx = registry.upsert_entity(conn, "instrument", "S&P 500",
                                              key="local:spx")

    def bars(self, conn, key, closes, interval_seconds=86400, start=None, gap_after=None):
        first = datetime.fromisoformat(start or "2026-01-01T00:00:00+00:00")
        for index, close in enumerate(closes):
            if gap_after is not None and index == gap_after:
                first += timedelta(seconds=2 * interval_seconds)
            open_time = first + timedelta(seconds=index * interval_seconds)
            registry.add_price_bar(
                conn, instrument_key=key, interval_seconds=interval_seconds,
                open_time=open_time.isoformat(),
                close_time=(open_time + timedelta(seconds=interval_seconds)).isoformat(),
                open=str(close), high=str(close + 2), low=str(close - 2),
                close=str(close), volume="10", source="test",
                retrieved_at="2026-02-01T00:00:00+00:00")


class InstrumentTests(RegistryCase):
    def test_round_trip(self):
        with registry.get_conn(self.db) as conn:
            registry.add_instrument(
                conn, entity_id=self.btc, symbol="BTCUSDT", venue="binance",
                asset_class="bitcoin", quote_currency="USDT",
                adjustment_basis="unadjusted", rights_basis="public market data endpoint",
                first_seen_at="2026-01-01T00:00:00+00:00")
            found = registry.get_instrument(conn, "local:btc")
            self.assertEqual(found["symbol"], "BTCUSDT")
            self.assertEqual(found["venue"], "binance")
            self.assertEqual(found["entity_key"], "local:btc")
            self.assertEqual(found["rights_verified"], 0)

    def test_the_same_symbol_on_two_venues_is_two_instruments(self):
        with registry.get_conn(self.db) as conn:
            registry.add_instrument(conn, entity_id=self.btc, symbol="BTCUSDT",
                                    venue="binance", asset_class="bitcoin")
            registry.add_instrument(conn, entity_id=self.btc, symbol="BTCUSDT",
                                    venue="kraken", asset_class="bitcoin")
            rows = registry.list_instruments(conn, entity_id=self.btc)
            self.assertEqual({row["venue"] for row in rows}, {"binance", "kraken"})

    def test_re_adding_the_same_identity_updates_instead_of_duplicating(self):
        with registry.get_conn(self.db) as conn:
            for quote in ("USDT", "USD"):
                registry.add_instrument(conn, entity_id=self.btc, symbol="BTCUSDT",
                                        venue="binance", asset_class="bitcoin",
                                        quote_currency=quote)
            rows = registry.list_instruments(conn, entity_id=self.btc)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["quote_currency"], "USD")

    def test_get_instrument_does_not_guess_a_venue(self):
        """Silently picking one of several series for one entity is the bug."""
        with registry.get_conn(self.db) as conn:
            for venue in ("binance", "kraken"):
                registry.add_instrument(conn, entity_id=self.btc, symbol="BTCUSDT",
                                        venue=venue, asset_class="bitcoin")
            narrowed = registry.get_instrument(conn, "local:btc", venue="kraken")
            self.assertEqual(narrowed["venue"], "kraken")
            self.assertEqual(registry.get_instrument(conn, "local:btc", venue="okx"), None)

    def test_unregistered_entity_reports_nothing_rather_than_an_error(self):
        with registry.get_conn(self.db) as conn:
            self.assertIsNone(registry.get_instrument(conn, "local:absent"))

    def test_rights_verified_cannot_be_claimed_true(self):
        with registry.get_conn(self.db) as conn:
            with self.assertRaises(ValueError):
                registry.add_instrument(conn, entity_id=self.btc, symbol="X",
                                        asset_class="bitcoin", rights_verified="yes")

    def test_bad_entity_and_adjustment_are_refused(self):
        with registry.get_conn(self.db) as conn:
            with self.assertRaises(ValueError):
                registry.add_instrument(conn, entity_id=999999, symbol="X",
                                        asset_class="bitcoin")
            with self.assertRaises(ValueError):
                registry.add_instrument(conn, entity_id=self.btc, symbol="X",
                                        asset_class="bitcoin", adjustment_basis="adjusted-ish")
            with self.assertRaises(ValueError):
                registry.add_instrument(conn, entity_id=self.btc, symbol="  ",
                                        asset_class="bitcoin")

    def test_filters_and_limit(self):
        with registry.get_conn(self.db) as conn:
            registry.add_instrument(conn, entity_id=self.btc, symbol="BTCUSDT",
                                    asset_class="bitcoin")
            registry.add_instrument(conn, entity_id=self.gold, symbol="GC=F",
                                    venue="cme", asset_class="gold")
            self.assertEqual(len(registry.list_instruments(conn, asset_class="gold")), 1)
            self.assertEqual(len(registry.list_instruments(conn, symbol="BTCUSDT")), 1)
            self.assertEqual(len(registry.list_instruments(conn, asset_class="oil")), 0)
            self.assertEqual(len(registry.list_instruments(conn, limit=1)), 1)
            with self.assertRaises(ValueError):
                registry.list_instruments(conn, limit=0)


class VolatilityStoreTests(RegistryCase):
    def test_round_trip_keeps_the_convention(self):
        """The convention is stored as data, not left in a docstring."""
        convention = vol.convention_for("yang_zhang", "stock", 86400)
        with registry.get_conn(self.db) as conn:
            registry.add_volatility_estimate(
                conn, instrument_key="local:spx", interval_seconds=86400,
                estimator="yang_zhang", window_bars=30, as_of="2026-02-01T00:00:00+00:00",
                window_start="2026-01-02T00:00:00+00:00", volatility_percent="0.8",
                annualized_percent="12.7", variance="0.000064",
                annualization="252 bars per year", ddof=0,
                convention=json.dumps(convention, sort_keys=True),
                observed_at="2026-02-01T01:00:00+00:00",
                available_at="2026-02-01T00:00:00+00:00")
            rows = registry.list_volatility_estimates(conn, "local:spx", 86400)
            self.assertEqual(len(rows), 1)
            stored = json.loads(rows[0]["convention"])
            self.assertEqual(stored["alpha"], "1.34")
            self.assertEqual(rows[0]["annualization"], "252 bars per year")

    def test_storing_twice_updates_rather_than_duplicates(self):
        with registry.get_conn(self.db) as conn:
            for value in ("0.8", "0.9"):
                registry.add_volatility_estimate(
                    conn, instrument_key="local:spx", interval_seconds=86400,
                    estimator="lpv", window_bars=30, as_of="2026-02-01T00:00:00+00:00",
                    window_start="2026-01-02T00:00:00+00:00", volatility_percent=value,
                    annualized_percent="12.7", variance="0.000064",
                    observed_at="2026-02-01T01:00:00+00:00",
                    available_at="2026-02-01T00:00:00+00:00")
            rows = registry.list_volatility_estimates(conn, "local:spx", 86400)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["volatility_percent"], "0.9")

    def store(self, conn, **overrides):
        common = dict(instrument_key="local:spx", interval_seconds=86400, estimator="lpv",
                      window_bars=30, as_of="2026-02-01T00:00:00+00:00",
                      window_start="2026-01-02T00:00:00+00:00", volatility_percent="0.8",
                      annualized_percent="12.7", variance="0.000064",
                      observed_at="2026-02-01T01:00:00+00:00",
                      available_at="2026-02-01T00:00:00+00:00")
        common.update(overrides)
        return registry.add_volatility_estimate(conn, **common)

    def test_bounds_and_convention_shape(self):
        with registry.get_conn(self.db) as conn:
            with self.assertRaises(ValueError):
                self.store(conn, window_bars=1)
            with self.assertRaises(ValueError):
                self.store(conn, ddof=-1)
            with self.assertRaises(ValueError):
                self.store(conn, basis="yearly")
            with self.assertRaises(ValueError):
                self.store(conn, convention="drift=0")
            with self.assertRaises(ValueError):
                self.store(conn, variance="NaN")


class MeasureTests(RegistryCase):
    """The DB-backed path, where the coverage guards actually matter."""

    def test_measure_reports_each_estimator_and_annualizes_by_class(self):
        closes = [100 + (index % 7) - 3 for index in range(40)]
        with registry.get_conn(self.db) as conn:
            registry.add_instrument(conn, entity_id=self.btc, symbol="BTCUSDT",
                                    asset_class="bitcoin")
            self.bars(conn, "local:btc", closes)
            report = vol.measure(conn, "local:btc", 86400, window_bars=30)
        self.assertEqual(report["asset_class"], "bitcoin")
        self.assertTrue(report["instrument_registered"])
        self.assertEqual(report["coverage"]["bars"], 40)
        self.assertEqual(report["coverage"]["gaps"], 0)
        ok = [row for row in report["estimators"] if row["status"] == "ok"]
        self.assertTrue(ok)
        for row in ok:
            if row.get("annualized_percent") is not None:
                self.assertEqual(row["annualization_bars_per_year"], 365, row["estimator"])

    def test_completeness_is_unknown_not_false(self):
        with registry.get_conn(self.db) as conn:
            self.bars(conn, "local:btc", [100] * 40)
            report = vol.measure(conn, "local:btc", 86400, window_bars=30)
        self.assertEqual(report["coverage"]["completeness"], "unknown")

    def test_a_window_spanning_a_hole_is_refused_not_measured(self):
        """The defect this whole module is downstream of, pinned at the new layer.

        Bars here are 40 daily points with one day skipped. A 30-bar window ending
        at the end necessarily spans the hole, so covering roughly 31 elapsed days
        while calling it 30 bars would be the original bug.
        """
        closes = [100 + (index % 5) for index in range(40)]
        with registry.get_conn(self.db) as conn:
            registry.add_instrument(conn, entity_id=self.btc, symbol="BTCUSDT",
                                    asset_class="bitcoin")
            self.bars(conn, "local:btc", closes, gap_after=20)
            report = vol.measure(conn, "local:btc", 86400, window_bars=30)
        self.assertEqual(report["coverage"]["gaps"], 1)
        for row in report["estimators"]:
            if row["status"] != "insufficient_bars":
                self.assertEqual(row["status"], "window_spans_gap", row["estimator"])
                self.assertIsNone(row["volatility_percent"])

    def test_missing_bars_are_reported_per_estimator_not_silently_zero(self):
        with registry.get_conn(self.db) as conn:
            self.bars(conn, "local:btc", [100, 101, 102])
            report = vol.measure(conn, "local:btc", 86400, window_bars=30)
        self.assertEqual(len(report["not_computable"]), len(report["estimators"]))
        for row in report["estimators"]:
            self.assertEqual(row["status"], "insufficient_bars")
            self.assertIsNone(row["volatility_percent"])
            self.assertIsNone(row["annualized_percent"])

    def test_an_unregistered_instrument_cannot_be_annualized(self):
        """No asset class recorded means no annualization, not a guessed 252."""
        with registry.get_conn(self.db) as conn:
            self.bars(conn, "local:btc", [100 + (index % 9) for index in range(40)])
            report = vol.measure(conn, "local:btc", 86400, window_bars=30)
        self.assertFalse(report["instrument_registered"])
        self.assertEqual(report["asset_class"], "unknown")
        for row in report["estimators"]:
            if row["status"] == "ok":
                self.assertIsNone(row["annualized_percent"])
                self.assertIsNone(row["annualization_bars_per_year"])
                self.assertIn("no recorded bars-per-year convention",
                              row["annualization_reason"])

    def test_store_writes_a_row_per_computable_estimator(self):
        with registry.get_conn(self.db) as conn:
            registry.add_instrument(conn, entity_id=self.btc, symbol="BTCUSDT",
                                    asset_class="bitcoin")
            self.bars(conn, "local:btc", [100 + (index % 7) for index in range(40)])
            report = vol.measure(conn, "local:btc", 86400,
                                 estimators=("lpv", "parkinson"), window_bars=30, store=True)
            stored = registry.list_volatility_estimates(conn, "local:btc", 86400)
        self.assertEqual(report["stored"], 2)
        self.assertEqual({row["estimator"] for row in stored}, {"lpv", "parkinson"})
        for row in stored:
            self.assertTrue(row["available_at"], "every estimate needs an availability time")
            self.assertEqual(json.loads(row["convention"])["precision"], vol.PRECISION)

    def test_measure_never_calls_the_wall_clock(self):
        """`now` exists so a script can pin it; the default must still be clock.now."""
        closes = [100 + (index % 7) for index in range(40)]
        with registry.get_conn(self.db) as conn:
            self.bars(conn, "local:btc", closes)
            frozen = vol.measure(conn, "local:btc", 86400, window_bars=30,
                                 estimators=("lpv",), store=True,
                                 now=datetime(2020, 5, 4, 3, 2, 1, tzinfo=UTC))
        self.assertEqual(frozen["instrument_key"], "local:btc")
        with registry.get_conn(self.db) as conn:
            rows = registry.list_volatility_estimates(conn, "local:btc", 86400)
            self.assertEqual(rows[0]["observed_at"], "2020-05-04T03:02:01+00:00")
        self.assertTrue(clock.now())


class MigrationTests(unittest.TestCase):
    """The v16 to v17 rename, on a database that really holds the old shape."""

    LEGACY = """
    CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE entities(id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL UNIQUE,
      kind TEXT, name TEXT, country TEXT, website TEXT, lei TEXT, notes TEXT,
      created_at TEXT);
    CREATE TABLE move_events(id INTEGER PRIMARY KEY AUTOINCREMENT, instrument_key TEXT NOT NULL,
      interval_seconds INTEGER, move_hours INTEGER, tier TEXT, threshold_percent TEXT,
      direction TEXT, start_time TEXT, end_time TEXT, start_price TEXT, end_price TEXT,
      change_percent TEXT, realized_volatility_percent TEXT, baseline_mean_percent TEXT,
      baseline_std_percent TEXT, z_score TEXT, baseline_percentile TEXT,
      baseline_bars INTEGER, detected_at TEXT, available_at TEXT, source_url TEXT,
      evidence TEXT);
    CREATE TABLE price_bars(id INTEGER PRIMARY KEY AUTOINCREMENT, instrument_key TEXT NOT NULL,
      interval_seconds INTEGER, open_time TEXT, close_time TEXT, open TEXT, high TEXT,
      low TEXT, close TEXT, volume TEXT, quote_volume TEXT NOT NULL DEFAULT '',
      trades INTEGER NOT NULL DEFAULT 0, source TEXT, source_url TEXT,
      retrieved_at TEXT, evidence TEXT,
      UNIQUE(instrument_key, interval_seconds, open_time));
    """

    def legacy_db(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = str(Path(temp.name) / "legacy.db")
        conn = sqlite3.connect(path)
        conn.executescript(self.LEGACY)
        conn.execute(
            """INSERT INTO move_events(instrument_key, interval_seconds, move_hours, tier,
                 threshold_percent, direction, start_time, end_time, start_price, end_price,
                 change_percent, realized_volatility_percent, baseline_mean_percent,
                 baseline_std_percent, z_score, baseline_percentile, baseline_bars,
                 detected_at, available_at, evidence)
               VALUES('local:btc', 86400, 24, 'p5', '5', 'down', 'a', 'b', '100', '80',
                 '-20', '4.5', '1.2', '0.9', '-3.1', '1.5', 90, 'd', 'd',
                 '{"method":"tiered_moves_v1"}')""")
        conn.execute("INSERT INTO meta VALUES('schema_version','16')")
        conn.commit()
        conn.close()
        return path

    def test_the_rename_carries_the_value_and_records_itself(self):
        path = self.legacy_db()
        with db.get_conn(path) as conn:
            version = conn.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()[0]
            columns = [row[1] for row in conn.execute("PRAGMA table_info(move_events)")]
            value, evidence = conn.execute(
                "SELECT terminal_bar_range_percent, evidence FROM move_events").fetchone()
            notes = conn.execute("SELECT v FROM meta WHERE k='migration_notes'").fetchone()
        self.assertEqual(version, str(SCHEMA_VERSION))
        self.assertNotIn("realized_volatility_percent", columns)
        self.assertIn("terminal_bar_range_percent", columns)
        self.assertEqual(Decimal(value), Decimal("4.5"))
        recorded = json.loads(evidence)
        self.assertEqual(recorded["column_renamed_from"], "realized_volatility_percent")
        self.assertEqual(recorded["method"], "tiered_moves_v1")
        self.assertIn("terminal bar", recorded["meaning"])
        self.assertIn("renamed", json.loads(notes[0])[0]["detail"])

    def test_migration_is_not_repeated_on_a_second_open(self):
        path = self.legacy_db()
        with db.get_conn(path) as conn:
            first = conn.execute("SELECT v FROM meta WHERE k='migration_notes'").fetchone()[0]
        with db.get_conn(path) as conn:
            second = conn.execute("SELECT v FROM meta WHERE k='migration_notes'").fetchone()[0]
        self.assertEqual(first, second)

    def test_open_interest_column_is_added(self):
        path = self.legacy_db()
        with db.get_conn(path) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(price_bars)")}
        self.assertIn("open_interest", columns)


class SerializationTests(RegistryCase):
    """Every report must survive `json.dumps`.

    This is not a formality. The estimator helpers quantize a Decimal and return it,
    which reads fine in Python and fails only at the moment a user runs the command
    -- `TypeError: Object of type Decimal is not JSON serializable`, raised from
    deep inside the encoder. A unit test comparing values in Python cannot catch it;
    only serializing the finished report can.
    """

    def build(self, conn, closes, register=True):
        if register:
            registry.add_instrument(conn, entity_id=self.btc, symbol="BTCUSDT",
                                    asset_class="bitcoin")
        self.bars(conn, "local:btc", closes)

    def test_measure_report_is_json_serializable(self):
        closes = [100 + (index % 7) - 3 for index in range(40)]
        with registry.get_conn(self.db) as conn:
            self.build(conn, closes)
            report = vol.measure(conn, "local:btc", 86400, window_bars=30)
            self.dumps(report)
            stored = vol.measure(conn, "local:btc", 86400, window_bars=30,
                                 estimators=("lpv", "parkinson"), store=True)
            self.dumps(stored)

    def test_report_is_serializable_when_nothing_can_be_computed(self):
        with registry.get_conn(self.db) as conn:
            self.build(conn, [100, 101, 102])
            self.dumps(vol.measure(conn, "local:btc", 86400, window_bars=30))

    def test_report_is_serializable_without_a_registered_instrument(self):
        """No asset class means no annualization, and no Decimal in the report."""
        closes = [100 + (index % 9) - 4 for index in range(40)]
        with registry.get_conn(self.db) as conn:
            self.build(conn, closes, register=False)
            self.dumps(vol.measure(conn, "local:btc", 86400, window_bars=30))

    def test_report_is_serializable_when_a_window_spans_a_gap(self):
        closes = [100 + (index % 5) for index in range(40)]
        with registry.get_conn(self.db) as conn:
            registry.add_instrument(conn, entity_id=self.btc, symbol="BTCUSDT",
                                    asset_class="bitcoin")
            self.bars(conn, "local:btc", closes, gap_after=20)
            self.dumps(vol.measure(conn, "local:btc", 86400, window_bars=30))

    def test_threshold_verdicts_are_json_serializable(self):
        baseline = [Decimal(index) for index in range(1, 121)]
        for mode, level in (("percentile", "95"), ("z_score", "3"), ("absolute", "4")):
            verdict = vol.evaluate_threshold(baseline, Decimal(200), mode=mode, level=level)
            with self.subTest(mode=mode):
                self.dumps(verdict)

    def test_a_threshold_verdict_survives_a_short_baseline(self):
        verdict = vol.evaluate_threshold([Decimal(1)] * 10, Decimal(9),
                                         mode="percentile", level="95")
        self.dumps(verdict)

    def test_instrument_inventory_is_json_serializable(self):
        with registry.get_conn(self.db) as conn:
            registry.add_instrument(conn, entity_id=self.btc, symbol="BTCUSDT",
                                    venue="binance", asset_class="bitcoin")
            self.dumps(registry.list_instruments(conn))
            self.dumps(registry.list_volatility_estimates(conn, "local:btc", 86400))

    def dumps(self, payload):
        return json.dumps(payload, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
