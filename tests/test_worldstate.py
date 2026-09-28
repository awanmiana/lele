import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, UTC
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from lele.analysis import worldstate
from lele.cli.main import main
from lele.core import registry


def _time(value):
    return datetime.fromisoformat(value).astimezone(UTC)


class WorldstateFeatureTests(unittest.TestCase):
    def row(self, identifier, kind, observed, available=None, value=None, mapping=None,
            currency="USD", unit="currency", basis="observed"):
        measurement = None
        if value is not None:
            measurement = {"value": value, "currency": currency, "unit": unit, "basis": basis}
        return {
            "id": identifier, "kind": kind, "observed_at": observed,
            "available_at": available or observed, "measurement": measurement, "mapping": mapping,
        }

    def records(self, rows):
        return [(_time(r["observed_at"]), _time(r["available_at"]), r) for r in rows]

    def test_point_in_time_window_and_availability(self):
        rows = [
            self.row("inside", "fund_flow", "2024-01-01T00:04:00Z", value="1"),
            self.row("edge", "fund_flow", "2024-01-01T00:00:00Z", value="1"),
            self.row("outside", "fund_flow", "2023-12-31T23:54:00Z", value="1"),
            self.row("late", "fund_flow", "2024-01-01T00:03:00Z", available="2024-01-01T00:06:00Z", value="1"),
        ]
        features = worldstate.build_features(self.records(rows), _time("2024-01-01T00:05:00Z"), 300)
        self.assertEqual(set(features["observation_ids"]), {"inside", "edge"})
        self.assertEqual(features["direction_score"], 2)
        self.assertEqual(features["net_direction"], "up")
        self.assertEqual(features["records"], 2)

    def test_one_second_window(self):
        rows = [self.row("almost", "fund_flow", "2024-01-01T00:04:59Z", value="1"),
                self.row("older", "fund_flow", "2024-01-01T00:04:58Z", value="1")]
        features = worldstate.build_features(self.records(rows), _time("2024-01-01T00:05:00Z"), 1)
        self.assertEqual(features["observation_ids"], ["almost"])

    def test_sentiment_routes_and_reason_bases(self):
        rows = [
            self.row("flow", "onchain_transfer", "2024-01-01T00:04:00Z", value="10",
                     mapping={"from": "wallet-a", "to": "exchange-b", "actor": None, "action": None,
                              "reason": None, "reason_basis": "unknown", "related_ids": [],
                              "source_locator": None}),
            self.row("mood", "social_sentiment", "2024-01-01T00:04:30Z", value="-3", unit="score"),
        ]
        features = worldstate.build_features(self.records(rows), _time("2024-01-01T00:05:00Z"), 300)
        self.assertEqual(features["routed_amount_by_currency"], {"USD": "10"})
        self.assertEqual(features["origins"], ["wallet-a"])
        self.assertEqual(features["destinations"], ["exchange-b"])
        self.assertEqual(features["reason_bases"], {"unknown": 1})
        self.assertEqual(features["sentiment"], {"records": 1, "total": "-3"})
        self.assertEqual(features["direction_score"], 0)

    def test_weighted_direction_kind_weights_and_recency(self):
        rows = [
            self.row("fast", "order_flow", "2024-01-01T00:04:30Z", value="1"),
            self.row("slow", "order_flow", "2024-01-01T00:00:30Z", value="1"),
            self.row("mood", "social_sentiment", "2024-01-01T00:04:00Z", value="1"),
            self.row("positions", "open_interest", "2024-01-01T00:04:00Z", value="1"),
        ]
        scoring = worldstate.weighted_direction(self.records(rows), _time("2024-01-01T00:05:00Z"), 300)
        self.assertEqual(scoring["method"], worldstate.WEIGHTED_SCORING_METHOD)
        self.assertEqual(scoring["recency_buckets"], 3)
        self.assertEqual(scoring["window_seconds"], 300)
        self.assertEqual(scoring["weighted_score"], 15)
        self.assertEqual(scoring["net_direction"], "up")
        self.assertEqual(scoring["scored_records"], 3)
        self.assertEqual(scoring["scored_kinds"], {"order_flow": 2, "social_sentiment": 1})
        self.assertEqual(scoring["weights"], worldstate.DIRECTION_WEIGHTS)

    def test_weighted_direction_excludes_late_outside_and_nondirectional(self):
        rows = [
            self.row("late", "liquidation", "2024-01-01T00:04:00Z",
                     available="2024-01-01T00:06:00Z", value="1"),
            self.row("outside", "order_flow", "2023-12-31T23:00:00Z", value="1"),
            self.row("news", "news_event", "2024-01-01T00:04:00Z"),
        ]
        scoring = worldstate.weighted_direction(self.records(rows), _time("2024-01-01T00:05:00Z"), 300)
        self.assertEqual(scoring["weighted_score"], 0)
        self.assertEqual(scoring["net_direction"], "flat")
        self.assertEqual(scoring["scored_records"], 0)
        self.assertEqual(scoring["scored_kinds"], {})

    def test_weighted_direction_down_and_cancelling_flat(self):
        down = [self.row("a", "order_flow", "2024-01-01T00:04:00Z", value="-2")]
        self.assertEqual(
            worldstate.weighted_direction(self.records(down), _time("2024-01-01T00:05:00Z"))["net_direction"],
            "down")
        flat = [self.row("a", "order_flow", "2024-01-01T00:04:00Z", value="1"),
                self.row("b", "order_flow", "2024-01-01T00:04:00Z", value="-1")]
        scoring = worldstate.weighted_direction(self.records(flat), _time("2024-01-01T00:05:00Z"))
        self.assertEqual(scoring["net_direction"], "flat")
        self.assertEqual(scoring["weighted_score"], 0)

    def test_weighted_direction_window_validation(self):
        for value in (0, -1, 86401, True, "300"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                worldstate.weighted_direction([], _time("2024-01-01T00:05:00Z"), value)


class WorldstateStudyTests(unittest.TestCase):
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
            ("2024-01-01T00:00:00Z", "100"), ("2024-01-01T00:05:00Z", "101"),
            ("2024-01-01T00:10:00Z", "102"), ("2024-01-01T00:15:00Z", "99"),
            ("2024-01-01T00:20:00Z", "98"),
        ]
        self.write_prices()

    def write_prices(self):
        payload = {"instrument": dict(self.instrument),
                   "prices": [{"timestamp": t, "price": p} for t, p in self.prices]}
        self.path.write_text(json.dumps(payload), encoding="utf-8")

    def record(self, identifier, value, observed, available=None, kind="fund_flow"):
        return {
            "id": identifier, "entity_key": "fixture:asset", "kind": kind,
            "observed_at": observed, "available_at": available or observed,
            "source_url": "local:synthetic-evidence", "description": "Synthetic evidence",
            "platform": "fixture", "industry": None, "location": None,
            "measurement": (None if value is None else
                            {"value": value, "currency": "USD", "unit": "currency", "basis": "observed"}),
        }

    def run_study(self, rows, **kwargs):
        self.evidence.write_text(json.dumps({"observations": rows}), encoding="utf-8")
        return worldstate.study(self.conn, self.eid, self.path, self.evidence, **kwargs)

    def test_direction_agreement_and_patterns(self):
        rows = [self.record("a", "1", "2023-12-31T23:59:00Z"),
                self.record("b", "1", "2024-01-01T00:04:00Z"),
                self.record("c", "-1", "2024-01-01T00:09:00Z"),
                self.record("d", "-1", "2024-01-01T00:14:00Z")]
        report = self.run_study(rows)
        self.assertEqual(report["window_seconds"], 300)
        self.assertEqual(report["coverage"]["eligible_boundaries"], 4)
        self.assertEqual(report["coverage"]["boundaries_with_evidence"], 4)
        self.assertEqual(report["direction_agreement"], {"evaluated": 4, "agreements": 4, "rate": "1"})
        self.assertEqual(report["weighted_direction_agreement"],
                         {"evaluated": 4, "agreements": 4, "rate": "1"})
        self.assertEqual(report["direction_agreement_with_evidence"],
                         {"evaluated": 4, "agreements": 4, "rate": "1"})
        self.assertEqual(report["weighted_direction_agreement_with_evidence"],
                         {"evaluated": 4, "agreements": 4, "rate": "1"})
        self.assertEqual(report["weighted_scoring"]["method"], worldstate.WEIGHTED_SCORING_METHOD)
        self.assertEqual(set(report["patterns_by_actual_direction"]), {"up", "down"})
        for bucket in report["patterns_by_actual_direction"].values():
            self.assertIn("mean_direction_score", bucket)
        for row in report["rows"]:
            self.assertNotIn("observed_price", row["features"])
            self.assertNotIn("target_price", row["features"])
            self.assertIn(row["weighted_direction"], ("up", "down", "flat"))

    def test_no_evidence_is_flat_and_disagrees_on_moves(self):
        report = self.run_study([])
        self.assertEqual(report["coverage"]["boundaries_with_evidence"], 0)
        self.assertEqual(report["direction_agreement"]["rate"], "0")
        self.assertTrue(all(row["predicted_direction"] == "flat" for row in report["rows"]))

    def test_late_publication_is_excluded(self):
        rows = [self.record("a", "1", "2024-01-01T00:04:00Z", available="2024-01-01T00:06:00Z")]
        report = self.run_study(rows)
        self.assertEqual(report["coverage"]["boundaries_with_evidence"], 0)
        self.assertTrue(all(row["predicted_direction"] == "flat" for row in report["rows"]))

    def test_future_price_mutation_does_not_change_earlier_features(self):
        rows = [self.record("a", "1", "2024-01-01T00:04:00Z")]
        first = self.run_study(rows)
        first_features = [row["features"] for row in first["rows"]]
        self.prices[-1] = ("2024-01-01T00:20:00Z", "500")
        self.write_prices()
        second = self.run_study(rows)
        for before, after in zip(first_features, [row["features"] for row in second["rows"]]):
            self.assertEqual(before, after)

    def test_extended_kind_and_route_validated(self):
        row = self.record("a", "10", "2024-01-01T00:04:00Z", kind="onchain_transfer")
        row["mapping"] = {"from": "wallet", "to": "exchange", "actor": None, "action": None,
                          "reason": None, "reason_basis": "unknown", "related_ids": [],
                          "source_locator": None}
        report = self.run_study([row])
        target_row = next(r for r in report["rows"] if r["as_of"].startswith("2024-01-01T00:05"))
        self.assertEqual(target_row["features"]["routed_amount_by_currency"], {"USD": "10"})
        bad = self.record("a", "10", "2024-01-01T00:04:00Z", kind="invented")
        with self.assertRaises(ValueError):
            self.run_study([bad])

    def test_invalid_options_identity_and_bounds(self):
        for kwargs in ({"window_seconds": 0}, {"window_seconds": True}, {"window_seconds": 86401},
                       {"limit": 0}, {"limit": 1001}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.evidence.write_text(json.dumps({"observations": []}), encoding="utf-8")
                worldstate.study(self.conn, self.eid, self.path, self.evidence, **kwargs)
        with self.assertRaises(ValueError):
            self.evidence.write_text(json.dumps({"observations": []}), encoding="utf-8")
            worldstate.study(self.conn, 999, self.path, self.evidence)

    def test_unmeasured_event_kinds(self):
        rows = [self.record("n", None, "2024-01-01T00:04:00Z", kind="news_event"),
                self.record("c", None, "2024-01-01T00:04:30Z", kind="calendar_event")]
        report = self.run_study(rows)
        target = next(r for r in report["rows"] if r["as_of"].startswith("2024-01-01T00:05"))
        self.assertEqual(target["features"]["kinds"], {"news_event": 1, "calendar_event": 1})
        self.assertEqual(target["features"]["event_records"], 2)
        self.assertEqual(target["features"]["direction_score"], 0)

    def test_evidence_size_and_row_limit(self):
        with self.assertRaises((FileNotFoundError, ValueError)):
            worldstate.study(self.conn, self.eid, self.path, self.root / "missing.json")
        budget = 2 * 1024 * 1024 + 1
        self.evidence.write_text(" " * budget)
        with self.assertRaises(ValueError):
            worldstate.study(self.conn, self.eid, self.path, self.evidence)

    def test_cli_roundtrip_read_only(self):
        rows = [self.record("a", "1", "2024-01-01T00:04:00Z")]
        self.evidence.write_text(json.dumps({"observations": rows}), encoding="utf-8")
        before_db = self.db.read_bytes()
        argv = ["--db", str(self.db), "--json", "worldstate", str(self.eid), str(self.path),
                "--evidence", str(self.evidence)]
        out, err = io.StringIO(), io.StringIO()
        with patch("socket.socket", side_effect=AssertionError("network forbidden")), \
                redirect_stdout(out), redirect_stderr(err):
            status = main(argv)
        self.assertEqual((status, err.getvalue()), (0, ""))
        result = json.loads(out.getvalue())
        self.assertEqual(result["direction_agreement"]["agreements"], 1)
        self.assertEqual(self.db.read_bytes(), before_db)

    def test_cli_rejects_bad_window(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["worldstate", "1", str(self.path), "--evidence", str(self.evidence),
                                   "--window-seconds", "0"]), 2)

    def test_prospective_evidence_forecast(self):
        from lele.analysis import prospective
        config = self.root / "config.json"
        ledger = self.root / "ledger.jsonl"
        config.write_text(json.dumps({
            "method": prospective.EVIDENCE_METHOD,
            "required_asset_classes": ["bitcoin"],
            "target_metric": "combined", "tolerance_bps": 100, "target_rate": "0.9",
            "min_settled_per_class": 1, "observation_window_seconds": 300,
        }), encoding="utf-8")
        prospective.preregister(str(config), str(ledger), now=_time("2024-01-01T00:10:00Z"))
        window_path = self.root / "window-prices.json"
        window_path.write_text(json.dumps({
            "instrument": dict(self.instrument),
            "prices": [{"timestamp": t, "price": p} for t, p in self.prices[:3]],
        }), encoding="utf-8")
        rows = [self.record("a", "5", "2024-01-01T00:09:00Z")]
        self.evidence.write_text(json.dumps({"observations": rows}), encoding="utf-8")
        result = prospective.forecast(self.conn, self.eid, window_path, ledger,
                                      now=_time("2024-01-01T00:10:00Z"), evidence_path=str(self.evidence))
        record = result["forecast"]
        self.assertEqual(record["method"], prospective.EVIDENCE_METHOD)
        self.assertEqual(record["world_state"]["net_direction"], "up")
        self.assertEqual(Decimal(record["forecast_price"]), Decimal("102.102"))
        with self.assertRaises(ValueError):
            prospective.forecast(self.conn, self.eid, window_path, ledger,
                                 now=_time("2024-01-01T00:10:00Z"))


if __name__ == "__main__":
    unittest.main()
