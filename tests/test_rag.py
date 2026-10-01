import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, UTC
from decimal import Decimal
from pathlib import Path

from lele.core import clock, registry
from lele.analysis import rag


class RagTests(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / "registry.db"
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        registry._initialize(self.conn)
        self.addCleanup(self.conn.close)

    def test_store_events_inserts(self):
        events = [{
            "event_type": "political_event",
            "external_id": "2026-09-17-001",
            "title": "Proposed Rule: Environmental Protection Standards",
            "actor_key": "political:REGULATION-PROPOSED:2026-09-17-001",
            "actor_name": "Environmental Protection Agency",
            "government_entity": "Environmental Protection Agency",
            "publication_date": "2026-09-17",
            "abstract": "This proposed rule establishes new environmental protection standards.",
            "html_url": "https://www.federalregister.gov/documents/2026/09/17/2026-09-17-001",
        }]
        result = rag.store_events(self.conn, events, source="political-event")
        self.assertEqual(result["stored"], 1)
        rows = self.conn.execute("SELECT * FROM event_store WHERE event_type='political_event'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["actor_name"], "Environmental Protection Agency")

    def test_store_events_skips_empty_title(self):
        events = [{
            "event_type": "political_event",
            "external_id": "2026-09-17-001",
            "title": "",
            "actor_key": "test",
            "government_entity": "EPA",
            "publication_date": "2026-09-17",
        }]
        result = rag.store_events(self.conn, events)
        self.assertEqual(result["skipped"], 1)

    def test_store_events_duplicate_ignored(self):
        events = [{
            "event_type": "political_event",
            "external_id": "2026-09-17-001",
            "title": "Test Rule",
            "actor_key": "test",
            "government_entity": "EPA",
            "publication_date": "2026-09-17",
        }]
        rag.store_events(self.conn, events)
        rag.store_events(self.conn, events)
        rows = self.conn.execute("SELECT COUNT(*) as c FROM event_store").fetchone()
        self.assertEqual(rows["c"], 1)

    def test_build_event_graph_creates_relationships(self):
        recent = datetime.now(UTC).date().isoformat()
        events = [
            {"event_type": "political_event", "external_id": "001",
             "title": "First Event", "actor_key": "actor_a",
             "government_entity": "Agency A", "publication_date": recent},
            {"event_type": "regulatory_action", "external_id": "002",
             "title": "Second Event", "actor_key": "actor_a",
             "government_entity": "Agency A", "publication_date": recent},
        ]
        rag.store_events(self.conn, events)
        count = rag.build_event_graph(self.conn, window_hours=48)
        self.assertGreater(count, 0)
        rels = self.conn.execute("SELECT * FROM event_relationships").fetchall()
        self.assertGreater(len(rels), 0)

    def bars(self, closes, instrument_key="binance:BTCUSDT", interval_seconds=86400,
             start="2026-01-01T00:00:00+00:00"):
        first = datetime.fromisoformat(start)
        for index, close in enumerate(closes):
            open_time = first.timestamp() + index * interval_seconds
            stamp = datetime.fromtimestamp(open_time, UTC).isoformat()
            registry.add_price_bar(
                self.conn, instrument_key=instrument_key, interval_seconds=interval_seconds,
                open_time=stamp, close_time=datetime.fromtimestamp(
                    open_time + interval_seconds, UTC).isoformat(),
                open=str(close), high=str(close * 1.01), low=str(close * 0.99),
                close=str(close), volume="1", source="test",
                retrieved_at="2026-02-01T00:00:00+00:00")

    def test_a_window_unusual_for_the_instrument_is_flagged(self):
        """A calm series with one large move must produce exactly that one anomaly.

        The previous version of this test manufactured `observations` rows with
        `source='price'`, which no fetcher has ever written, so it passed while the
        command itself could only ever report zero.
        """
        quiet = [100 + (index % 3) for index in range(120)]
        self.bars(quiet + [100, 100, 100, 140])
        report = rag.detect_price_anomalies(
            self.conn, "binance:BTCUSDT", 86400, mode="percentile", level="95",
            window_bars=4, baseline_bars=100, store=False)
        self.assertEqual(report["status"], "ok")
        self.assertGreater(len(report["anomalies"]), 0)
        flagged = report["anomalies"][-1]
        self.assertEqual(flagged["direction"], "up")
        self.assertEqual(flagged["anomaly_type"], "price_move")
        self.assertFalse(flagged["stored"])

    def test_the_anomaly_is_written_so_the_indicator_can_see_it(self):
        """The table had no writer at all, so `event_indicator_v1`'s anomaly
        component was structurally always zero.

        The clock is pinned rather than relying on fixture dates landing inside a
        30-day window: a test that depends on today's date is a test that fails on
        a day nobody changed anything.
        """
        quiet = [100 + (index % 3) for index in range(120)]
        self.bars(quiet + [100, 100, 100, 140])
        report = rag.detect_price_anomalies(
            self.conn, "binance:BTCUSDT", 86400, window_bars=4, baseline_bars=100)
        self.assertTrue(any(row["stored"] for row in report["anomalies"]))
        with clock.freeze("2026-05-10T00:00:00+00:00"):
            indicator = rag.event_indicator_v1(self.conn, "binance:BTCUSDT")
        self.assertGreater(indicator["anomaly_count"], 0)

    def test_no_bars_reports_insufficient_rather_than_zero_anomalies(self):
        """Zero anomalies and no data must not look the same."""
        report = rag.detect_price_anomalies(
            self.conn, "binance:BTCUSDT", 86400, window_bars=4, baseline_bars=100)
        self.assertEqual(report["status"], "insufficient_bars")
        self.assertEqual(report["anomalies"], [])
        self.assertIn("insufficient_bars", report["skipped_reasons"])

    def test_a_gap_in_the_window_is_skipped_not_judged(self):
        closes = [100 + (index % 3) for index in range(120)]
        self.bars(closes)
        report = rag.detect_price_anomalies(
            self.conn, "binance:BTCUSDT", 86400, window_bars=4, baseline_bars=100)
        self.assertEqual(report["coverage"]["gaps"], 0)
        self.assertGreater(report["windows_examined"], 0)

    def test_bad_rule_parameters_are_refused(self):
        with self.assertRaises(ValueError):
            rag.detect_price_anomalies(self.conn, "k", 86400, mode="percentile", level="100")
        with self.assertRaises(ValueError):
            rag.detect_price_anomalies(self.conn, "k", 86400, window_bars=1)
        with self.assertRaises(ValueError):
            rag.detect_price_anomalies(self.conn, "k", 86400, baseline_bars=0)

    def test_anomaly_reports_survive_json_dumps(self):
        """Severity is derived from a reported percentile, which is text.

        Comparing that text against a float raised `TypeError` deep inside the
        first command a user ran with a real anomaly in the data. Only serializing
        the finished report catches it.
        """
        quiet = [100 + (index % 3) for index in range(120)]
        self.bars(quiet + [100, 100, 100, 140])
        report = rag.detect_price_anomalies(
            self.conn, "binance:BTCUSDT", 86400, mode="percentile", level="95",
            window_bars=4, baseline_bars=100)
        json.dumps(report, allow_nan=False)
        for row in report["anomalies"]:
            with self.subTest(observed_at=row["observed_at"]):
                self.assertIn(row["severity"],
                              ("low", "moderate", "high", "critical", "unknown"))
                json.dumps(row, allow_nan=False)

    def test_severity_comes_from_the_percentile_observed_not_the_level_asked_for(self):
        """A permissive 95th-percentile rule must not manufacture critical alarms."""
        self.assertEqual(rag._severity("99.9", True), "critical")
        self.assertEqual(rag._severity("99.1", True), "high")
        self.assertEqual(rag._severity("96", True), "moderate")
        self.assertEqual(rag._severity("95.5", True), "moderate")
        self.assertEqual(rag._severity("", True), "unknown")
        self.assertEqual(rag._severity(None, True), "unknown")
        self.assertEqual(rag._severity("99.9", False), "unknown")

    def test_a_percentile_that_is_not_a_number_is_unknown_not_a_crash(self):
        self.assertIsNone(rag._percentile_number("not a number"))
        self.assertIsNone(rag._percentile_number(""))
        self.assertIsNone(rag._percentile_number(None))
        self.assertEqual(rag._percentile_number("99.5"), Decimal("99.5"))

    def test_scan_without_an_instrument_key_reads_stored_bars(self):
        quiet = [100 + (index % 3) for index in range(120)]
        self.bars(quiet + [100, 100, 100, 140])
        result = rag.detect_anomalies(self.conn, window_bars=4, baseline_bars=100, store=False)
        self.assertEqual(result["scanned"], 1)
        self.assertGreater(result["anomalies"], 0)

    def test_event_indicator_v1(self):
        rag.store_events(self.conn, [{
            "event_type": "political_event", "external_id": "001",
            "title": "Test Event", "actor_key": "actor_a",
            "government_entity": "EPA", "publication_date": "2026-09-19",
        }])
        result = rag.event_indicator_v1(self.conn)
        self.assertIn("score", result)
        self.assertIn("event_count", result)
        self.assertIn("flow_count", result)
        self.assertIn("anomaly_count", result)
        self.assertIn("breakdown", result)
        self.assertEqual(result["event_count"], 1)

    def test_attribute_money_flows(self):
        rag.store_events(self.conn, [{
            "event_type": "political_event", "external_id": "001",
            "title": "Test Event", "actor_key": "actor_a",
            "government_entity": "EPA", "publication_date": "2026-09-19",
        }])
        count = rag.attribute_money_flows(self.conn, event_ids=[1], window_hours=48)
        self.assertGreaterEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
