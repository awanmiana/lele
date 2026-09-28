import copy
import hashlib
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, UTC
from decimal import Context, Decimal, Inexact, ROUND_UP, localcontext
from pathlib import Path
from unittest.mock import patch

from lele.analysis import comparison, projection
from lele.cli.main import main
from lele.core import registry


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "prices fixture.json"
        self.db = self.root / "registry #?.db"
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(registry._SCHEMA)
        self.eid = registry.upsert_entity(self.conn, "company", "Fixture", key="fixture:one")
        self.conn.commit()
        self.addCleanup(self.conn.close)
        self.instrument = {
            "entity_key": "fixture:one", "symbol": "EXPLICIT", "asset_class": "stock",
            "venue": "fixture venue", "currency": "USD", "unit": "share",
            "price_type": "last", "source_url": "local:comparison-fixture",
        }
        self.start = datetime(2024, 1, 1, tzinfo=UTC)

    def payload(self, prices=None, minutes=None):
        if prices is None:
            prices = [str(i) for i in range(1, 11)]
        if minutes is None:
            minutes = range(0, len(prices) * 5, 5)
        return {
            "instrument": dict(self.instrument),
            "prices": [{"timestamp": (self.start + timedelta(minutes=m)).isoformat(), "price": p}
                       for m, p in zip(minutes, prices)],
        }

    def run_payload(self, payload=None, percent=70):
        self.path.write_text(json.dumps(self.payload() if payload is None else payload), encoding="utf-8")
        return comparison.compare(self.conn, self.eid, self.path, percent)

    def invoke(self, args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = main(args)
        return status, out.getvalue(), err.getvalue()

    def test_cli_roundtrip_read_only(self):
        expected = self.run_payload()
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        argv = ["--db", str(self.db), "--json", "compare", str(self.eid), str(self.path), "--train-percent", "70"]
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            status, out, err = self.invoke(argv)
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(json.loads(out), expected)
        result = subprocess.run([sys.executable, "-m", "lele", *argv], capture_output=True, text=True, timeout=20)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual(json.loads(result.stdout), expected)
        human = self.invoke([a for a in argv if a != "--json"])
        self.assertEqual((human[0], human[2]), (0, ""))
        self.assertEqual(json.loads(human[1]), expected)
        self.assertEqual({p.name: p.read_bytes() for p in self.root.iterdir()}, before)
        self.assertEqual(expected["training"]["eligible_predictions"], 4)
        self.assertEqual(expected["test"]["eligible_predictions"], 3)
        self.assertEqual(expected["selection"]["method"], comparison.METHODS[1])
        self.assertEqual(expected["selection"]["test_score"]["mae"], "0")

    def test_report_shape_metrics_and_hash(self):
        payload = self.payload()
        raw = b"\xef\xbb\xbf" + json.dumps(payload, indent=2).encode() + b"\n"
        self.path.write_bytes(raw)
        with patch.object(projection, "_load", wraps=projection._load) as loader:
            result = comparison.compare(self.conn, self.eid, self.path)
        loader.assert_called_once_with(self.path)
        self.assertEqual(set(result), {"method", "evaluation_kind", "entity_id", "instrument",
                                      "horizon_seconds", "split", "selection", "training", "test",
                                      "objective", "provenance", "limitations"})
        self.assertEqual(result["instrument"], self.instrument)
        self.assertEqual(result["entity_id"], self.eid)
        self.assertEqual(result["horizon_seconds"], 300)
        self.assertEqual(result["provenance"], {
            "sha256": hashlib.sha256(raw).hexdigest(), "input_bytes": len(raw),
            "validation": "syntax_only", "truth_verified": False,
        })
        self.assertEqual(result["split"], {
            "train_percent": 70,
            "rule": "first floor(observation_count * train_percent / 100) observations; targets partitioned by index",
            "train_observations": 7, "test_observations": 3,
            "train_start": "2024-01-01T00:00:00+00:00", "train_end": "2024-01-01T00:30:00+00:00",
            "test_start": "2024-01-01T00:35:00+00:00", "test_end": "2024-01-01T00:45:00+00:00",
        })
        for section, count, warmup in (("training", 4, 3), ("test", 3, 0)):
            score = result[section]
            self.assertEqual(set(score), {"status", "target_observations", "eligible_predictions",
                                         "skipped_warmup_targets", "skipped_gap_targets", "methods", "predictions"})
            self.assertEqual(score["status"], "evaluated")
            self.assertEqual(score["target_observations"], count + warmup)
            self.assertEqual(score["skipped_warmup_targets"], warmup)
            self.assertEqual(score["skipped_gap_targets"], 0)
            for method, error in zip(comparison.METHODS, ("1", "0", "2")):
                self.assertEqual(score["methods"][method], {
                    "eligible_predictions": count, "exact_hits": count if error == "0" else 0,
                    "exact_match_rate": "1" if error == "0" else "0", "mae": error,
                    "nonpositive_forecasts": 0,
                })
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertEqual(result["objective"]["status"], "not_achieved")
        self.assertEqual(result["evaluation_kind"], "exploratory_historical_not_prospective")
        self.assertNotIn("historical_target_met", json.dumps(result))
        json.dumps(result, allow_nan=False)

    def test_constant_deterministic_tie_never_achieves_objective(self):
        for prices in (["10"] * 10, ["10.0", "1e1", "+10.00", "10", "10.000"] * 2):
            result = self.run_payload(self.payload(prices))
            self.assertEqual(result["selection"]["method"], comparison.METHODS[0])
            self.assertEqual(result["selection"]["tie_order"], list(comparison.METHODS))
            for section in ("training", "test"):
                for score in result[section]["methods"].values():
                    self.assertEqual(score["exact_hits"], score["eligible_predictions"])
                    self.assertEqual(score["exact_match_rate"], "1")
                    self.assertEqual(Decimal(score["mae"]), 0)
            self.assertEqual(result["objective"]["status"], "not_achieved")

    def test_selection_prioritizes_hits_before_mae(self):
        result = self.run_payload(self.payload(["1", "2", "3", "4", "100", "100", "100", "100"]), 75)
        scores = result["training"]["methods"]
        self.assertEqual(scores[comparison.METHODS[0]]["exact_hits"], 1)
        self.assertEqual(scores[comparison.METHODS[1]]["exact_hits"], 1)
        self.assertEqual(result["selection"]["method"], comparison.METHODS[0])
        result = self.run_payload(self.payload(["1", "2", "3", "4", "100", "101", "102", "103"]), 75)
        self.assertEqual(result["selection"]["method"], comparison.METHODS[1])
        self.assertGreater(Decimal(result["training"]["methods"][comparison.METHODS[1]]["mae"]),
                           Decimal(result["training"]["methods"][comparison.METHODS[0]]["mae"]))

    def test_mae_breaks_zero_hit_tie_and_mean_can_win(self):
        result = self.run_payload(self.payload(["1", "7", "1", "4", "5", "6"]), 70)
        self.assertTrue(all(s["exact_hits"] == 0 for s in result["training"]["methods"].values()))
        self.assertEqual(result["selection"]["method"], comparison.METHODS[2])
        self.assertEqual(result["training"]["methods"][comparison.METHODS[2]]["mae"], "1")

    def test_mutated_suffix_cannot_change_training_or_selection(self):
        original = self.run_payload()
        changed = self.payload()
        for row in changed["prices"][7:]:
            row["price"] = "999999"
        result = self.run_payload(changed)
        self.assertEqual(result["training"], original["training"])
        for field in ("status", "rule", "tie_order", "method"):
            self.assertEqual(result["selection"][field], original["selection"][field])
        self.assertNotEqual(result["selection"]["test_score"], original["selection"]["test_score"])
        self.assertEqual(result["test"]["predictions"][0]["prices"], original["test"]["predictions"][0]["prices"])
        self.assertEqual(result["test"]["predictions"][1]["prices"][comparison.METHODS[0]], "999999")
        self.assertNotEqual(result["provenance"]["sha256"], original["provenance"]["sha256"])
        self.assertGreater(result["test"]["methods"][comparison.METHODS[0]]["exact_hits"],
                           result["selection"]["test_score"]["exact_hits"])
        self.assertEqual(result["selection"]["method"], comparison.METHODS[1])

    def test_prefix_forecasts_and_current_label_mutations(self):
        payload = self.payload(["2", "9", "4", "7", "1", "5", "8", "6", "3", "10"])
        full = self.run_payload(payload)
        predictions = full["training"]["predictions"] + full["test"]["predictions"]
        for index in range(3, 10):
            prefix = copy.deepcopy(payload)
            prefix["prices"] = prefix["prices"][:index + 1]
            result = self.run_payload(prefix)
            rows = result["training"]["predictions"] + result["test"]["predictions"]
            self.assertEqual(rows, predictions[:index - 2])
            changed = copy.deepcopy(payload)
            for row in changed["prices"][index:]:
                row["price"] = "98765"
            result = self.run_payload(changed)
            rows = result["training"]["predictions"] + result["test"]["predictions"]
            self.assertEqual(rows[:index - 3], predictions[:index - 3])
            self.assertEqual(rows[index - 3]["prices"], predictions[index - 3]["prices"])

    def test_train_test_target_separation_and_identical_eligibility(self):
        result = self.run_payload()
        training = result["training"]["predictions"]
        test = result["test"]["predictions"]
        self.assertEqual([r["target_index"] for r in training], [3, 4, 5, 6])
        self.assertEqual([r["target_index"] for r in test], [7, 8, 9])
        self.assertLess(training[-1]["target_time"], test[0]["target_time"])
        self.assertEqual(test[0]["as_of"], result["split"]["train_end"])
        for row in training + test:
            self.assertEqual(set(row["prices"]), set(comparison.METHODS))
            self.assertLess(row["history_start"], row["as_of"])
            self.assertLess(row["as_of"], row["target_time"])
        for section in ("training", "test"):
            score = result[section]
            self.assertTrue(all(m["eligible_predictions"] == len(score["predictions"])
                                for m in score["methods"].values()))

    def test_gap_at_each_window_position_is_not_bridged(self):
        for gap in range(1, 10):
            minutes = [i * 5 + (5 if i >= gap else 0) for i in range(10)]
            result = self.run_payload(self.payload(["1"] * 10, minutes))
            expected = [i for i in range(3, 10) if not i - 2 <= gap <= i]
            rows = result["training"]["predictions"] + result["test"]["predictions"]
            self.assertEqual([r["target_index"] for r in rows], expected)
            for section in ("training", "test"):
                score = result[section]
                self.assertEqual(score["target_observations"], score["eligible_predictions"] +
                                 score["skipped_warmup_targets"] + score["skipped_gap_targets"])

    def test_small_datasets_and_empty_training(self):
        for length in range(2, 6):
            for percent in (1, 50, 70, 99):
                result = self.run_payload(self.payload(["1"] * length), percent)
                split = length * percent // 100
                self.assertEqual(result["training"]["eligible_predictions"], max(0, split - 3))
                self.assertEqual(result["test"]["eligible_predictions"], max(0, length - max(split, 3)))
                if split <= 3:
                    self.assertEqual(result["training"]["status"], "insufficient_data")
                    self.assertEqual(result["selection"]["status"], "no_eligible_training_targets")
                    self.assertIsNone(result["selection"]["method"])
                    self.assertIsNone(result["selection"]["test_score"])
                    for score in result["training"]["methods"].values():
                        self.assertEqual(score["exact_hits"], 0)
                        self.assertIsNone(score["mae"])
                        self.assertIsNone(score["exact_match_rate"])
                if not split:
                    self.assertIsNone(result["split"]["train_start"])
                    self.assertIsNone(result["split"]["train_end"])

    def test_no_eligible_training_and_test_are_explicit(self):
        result = self.run_payload(self.payload(["1"] * 10, range(0, 100, 10)))
        self.assertEqual(result["training"]["status"], "no_eligible_targets")
        self.assertEqual(result["test"]["status"], "no_eligible_targets")
        self.assertIsNone(result["selection"]["method"])
        result = self.run_payload(self.payload(["1"] * 10, [0, 5, 10, 15, 20, 25, 30, 40, 45, 50]))
        self.assertEqual(result["selection"]["status"], "selected")
        self.assertEqual(result["test"]["status"], "no_eligible_targets")
        self.assertEqual(result["selection"]["test_score"]["eligible_predictions"], 0)
        self.assertIsNone(result["selection"]["test_score"]["mae"])

    def test_nonpositive_forecasts_retained_unclamped_and_scored(self):
        for third, forecast, error in (("1", "-1", "2"), ("1.5", "0", "1")):
            result = self.run_payload(self.payload(["4", "3", third, "1", "1", "1"]), 70)
            section = result["training"]
            self.assertEqual(Decimal(section["predictions"][0]["prices"][comparison.METHODS[1]]), Decimal(forecast))
            score = section["methods"][comparison.METHODS[1]]
            self.assertEqual(score["nonpositive_forecasts"], 1)
            self.assertEqual(Decimal(score["mae"]), Decimal(error))
            self.assertEqual(score["exact_hits"], 0)
            self.assertTrue(all(s["eligible_predictions"] == 1 for s in section["methods"].values()))
        result = self.run_payload(self.payload(["4", "3", "2", "1", "1", "1"]), 70)
        self.assertEqual(result["selection"]["method"], comparison.METHODS[1])
        self.assertEqual(result["test"]["predictions"][0]["prices"][comparison.METHODS[1]], "0")
        self.assertEqual(result["selection"]["test_score"]["nonpositive_forecasts"], 1)
        self.assertEqual(result["selection"]["test_score"]["eligible_predictions"], 2)
        self.assertEqual(result["selection"]["test_score"]["mae"], "0.5")

    def test_precision_and_hostile_caller_context(self):
        payload = self.payload(["1." + "0" * 59 + str(i) for i in range(1, 7)])
        expected = self.run_payload(payload)
        with localcontext() as context:
            context.prec = 2
            context.rounding = ROUND_UP
            context.Emax = 2
            context.Emin = -2
            context.traps[Inexact] = True
            before = context.copy()
            result = self.run_payload(payload)
            self.assertEqual(context.prec, before.prec)
            self.assertEqual(context.rounding, before.rounding)
            self.assertEqual(context.flags, before.flags)
            self.assertEqual(context.traps, before.traps)
        self.assertEqual(result, expected)
        self.assertEqual(result["selection"]["method"], comparison.METHODS[1])
        self.assertEqual(Decimal(result["training"]["methods"][comparison.METHODS[0]]["mae"]), Decimal("1e-60"))
        result = self.run_payload(self.payload(["9999999999999", "1e-12", "1e-12", "1e-12", "1", "2"]))
        with localcontext(Context(prec=128)):
            expected_mae = (Decimal("9999999999999") - Decimal("1e-12")) / 3
        self.assertEqual(Decimal(result["training"]["methods"][comparison.METHODS[2]]["mae"]), expected_mae)

    def test_nonterminating_mean_and_rate_are_128_digit_strings(self):
        result = self.run_payload(self.payload(["1", "1", "2", "1", "1", "1", "3", "4", "5", "6"]))
        with localcontext(Context(prec=128)):
            self.assertEqual(result["training"]["predictions"][0]["prices"][comparison.METHODS[2]], str(Decimal(4) / 3))
            self.assertEqual(result["test"]["methods"][comparison.METHODS[1]]["exact_match_rate"], str(Decimal(2) / 3))
        result = self.run_payload(self.payload(["1", "2", "3", "2", "1", "1"]))
        self.assertEqual(result["training"]["methods"][comparison.METHODS[2]]["exact_hits"], 1)

    def test_api_read_only_and_transaction_neutral(self):
        self.run_payload()
        registry.set_attr(self.conn, self.eid, "pending", "yes")
        before = self.conn.total_changes
        self.conn.execute("PRAGMA query_only=ON")
        statements = []
        self.conn.set_trace_callback(statements.append)
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            comparison.compare(self.conn, self.eid, self.path)
            self.assertTrue(self.conn.in_transaction)
            with self.assertRaises(ValueError):
                comparison.compare(self.conn, self.eid, self.path, 100)
            self.assertTrue(self.conn.in_transaction)
        self.assertEqual(self.conn.total_changes, before)
        self.assertTrue(all(s.lstrip().upper().startswith("SELECT") for s in statements))
        self.conn.rollback()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM attributes").fetchone()[0], 0)

    def test_invalid_api_arguments_identity_and_files(self):
        self.run_payload()
        for percent in (0, 100, -1, True, 70.0, "70", None):
            with self.subTest(percent=percent), self.assertRaises(ValueError):
                comparison.compare(self.conn, self.eid, self.path, percent)
        for eid in (0, -1, True, 1.0, "1", None, 2**63, self.eid + 1):
            with self.subTest(eid=eid), self.assertRaises(ValueError):
                comparison.compare(self.conn, eid, self.path)
        payload = self.payload()
        payload["instrument"]["entity_key"] = "fixture:other"
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.run_payload(payload)
        with self.assertRaises(FileNotFoundError):
            comparison.compare(self.conn, self.eid, self.root / "missing.json")

    def test_shared_schema_validation_and_bounds(self):
        valid = self.payload()
        for field, value in (("price", "0"), ("price", "-1"), ("price", 1), ("price", "NaN"),
                             ("price", "1e13"), ("price", "1e-13"), ("timestamp", "2024-01-01T00:01:00Z"),
                             ("timestamp", valid["prices"][0]["timestamp"])):
            payload = copy.deepcopy(valid)
            payload["prices"][1][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.run_payload(payload)
        for count in (0, 1, 10001):
            with self.subTest(count=count), self.assertRaises(ValueError):
                self.run_payload(self.payload(["1"] * count))
        result = self.run_payload(self.payload(["1"] * 10000))
        self.assertEqual(result["training"]["eligible_predictions"], 6997)
        self.assertEqual(result["test"]["eligible_predictions"], 3000)
        raw = json.dumps(valid).encode()
        for invalid in (b"\xff", b"{", b"[" * 2000, raw.replace(b'"instrument":', b'"instrument":{},"instrument":'),
                        raw.replace(b'"1"', b'NaN', 1), raw.replace(b'"prices":', b'"extra":1,"prices":')):
            self.path.write_bytes(invalid)
            with self.assertRaises(ValueError):
                comparison.compare(self.conn, self.eid, self.path)
        self.path.write_bytes(raw + b" " * (projection.MAX_FILE_BYTES - len(raw)))
        self.assertEqual(comparison.compare(self.conn, self.eid, self.path)["provenance"]["input_bytes"], projection.MAX_FILE_BYTES)
        self.path.write_bytes(raw + b" " * (projection.MAX_FILE_BYTES + 1 - len(raw)))
        with self.assertRaisesRegex(ValueError, "2 MiB"):
            comparison.compare(self.conn, self.eid, self.path)

    def test_cli_invalid_args_fail_before_database_creation(self):
        missing = self.root / "absent.db"
        base = ["--db", str(missing), "compare", "1", str(self.path)]
        for value in ("0", "100", "-1", "1.5", "bad", "9" * 5000):
            status, out, err = self.invoke(base + ["--train-percent", value])
            self.assertEqual((status, out), (2, ""))
            self.assertIn("train-percent", err)
        for args in (["compare"], ["compare", "1"], ["compare", "0", "x"],
                     ["compare", "1", ""], base + ["--train-per", "70"]):
            self.assertEqual(self.invoke(args)[0], 2)
        self.assertEqual(self.invoke(base)[0], 1)
        self.assertFalse(missing.exists())
        self.assertEqual(self.invoke(["compare", "--help"])[0], 0)

    def test_cli_insufficient_data_and_invalid_payload(self):
        self.run_payload(self.payload(["1", "1"]))
        args = ["--db", str(self.db), "--json", "compare", str(self.eid), str(self.path)]
        status, out, err = self.invoke(args)
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(json.loads(out)["test"]["status"], "insufficient_data")
        before = self.db.read_bytes()
        self.path.write_text("{}")
        self.assertEqual(self.invoke(args)[0], 1)
        self.assertEqual(self.db.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
