import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, UTC
from decimal import Context, Decimal, localcontext
from pathlib import Path
from unittest.mock import patch

from lele.analysis import episodes
from lele.cli.main import main
from lele.core import registry


class EpisodeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "prices.json"
        self.evidence = self.root / "evidence.json"
        self.db = self.root / "registry.db"
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(registry._SCHEMA)
        self.eid = registry.upsert_entity(self.conn, "instrument", "Synthetic", key="fixture:asset")
        self.conn.commit()
        self.addCleanup(self.conn.close)
        self.instrument = {
            "entity_key": "fixture:asset", "symbol": "TEST", "asset_class": "bitcoin",
            "venue": "synthetic", "currency": "USD", "unit": "coin",
            "price_type": "last", "source_url": "local:synthetic",
        }
        self.prices = [
            ("2024-01-01T00:00:00Z", "100"), ("2024-01-01T00:05:00Z", "105"),
            ("2024-01-01T00:10:00Z", "110"), ("2024-01-01T00:15:00Z", "105"),
            ("2024-01-01T00:20:00Z", "100"),
        ]
        self.path.write_text(json.dumps({
            "instrument": dict(self.instrument),
            "prices": [{"timestamp": t, "price": p} for t, p in self.prices],
        }), encoding="utf-8")

    def record(self, identifier, value, observed, available=None):
        return {
            "id": identifier, "entity_key": "fixture:asset", "kind": "fund_flow",
            "observed_at": observed, "available_at": available or observed,
            "source_url": "local:synthetic-evidence", "description": "Synthetic evidence",
            "platform": "fixture", "industry": None, "location": None,
            "measurement": {"value": value, "currency": "USD", "unit": "currency", "basis": "observed"},
            "mapping": None,
        }

    def run_analyze(self, rows=None, **kwargs):
        if rows is not None:
            self.evidence.write_text(json.dumps({"observations": rows}), encoding="utf-8")
        return episodes.analyze(self.conn, self.eid, self.path,
                                self.evidence if rows is not None else None, **kwargs)

    def test_zigzag_detects_pump_and_dump(self):
        report = self.run_analyze()
        self.assertEqual(report["summary"]["pumps"], 1)
        self.assertEqual(report["summary"]["dumps"], 1)
        self.assertEqual(len(report["episodes"]), 2)
        pump, dump = report["episodes"]
        self.assertEqual(pump["direction"], "up")
        self.assertEqual(pump["start"], "2024-01-01T00:00:00+00:00")
        self.assertEqual(pump["end"], "2024-01-01T00:10:00+00:00")
        self.assertEqual(Decimal(pump["magnitude_percent"]), Decimal(10))
        with localcontext(Context(prec=128)):
            self.assertEqual(Decimal(pump["duration_hours"]), Decimal(600) / Decimal(3600))
        self.assertEqual(dump["direction"], "down")
        self.assertEqual(dump["start_price"], "110")
        self.assertEqual(dump["end_price"], "100")
        self.assertEqual(report["coverage"]["evidence_records_supplied"], 0)
        json.dumps(report, allow_nan=False)

    def test_zigzag_threshold_changes_episodes(self):
        report = self.run_analyze(threshold_percent=100)
        self.assertEqual(report["summary"]["pumps"], 0)
        self.assertEqual(report["summary"]["dumps"], 0)

    def test_precursor_is_point_in_time(self):
        rows = [self.record("before", "5", "2024-01-01T00:07:00Z"),
                self.record("late", "5", "2024-01-01T00:08:00Z", available="2024-01-01T00:12:00Z")]
        report = self.run_analyze(rows)
        pump, dump = report["episodes"]
        self.assertEqual(pump["precursor"]["records"], 0)
        self.assertEqual(dump["precursor"]["records"], 1)
        self.assertEqual(dump["precursor"]["observation_ids"], ["before"])
        self.assertEqual(dump["precursor"]["direction_score"], 1)
        comparison = report["summary"]["precursor_comparison"]
        self.assertEqual(comparison["dump"]["episodes_with_evidence"], 1)
        self.assertEqual(comparison["pump"]["episodes_with_evidence"], 0)

    def test_regime_structure(self):
        report = self.run_analyze(horizon_hours=1)
        for episode in report["episodes"]:
            self.assertIn("regime", episode)
            self.assertIn(episode["regime"]["label"], ("bull", "bear", "range", "unknown"))
            self.assertTrue(episode["regime"]["lookback_return_percent"] is None
                            or isinstance(episode["regime"]["lookback_return_percent"], str))

    def write_prices(self, prices):
        self.path.write_text(json.dumps({
            "instrument": dict(self.instrument),
            "prices": [{"timestamp": t, "price": p} for t, p in prices],
        }), encoding="utf-8")

    def long_series(self):
        base = datetime(2024, 1, 1, tzinfo=UTC)
        times = [(base + timedelta(minutes=5 * i)).isoformat() for i in range(61)]
        prices = [100.0] * 61
        prices[5], prices[6], prices[7], prices[8] = 104.0, 108.0, 104.0, 100.0
        self.write_prices([(times[i], str(prices[i])) for i in range(61)])
        return times

    def test_non_episode_controls_exclude_legs_and_preceding_horizon(self):
        self.long_series()
        report = self.run_analyze(horizon_hours=1, control_stride=12)
        self.assertEqual([leg["direction"] for leg in report["episodes"]], ["up", "down"])
        excluded = set()
        for leg in report["episodes"]:
            excluded.update(range(leg["start_index"], leg["end_index"] + 1))
        controls = report["controls"]
        self.assertEqual([control["index"] for control in controls], [9, 21, 33, 45, 57])
        for control in controls:
            self.assertNotIn(control["index"], excluded)
        comparison = report["summary"]["control_comparison"]
        self.assertEqual(comparison["eligible"], 52)
        self.assertEqual(comparison["sampled"], 5)
        self.assertFalse(comparison["truncated"])
        self.assertEqual(comparison["with_evidence"], 0)
        self.assertIsNone(comparison["mean_direction_score"])
        json.dumps(report, allow_nan=False)

    def test_control_precursors_use_evidence_point_in_time(self):
        times = self.long_series()
        report = self.run_analyze([self.record("c", "5", times[19])], horizon_hours=1,
                                  control_stride=12)
        sampled = next(control for control in report["controls"] if control["index"] == 21)
        self.assertEqual(sampled["precursor"]["records"], 1)
        self.assertEqual(sampled["precursor"]["observation_ids"], ["c"])
        self.assertEqual(sampled["precursor"]["direction_score"], 1)
        comparison = report["summary"]["control_comparison"]
        self.assertEqual(comparison["with_evidence"], 1)
        self.assertEqual(comparison["mean_direction_score"], "1")
        self.assertEqual(comparison["precursor_kind_totals"], {"fund_flow": 1})

    def test_invalid_options_identity_and_bounds(self):
        for kwargs in ({"threshold_percent": 0}, {"threshold_percent": 1001},
                       {"threshold_percent": True}, {"horizon_hours": 0}, {"horizon_hours": 73},
                       {"limit": 0}, {"limit": 1001}, {"control_stride": 0},
                       {"control_stride": episodes.CONTROL_STRIDE_MAX + 1},
                       {"control_stride": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.run_analyze(**kwargs)
        with self.assertRaises(ValueError):
            episodes.analyze(self.conn, 999, self.path)
        bad = json.loads(self.path.read_text(encoding="utf-8"))
        bad["instrument"]["entity_key"] = "other"
        self.path.write_text(json.dumps(bad), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.run_analyze()

    def test_cli_roundtrip_read_only(self):
        self.evidence.write_text(json.dumps({"observations": [
            self.record("before", "5", "2024-01-01T00:07:00Z")]}), encoding="utf-8")
        before = self.db.read_bytes()
        argv = ["--db", str(self.db), "--json", "episodes", str(self.eid), str(self.path),
                "--evidence", str(self.evidence)]
        out, err = io.StringIO(), io.StringIO()
        with patch("socket.socket", side_effect=AssertionError("network forbidden")), \
                redirect_stdout(out), redirect_stderr(err):
            status = main(argv)
        self.assertEqual((status, err.getvalue()), (0, ""))
        result = json.loads(out.getvalue())
        self.assertEqual(result["summary"]["pumps"], 1)
        self.assertEqual(result["summary"]["dumps"], 1)
        self.assertEqual(self.db.read_bytes(), before)

    def test_cli_control_stride(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = main(["--db", str(self.db), "--json", "episodes", str(self.eid), str(self.path),
                           "--control-stride", "5"])
        self.assertEqual((status, err.getvalue()), (0, ""))
        result = json.loads(out.getvalue())
        self.assertIn("control_comparison", result["summary"])
        self.assertEqual(result["summary"]["control_comparison"]["stride"], 5)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["episodes", "1", str(self.path), "--control-stride", "0"]), 2)

    def test_cli_rejects_bad_threshold(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["episodes", "1", str(self.path), "--threshold-percent", "0"]), 2)


if __name__ == "__main__":
    unittest.main()
