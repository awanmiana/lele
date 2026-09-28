"""A stored move must be a move of the length it claims to be.

Bars arrive from a bounded, resumable fetch, so a series can contain a hole: a
fetch that stopped after 1000 daily bars, or one whose provider missed days, or
a partial restore from a backup. The detector measures a return as the
difference between two closes a fixed number of bars apart. When a bar is
missing, that difference silently spans more than the requested horizon while
still being labelled as, say, a 24-hour move — and it is then counted, compared
against a baseline built on the same distorted series, and stored.

These tests pin the guarantee that such a window is refused rather than
measured, and that the refusal is visible in the report.

No network. Bars are written directly into the registry.
"""
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, UTC
from decimal import Decimal
from pathlib import Path

from lele.analysis import moves
from lele.core import registry

BASE = datetime(2026, 1, 1, tzinfo=UTC)
KEY = "binance:BTCUSDT"
DAY = 86400


def bar(conn, moment, close, interval_seconds=DAY):
    registry.add_price_bar(
        conn, instrument_key=KEY, interval_seconds=interval_seconds,
        open_time=moment.isoformat(), close_time=(moment + timedelta(seconds=interval_seconds)).isoformat(),
        open=str(close), high=str(close * 1.01), low=str(close * 0.99), close=str(close),
        volume="100", source="binance", retrieved_at=moment.isoformat(),
        source_url="https://api.binance.com/api/v3/klines")


def series(conn, closes, step=timedelta(seconds=DAY), start=BASE, skip=(), interval_seconds=DAY):
    """Write a series at `step` spacing, leaving `skip` indices unwritten."""
    for index, close in enumerate(closes):
        if index in skip:
            continue
        bar(conn, start + step * index, close, interval_seconds)
    conn.commit()


class GapCase(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.conn = sqlite3.connect(Path(directory.name) / "registry.db")
        self.conn.row_factory = sqlite3.Row
        registry._initialize(self.conn)
        self.addCleanup(self.conn.close)
        self.entity_id = registry.upsert_entity(conn=self.conn, kind="instrument",
                                                name="Bitcoin", key=KEY)

    def detect(self, **kwargs):
        arguments = {"instrument_key": KEY, "interval_seconds": DAY, "move_hours": 24,
                     "thresholds": (3,), "baseline_bars": 20, "limit": 100, "store": False}
        arguments.update(kwargs)
        return moves.detect(self.conn, **arguments)

    def test_a_missing_bar_is_reported_not_hidden(self):
        series(self.conn, [100] * 60, skip=(30,))
        report = self.detect()
        self.assertEqual(report["coverage"]["cadence_seconds"], DAY,
                         "the bar spacing must be measured, not assumed")
        self.assertEqual(report["coverage"]["gaps"], 1)
        self.assertIn("missing bar", report["coverage"]["coverage_note"])

    def test_no_window_across_a_gap_is_reported_as_a_move(self):
        """The hole sits inside the largest excursion; it must not become a move."""
        closes = [100] * 60
        closes[31] = 100
        closes[32] = 400
        closes[33] = 100
        series(self.conn, closes, skip=(30,))
        with_gap = self.detect()
        for move in with_gap["moves"]:
            start = datetime.fromisoformat(move["start_time"])
            end = datetime.fromisoformat(move["end_time"])
            self.assertLessEqual((end - start).total_seconds(), 24 * 3600,
                                 "a reported move must not span the hole")
        self.assertEqual(with_gap["summary"]["status"], "ok")

    def test_the_same_data_without_the_hole_reports_the_move(self):
        """Proves the gap is what suppressed it, not the threshold or the sample."""
        closes = [100] * 60
        closes[31] = 100
        closes[32] = 400
        closes[33] = 100
        series(self.conn, closes)
        contiguous = self.detect()
        self.assertEqual(contiguous["coverage"]["gaps"], 0)
        self.assertTrue(contiguous["moves"],
                        "a 300% day in a contiguous series must be detected")
        self.assertGreater(max(abs(Decimal(m["change_percent"])) for m in contiguous["moves"]),
                           Decimal(200))

    def test_every_reported_move_covers_exactly_the_horizon(self):
        closes = []
        for index in range(120):
            closes.append(100 if index % 7 else 130)
        series(self.conn, closes, skip=(20, 21, 60))
        for move in self.detect()["moves"]:
            start = datetime.fromisoformat(move["start_time"])
            end = datetime.fromisoformat(move["end_time"])
            self.assertAlmostEqual((end - start).total_seconds() / 3600, 24, delta=1.2,
                                   msg=f"window {move['start_time']}..{move['end_time']}")

    def test_current_refuses_a_window_that_spans_a_gap(self):
        closes = [100] * 40
        closes[35] = 150
        series(self.conn, closes, skip=(38,))
        reading = moves.current(self.conn, KEY, DAY, 24)
        self.assertEqual(reading["status"], "window_spans_gap")
        self.assertIn("missing", reading["note"])
        self.assertNotIn("change_percent", reading,
                         "a window of unknown length must not report a change")

    def test_current_reports_a_contiguous_window(self):
        series(self.conn, [100 + index for index in range(40)])
        reading = moves.current(self.conn, KEY, DAY, 24)
        self.assertEqual(reading["status"], "ok")
        self.assertEqual(reading["elapsed_hours"], "24.0000")
        self.assertIn("change_percent", reading)

    def test_sub_daily_series_reports_its_own_cadence(self):
        step = timedelta(hours=4)
        series(self.conn, [100 + (index % 5) for index in range(120)], step=step,
               interval_seconds=4 * 3600)
        report = self.detect(interval_seconds=4 * 3600, move_hours=24)
        self.assertEqual(report["coverage"]["cadence_seconds"], 4 * 3600)
        for move in report["moves"]:
            start = datetime.fromisoformat(move["start_time"])
            end = datetime.fromisoformat(move["end_time"])
            self.assertAlmostEqual((end - start).total_seconds() / 3600, 24, delta=1.2)

    def test_completeness_is_not_claimed(self):
        series(self.conn, [100] * 60)
        report = self.detect()
        self.assertEqual(report["coverage"]["completeness"], "unknown")
        self.assertNotIn("all_history", report["coverage"],
                         "a field that is always False is a claim nothing can check")

    def test_helpers_are_pure(self):
        first = [BASE, BASE + timedelta(seconds=DAY), BASE + timedelta(days=2)]
        second = [BASE, BASE + timedelta(days=5), BASE + timedelta(days=6)]
        self.assertEqual(moves._cadence(first, DAY), DAY)
        self.assertEqual(moves._gaps(first, DAY), frozenset())
        self.assertEqual(moves._gaps(second, DAY), frozenset({1}))


if __name__ == "__main__":
    unittest.main()
