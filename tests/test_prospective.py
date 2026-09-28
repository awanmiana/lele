import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, UTC
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from lele.analysis import prospective, projection
from lele.cli.main import main
from lele.core import registry


class ProspectiveTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db = self.root / "registry #?.db"
        self.ledger = self.root / "forecast-ledger.jsonl"
        self.config_path = self.root / "config.json"
        self.path = self.root / "prices fixture.json"
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(registry._SCHEMA)
        self.eid = registry.upsert_entity(self.conn, "company", "Fixture", key="fixture:one")
        self.conn.commit()
        self.addCleanup(self.conn.close)
        self.instrument = {
            "entity_key": "fixture:one", "symbol": "EXPLICIT", "asset_class": "stock",
            "venue": "fixture venue", "currency": "USD", "unit": "share",
            "price_type": "last", "source_url": "local:prospective-fixture",
        }
        self.start = datetime(2024, 1, 1, tzinfo=UTC)
        self.config = {
            "method": prospective.comparison.METHODS[0],
            "required_asset_classes": ["stock"],
            "target_metric": "combined",
            "tolerance_bps": 100,
            "target_rate": "0.9",
            "min_settled_per_class": 1,
        }

    def write_config(self, config=None):
        self.config_path.write_text(json.dumps(self.config if config is None else config), encoding="utf-8")
        return self.config_path

    def write_prices(self, prices, minutes=None, instrument=None, path=None):
        if minutes is None:
            minutes = range(0, len(prices) * 5, 5)
        payload = {
            "instrument": dict(instrument or self.instrument),
            "prices": [{"timestamp": (self.start + timedelta(minutes=m)).isoformat(), "price": p}
                       for m, p in zip(minutes, prices)],
        }
        target = path or self.path
        target.write_text(json.dumps(payload), encoding="utf-8")
        return target

    def invoke(self, args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = main(args)
        return status, out.getvalue(), err.getvalue()

    def preregister(self, config=None, force=False, now=None):
        return prospective.preregister(self.write_config(config), self.ledger, force, now)

    def write_evidence(self, rows, path=None):
        target = path or (self.root / "evidence.json")
        target.write_text(json.dumps({"observations": rows}), encoding="utf-8")
        return target

    def evidence_row(self, identifier, kind, value, minutes, unit="currency"):
        stamp = (self.start + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {
            "id": identifier, "entity_key": "fixture:one", "kind": kind,
            "observed_at": stamp, "available_at": stamp,
            "source_url": "local:prospective-fixture", "description": "Synthetic evidence",
            "platform": "fixture", "industry": None, "location": None,
            "measurement": {"value": value, "currency": "USD", "unit": unit, "basis": "observed"},
            "mapping": None,
        }

    def test_preregister_creates_chain_and_reports(self):
        result = self.preregister(now=self.start)
        self.assertEqual(result["action"], "preregister")
        self.assertEqual(result["config"], self.config)
        self.assertEqual(result["records"], 1)
        raw = self.ledger.read_text(encoding="utf-8")
        self.assertEqual(len(raw.strip().splitlines()), 1)
        records, _ = prospective._read_ledger(self.ledger)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["record"], "run")
        self.assertEqual(records[0]["seq"], 0)
        self.assertEqual(records[0]["prev"], "")
        self.assertEqual(records[0]["run_id"], result["run_id"])

    def test_preregister_refuses_overwrite_and_force_replaces(self):
        self.preregister(now=self.start)
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.preregister(now=self.start)
        replaced = self.preregister({"method": prospective.comparison.METHODS[1],
                                     "required_asset_classes": ["gold"],
                                     "target_metric": "direction", "tolerance_bps": 0,
                                     "target_rate": 0.5, "min_settled_per_class": 2}, force=True,
                                    now=self.start)
        records, _ = prospective._read_ledger(self.ledger)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["run_id"], replaced["run_id"])
        self.assertEqual(records[0]["config"]["method"], prospective.comparison.METHODS[1])

    def test_run_id_deterministic_and_config_normalized(self):
        first = self.preregister(now=self.start)
        second = prospective.preregister(self.write_config(), self.ledger, True, self.start)
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertEqual(second["config"]["target_rate"], "0.9")
        self.assertEqual(second["config"]["required_asset_classes"], ["stock"])

    def test_config_validation_rejects_bad_values(self):
        cases = [
            {"method": "unknown"}, {"required_asset_classes": []},
            {"required_asset_classes": ["stock", "stock"]},
            {"required_asset_classes": ["bonds"]}, {"target_metric": "profit"},
            {"tolerance_bps": -1}, {"tolerance_bps": 10001}, {"tolerance_bps": True},
            {"target_rate": 0}, {"target_rate": 1.5}, {"target_rate": "nan"},
            {"min_settled_per_class": 0}, {"min_settled_per_class": "3"},
        ]
        for change in cases:
            config = dict(self.config)
            config.update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.preregister(config)
        with self.assertRaises(ValueError):
            self.preregister({k: v for k, v in self.config.items() if k != "method"})
        with self.assertRaises(ValueError):
            self.preregister({**self.config, "extra": 1})

    def test_forecast_appends_prospective_and_is_idempotent(self):
        self.preregister(now=self.start)
        self.write_prices(["1", "2", "3"])
        result = prospective.forecast(self.conn, self.eid, self.path, self.ledger, now=self.start)
        self.assertTrue(result["appended"])
        record = result["forecast"]
        self.assertEqual(record["status"], "prospective")
        self.assertEqual(record["method"], prospective.comparison.METHODS[0])
        self.assertEqual(record["last_price"], "3")
        self.assertEqual(record["forecast_price"], "3")
        self.assertEqual(record["as_of"], (self.start + timedelta(minutes=10)).isoformat())
        self.assertEqual(record["target_time"], (self.start + timedelta(minutes=15)).isoformat())
        again = prospective.forecast(self.conn, self.eid, self.path, self.ledger, now=self.start)
        self.assertFalse(again["appended"])
        self.assertEqual(again["forecast"]["forecast_id"], record["forecast_id"])
        records, _ = prospective._read_ledger(self.ledger)
        self.assertEqual(len(records), 2)

    def test_forecast_backfilled_when_target_is_not_future(self):
        self.preregister(now=self.start)
        self.write_prices(["1", "2", "3"])
        late = self.start + timedelta(minutes=20)
        result = prospective.forecast(self.conn, self.eid, self.path, self.ledger, now=late)
        self.assertEqual(result["forecast"]["status"], "backfilled")

    def test_forecast_requires_consecutive_history(self):
        self.preregister(now=self.start)
        self.write_prices(["1", "2", "3"], minutes=[0, 5, 20])
        with self.assertRaisesRegex(ValueError, "gap"):
            prospective.forecast(self.conn, self.eid, self.path, self.ledger, now=self.start)
        self.write_prices(["1", "2"])
        with self.assertRaisesRegex(ValueError, "three recorded observations"):
            prospective.forecast(self.conn, self.eid, self.path, self.ledger, now=self.start)

    def test_forecast_identity_and_registry_checks(self):
        self.preregister(now=self.start)
        self.write_prices(["1", "2", "3"], instrument={**self.instrument, "entity_key": "fixture:other"})
        with self.assertRaisesRegex(ValueError, "does not match"):
            prospective.forecast(self.conn, self.eid, self.path, self.ledger, now=self.start)
        self.write_prices(["1", "2", "3"])
        with self.assertRaises(ValueError):
            prospective.forecast(self.conn, self.eid + 1, self.path, self.ledger, now=self.start)
        for eid in (0, -1, True, 1.0, "1", None):
            with self.subTest(eid=eid), self.assertRaises(ValueError):
                prospective.forecast(self.conn, eid, self.path, self.ledger, now=self.start)

    def test_forecast_without_preregistration_fails(self):
        self.write_prices(["1", "2", "3"])
        self.ledger.write_text("", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "no pre-registration"):
            prospective.forecast(self.conn, self.eid, self.path, self.ledger, now=self.start)

    def test_evidence_method_config_and_arguments(self):
        result = self.preregister({**self.config, "method": prospective.EVIDENCE_METHOD}, now=self.start)
        self.assertEqual(result["config"]["method"], prospective.EVIDENCE_METHOD)
        self.write_prices(["1", "2", "3"])
        with self.assertRaisesRegex(ValueError, "requires an evidence file"):
            prospective.forecast(self.conn, self.eid, self.path, self.ledger, now=self.start)
        self.preregister(self.config, force=True, now=self.start)
        evidence = self.root / "evidence.json"
        evidence.write_text(json.dumps({"observations": []}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "only used by evidence methods"):
            prospective.forecast(self.conn, self.eid, self.path, self.ledger, now=self.start,
                                 evidence_path=str(evidence))

    def test_evidence_method_versions_registered(self):
        self.assertIn(prospective.EVIDENCE_METHOD, prospective.METHODS)
        self.assertIn(prospective.EVIDENCE_METHOD_V2, prospective.METHODS)
        self.assertEqual(prospective.EVIDENCE_METHOD, "evidence_score_10bps_v1")
        self.assertEqual(prospective.EVIDENCE_METHOD_V2, "evidence_score_10bps_v2")

    def test_weighted_evidence_method_forecast_and_record(self):
        result = self.preregister({**self.config, "method": prospective.EVIDENCE_METHOD_V2}, now=self.start)
        self.assertEqual(result["config"]["method"], prospective.EVIDENCE_METHOD_V2)
        self.write_prices(["1", "2", "3"])
        evidence = self.write_evidence([
            self.evidence_row("flow", "order_flow", "-1", 9),
            self.evidence_row("mood", "social_sentiment", "1", 9, unit="score"),
        ])
        with self.assertRaisesRegex(ValueError, "requires an evidence file"):
            prospective.forecast(self.conn, self.eid, self.path, self.ledger, now=self.start)
        forecast = prospective.forecast(self.conn, self.eid, self.path, self.ledger, now=self.start,
                                        evidence_path=str(evidence))["forecast"]
        self.assertEqual(forecast["method"], prospective.EVIDENCE_METHOD_V2)
        self.assertEqual(forecast["world_state"]["net_direction"], "flat")
        self.assertEqual(forecast["evidence_scoring"]["net_direction"], "down")
        self.assertEqual(forecast["evidence_scoring"]["weighted_score"], -6)
        self.assertEqual(forecast["evidence_scoring"]["method"], "kind_weight_recency_buckets_v1")
        self.assertEqual(forecast["evidence_step_bps"], 10)
        self.assertEqual(Decimal(forecast["forecast_price"]), Decimal("2.997"))
        before_db = self.db.read_bytes()
        status, out, err = self.invoke(["--db", str(self.db), "--json", "prospective", "forecast",
                                        "--ledger", str(self.ledger), "--id", str(self.eid),
                                        "--path", str(self.path), "--evidence", str(evidence)])
        self.assertEqual((status, err), (0, ""))
        self.assertFalse(json.loads(out)["appended"])
        self.assertEqual(self.db.read_bytes(), before_db)

    def test_observation_window_seconds_validation(self):
        for value in (0, 86401, True, "300"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.preregister({**self.config, "observation_window_seconds": value})
        result = self.preregister({**self.config, "observation_window_seconds": 1}, now=self.start, force=True)
        self.assertEqual(result["config"]["observation_window_seconds"], 1)

    def test_ledger_tamper_is_detected(self):
        self.preregister(now=self.start)
        self.write_prices(["1", "2", "3"])
        prospective.forecast(self.conn, self.eid, self.path, self.ledger, now=self.start)
        lines = self.ledger.read_text(encoding="utf-8").splitlines()
        lines[1] = lines[1].replace('"forecast_price":"3"', '"forecast_price":"9"')
        self.ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            prospective.score(self.ledger)
        with self.assertRaises(ValueError):
            prospective.forecast(self.conn, self.eid, self.path, self.ledger, now=self.start)

    def test_settle_matches_exact_target_and_leaves_gaps_pending(self):
        self.preregister(now=self.start)
        self.write_prices(["1", "2", "3"])
        prospective.forecast(self.conn, self.eid, self.path, self.ledger, now=self.start)
        self.write_prices(["1", "2", "3", "3"], minutes=[0, 5, 10, 15])
        result = prospective.settle(self.conn, self.eid, self.path, self.ledger, now=self.start)
        self.assertEqual((result["settled"], result["pending"]), (1, 0))
        outcome = result["settlements"][0]["outcomes"]
        self.assertTrue(outcome["direction_hit"])
        self.assertTrue(outcome["tolerance_hit"])
        self.assertTrue(outcome["exact_hit"])
        self.assertTrue(outcome["combined_hit"])
        self.assertEqual(result["settlements"][0]["actual_price"], "3")
        again = prospective.settle(self.conn, self.eid, self.path, self.ledger, now=self.start)
        self.assertEqual(again["settled"], 0)
        self.write_prices(["1", "2", "3", "4"], minutes=[0, 5, 10, 15])
        gap = prospective.settle(self.conn, self.eid, self.path, self.ledger, now=self.start)
        self.assertEqual((gap["settled"], gap["pending"]), (0, 0))

    def test_settle_gap_stays_pending(self):
        self.preregister(now=self.start)
        self.write_prices(["1", "2", "3"])
        prospective.forecast(self.conn, self.eid, self.path, self.ledger, now=self.start)
        self.write_prices(["1", "2", "3", "5"], minutes=[0, 5, 10, 20])
        result = prospective.settle(self.conn, self.eid, self.path, self.ledger, now=self.start)
        self.assertEqual((result["settled"], result["pending"]), (0, 1))

    def test_score_direction_tolerance_exact_and_combined(self):
        config = {**self.config, "target_metric": "direction", "tolerance_bps": 50}
        self.preregister(config, now=self.start)
        self.write_prices(["1", "2", "3"])
        prospective.forecast(self.conn, self.eid, self.path, self.ledger, now=self.start)
        self.write_prices(["1", "2", "3", "4"], minutes=[0, 5, 10, 15])
        prospective.settle(self.conn, self.eid, self.path, self.ledger, now=self.start)
        result = prospective.score(self.ledger)
        overall = result["metrics"]["overall"]
        self.assertEqual(overall["settled"], 1)
        self.assertEqual(overall["direction_hits"], 0)
        self.assertEqual(overall["tolerance_hits"], 0)
        self.assertEqual(overall["exact_hits"], 0)
        self.assertEqual(overall["combined_hits"], 0)
        self.assertEqual(overall["target_hits"], 0)
        self.assertEqual(overall["rate"], "0")
        self.assertEqual(result["objective"]["status"], "not_achieved")
        self.assertFalse(result["objective"]["target_met"])

    def test_score_counts_hits_and_aggregates_by_class(self):
        self.preregister(self.config, now=self.start)
        self.write_prices(["1", "2", "3"])
        prospective.forecast(self.conn, self.eid, self.path, self.ledger, now=self.start)
        self.write_prices(["1", "2", "3", "3"], minutes=[0, 5, 10, 15])
        prospective.settle(self.conn, self.eid, self.path, self.ledger, now=self.start)
        result = prospective.score(self.ledger)
        overall = result["metrics"]["overall"]
        self.assertEqual(overall["combined_hits"], 1)
        self.assertEqual(overall["rate"], "1")
        self.assertEqual(overall["mae"], "0")
        self.assertEqual(overall["mae_units"], ["share"])
        self.assertEqual(result["metrics"]["by_asset_class"]["stock"]["rate"], "1")
        self.assertEqual(result["coverage"], {
            "issued": 1, "prospective": 1, "backfilled": 0, "settled_prospective": 1,
            "pending_prospective": 0, "settled_all_statuses": 1,
            "by_asset_class": {"stock": {"issued": 1, "prospective": 1, "settled": 1}},
        })

    def test_backfilled_forecasts_are_excluded_from_scoring(self):
        self.preregister(self.config, now=self.start)
        self.write_prices(["1", "2", "3"])
        late = self.start + timedelta(minutes=20)
        prospective.forecast(self.conn, self.eid, self.path, self.ledger, now=late)
        self.write_prices(["1", "2", "3", "3"], minutes=[0, 5, 10, 15])
        prospective.settle(self.conn, self.eid, self.path, self.ledger, now=late)
        result = prospective.score(self.ledger)
        self.assertEqual(result["coverage"]["issued"], 1)
        self.assertEqual(result["coverage"]["prospective"], 0)
        self.assertEqual(result["coverage"]["backfilled"], 1)
        self.assertEqual(result["metrics"]["overall"]["settled"], 0)
        self.assertEqual(result["objective"]["status"], "not_achieved")

    def test_objective_met_across_required_classes(self):
        bitcoin = registry.upsert_entity(self.conn, "company", "Bitcoin Fixture", key="fixture:btc")
        self.conn.commit()
        config = {
            "method": prospective.comparison.METHODS[1],
            "required_asset_classes": ["bitcoin", "stock"],
            "target_metric": "combined", "tolerance_bps": 10, "target_rate": "0.9",
            "min_settled_per_class": 1,
        }
        self.preregister(config, now=self.start)
        self.write_prices(["1", "2", "3"])
        prospective.forecast(self.conn, self.eid, self.path, self.ledger, now=self.start)
        btc_instrument = {**self.instrument, "entity_key": "fixture:btc", "symbol": "BTCFIX",
                          "asset_class": "bitcoin", "unit": "BTC"}
        btc_path = self.write_prices(["10", "11", "12"], instrument=btc_instrument, path=self.root / "btc.json")
        prospective.forecast(self.conn, bitcoin, btc_path, self.ledger, now=self.start)
        self.write_prices(["1", "2", "3", "4"], minutes=[0, 5, 10, 15])
        prospective.settle(self.conn, self.eid, self.path, self.ledger, now=self.start)
        btc_settle = self.write_prices(["10", "11", "12", "13"], instrument=btc_instrument,
                                       path=self.root / "btc-settle.json")
        prospective.settle(self.conn, bitcoin, btc_settle, self.ledger, now=self.start)
        result = prospective.score(self.ledger)
        self.assertEqual(result["metrics"]["overall"]["rate"], "1")
        self.assertEqual(result["metrics"]["required_asset_classes"]["bitcoin"]["rate"], "1")
        self.assertTrue(result["objective"]["target_met"])
        self.assertEqual(result["objective"]["status"], "prospective_target_met_unverified")

    def test_score_specific_run_and_unknown_run(self):
        first = self.preregister(now=self.start)
        result = prospective.score(self.ledger, first["run_id"])
        self.assertEqual(result["run_id"], first["run_id"])
        self.assertEqual(prospective.score(self.ledger)["run_id"], first["run_id"])
        with self.assertRaisesRegex(ValueError, "run not found"):
            prospective.score(self.ledger, "0" * 64)

    def test_precision_under_hostile_caller_context(self):
        self.preregister(self.config, now=self.start)
        self.write_prices(["1", "2", "3"])
        prospective.forecast(self.conn, self.eid, self.path, self.ledger, now=self.start)
        self.write_prices(["1", "2", "3", "3"], minutes=[0, 5, 10, 15])
        prospective.settle(self.conn, self.eid, self.path, self.ledger, now=self.start)
        expected = prospective.score(self.ledger)
        from decimal import Inexact, ROUND_UP, localcontext
        with localcontext() as context:
            context.prec = 2
            context.rounding = ROUND_UP
            context.Emax = 2
            context.Emin = -2
            context.traps[Inexact] = True
            result = prospective.score(self.ledger)
        self.assertEqual(result, expected)
        self.assertEqual(result["metrics"]["overall"]["rate"], "1")

    def test_cli_roundtrip_and_read_only_database(self):
        config_path = self.write_config()
        self.write_prices(["1", "2", "3"])
        before_db = self.db.read_bytes()
        base = ["--db", str(self.db), "--json", "prospective"]
        with patch.object(prospective, "_now", return_value=self.start), \
                patch("socket.socket", side_effect=AssertionError("network forbidden")):
            status, out, err = self.invoke(base + ["preregister", "--ledger", str(self.ledger),
                                                   "--config", str(config_path)])
            self.assertEqual((status, err), (0, ""))
            self.assertEqual(json.loads(out)["config"], self.config)
            status, out, err = self.invoke(base + ["forecast", "--ledger", str(self.ledger),
                                                   "--id", str(self.eid), "--path", str(self.path)])
            self.assertEqual((status, err), (0, ""))
            self.assertTrue(json.loads(out)["appended"])
            self.write_prices(["1", "2", "3", "3"], minutes=[0, 5, 10, 15])
            status, out, err = self.invoke(base + ["settle", "--ledger", str(self.ledger),
                                                   "--id", str(self.eid), "--path", str(self.path)])
            self.assertEqual((status, err), (0, ""))
            self.assertEqual(json.loads(out)["settled"], 1)
            status, out, err = self.invoke(base + ["score", "--ledger", str(self.ledger)])
            self.assertEqual((status, err), (0, ""))
            self.assertEqual(json.loads(out)["metrics"]["overall"]["rate"], "1")
            self.assertEqual(self.db.read_bytes(), before_db)
            human = self.invoke([a for a in base + ["score", "--ledger", str(self.ledger)] if a != "--json"])
            self.assertEqual((human[0], human[2]), (0, ""))
            self.assertEqual(json.loads(human[1])["metrics"]["overall"]["rate"], "1")

    def test_cli_subprocess_roundtrip(self):
        config_path = self.write_config()
        self.write_prices(["1", "2", "3"])
        base = ["--db", str(self.db), "--json", "prospective"]
        for args in (["preregister", "--ledger", str(self.ledger), "--config", str(config_path)],
                     ["forecast", "--ledger", str(self.ledger), "--id", str(self.eid), "--path", str(self.path)]):
            result = subprocess.run([sys.executable, "-m", "lele", *base, *args],
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual((result.returncode, result.stderr), (0, ""), result.stderr)

    def test_cli_invalid_arguments_fail_before_database_creation(self):
        missing = self.root / "absent.db"
        ledger = str(self.root / "new-ledger.jsonl")
        cases = [
            ["prospective", "preregister", "--ledger", ledger],
            ["prospective", "forecast", "--ledger", ledger],
            ["prospective", "forecast", "--ledger", ledger, "--id", "1"],
            ["prospective", "settle", "--ledger", ledger, "--path", "x"],
            ["prospective", "score", "--ledger", ledger, "--run", "not-a-hash"],
            ["prospective", "score", "--ledger", ledger, "--id", "1"],
            ["prospective", "preregister", "--ledger", ledger, "--config", "c", "--id", "1"],
        ]
        for args in cases:
            with self.subTest(args=args):
                status, out, err = self.invoke(["--db", str(missing), *args])
                self.assertEqual((status, out), (2, ""))
        self.assertFalse(missing.exists())
        self.assertFalse(Path(ledger).exists())

    def test_cli_ledger_protects_registry(self):
        status, out, err = self.invoke(["--db", str(self.db), "prospective", "score", "--ledger", str(self.db)])
        self.assertEqual((status, out), (1, ""))
        self.assertIn("registry", err)

    def test_cli_preregister_failure_does_not_create_database(self):
        missing = self.root / "absent.db"
        self.write_config()
        status, out, err = self.invoke(["--db", str(missing), "prospective", "preregister",
                                        "--ledger", str(self.ledger), "--config", str(self.config_path)])
        self.assertEqual((status, err), (0, ""))
        self.assertFalse(missing.exists())
        self.assertTrue(self.ledger.exists())

    def test_score_empty_ledger_run(self):
        self.preregister(now=self.start)
        result = prospective.score(self.ledger)
        self.assertEqual(result["coverage"]["issued"], 0)
        self.assertEqual(result["metrics"]["overall"]["settled"], 0)
        self.assertIsNone(result["metrics"]["overall"]["rate"])
        self.assertEqual(result["objective"]["status"], "not_achieved")
        json.dumps(result, allow_nan=False)

    def test_projection_load_unchanged_by_forecast(self):
        self.preregister(now=self.start)
        self.write_prices(["1", "2", "3"])
        raw = self.path.read_bytes()
        prospective.forecast(self.conn, self.eid, self.path, self.ledger, now=self.start)
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertEqual(projection.METHOD, "five_minute_persistence_v1")
        with self.assertRaises(ValueError):
            prospective.preregister(self.config_path, self.ledger, force=False)


if __name__ == "__main__":
    unittest.main()
