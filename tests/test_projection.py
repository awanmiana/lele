import copy
import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, UTC
from decimal import Decimal, Inexact, localcontext
from pathlib import Path
from unittest.mock import patch

from lele.analysis import engine, projection
from lele.core import db, registry


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:", factory=db.Connection)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(registry._SCHEMA)
        self.addCleanup(self.conn.close)
        self.eid = registry.upsert_entity(self.conn, "company", "Fixture", key="fixture:one")
        self.conn.commit()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "prices.json"
        self.instrument = {
            "entity_key": "fixture:one", "symbol": "EXPLICIT", "asset_class": "stock",
            "venue": "fixture venue", "currency": "USD", "unit": "share",
            "price_type": "last", "source_url": "local:projection-fixture",
        }
        self.start = datetime(2024, 1, 1, tzinfo=UTC)

    def payload(self, prices=("10.00", "10.00"), minutes=None):
        if minutes is None:
            minutes = range(0, len(prices) * 5, 5)
        return {
            "instrument": dict(self.instrument),
            "prices": [{"timestamp": (self.start + timedelta(minutes=m)).isoformat(), "price": p}
                       for m, p in zip(minutes, prices)],
        }

    def run_payload(self, payload):
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        return projection.project(self.conn, self.eid, self.path)

    def assert_invalid(self, payload):
        before = self.conn.total_changes
        transaction = self.conn.in_transaction
        with patch.object(engine, "finmap") as context:
            with self.assertRaises(ValueError):
                self.run_payload(payload)
        context.assert_not_called()
        self.assertEqual(self.conn.total_changes, before)
        self.assertEqual(self.conn.in_transaction, transaction)

    def test_shape_exact_price_and_raw_provenance(self):
        payload = self.payload(("10.000000000000000000001", "+1.0000000000000000000002e1"))
        raw = b"\xef\xbb\xbf" + json.dumps(payload, indent=2).encode() + b"\n"
        self.path.write_bytes(raw)
        result = projection.project(self.conn, self.eid, self.path)
        self.assertEqual(set(result), {"method", "instrument", "horizon_seconds", "forecast",
                                       "evaluation", "objective", "research", "provenance", "limitations"})
        self.assertEqual(result["method"], "five_minute_persistence_v1")
        self.assertEqual(result["instrument"], self.instrument)
        self.assertEqual(result["horizon_seconds"], 300)
        self.assertEqual(result["forecast"], {
            "as_of": "2024-01-01T00:05:00+00:00", "target_time": "2024-01-01T00:10:00+00:00",
            "price": "+1.0000000000000000000002e1", "status": "baseline_unvalidated",
        })
        self.assertEqual(result["provenance"], {
            "sha256": hashlib.sha256(raw).hexdigest(), "input_bytes": len(raw),
            "validation": "syntax_only", "truth_verified": False,
        })
        self.assertEqual(Decimal(result["evaluation"]["mae"]), Decimal("1e-21"))
        self.assertEqual(self.path.read_bytes(), raw)
        json.dumps(result, allow_nan=False)

    def test_decimal_precision_and_caller_context_independence(self):
        prices = ("1." + "0" * 59 + "1", "1." + "0" * 59 + "2")
        with localcontext() as context:
            context.prec = 2
            context.traps[Inexact] = True
            result = self.run_payload(self.payload(prices))
            self.assertEqual(context.prec, 2)
        self.assertEqual(result["evaluation"]["exact_hits"], 0)
        self.assertEqual(Decimal(result["evaluation"]["mae"]), Decimal("1e-60"))
        result = self.run_payload(self.payload(("9999999999999", "0.000000000001")))
        self.assertEqual(Decimal(result["evaluation"]["mae"]), Decimal("9999999999998.999999999999"))

    def test_constant_history_does_not_achieve_objective(self):
        result = self.run_payload(self.payload(("10", "10.0", "1e1", "10.00")))
        self.assertEqual(result["evaluation"], {
            "eligible_predictions": 3, "skipped_gap_pairs": 0, "exact_hits": 3,
            "exact_match_rate": 1.0, "mae": "0.00", "historical_target_met": True,
            "target_rate": 0.9,
        })
        self.assertEqual(result["objective"]["status"], "not_achieved")
        self.assertIn("prospective 90%", result["objective"]["reason"])
        self.assertIn("not a live or stale-free", " ".join(result["limitations"]))

    def test_threshold_is_historical_only(self):
        result = self.run_payload(self.payload(["1"] * 10 + ["2"]))
        self.assertEqual(result["evaluation"]["exact_match_rate"], 0.9)
        self.assertTrue(result["evaluation"]["historical_target_met"])
        self.assertEqual(result["objective"]["status"], "not_achieved")
        result = self.run_payload(self.payload(["1"] * 9 + ["2", "3"]))
        self.assertFalse(result["evaluation"]["historical_target_met"])

    def test_moving_series_no_hits(self):
        result = self.run_payload(self.payload(("1", "2", "4", "7")))
        self.assertEqual(result["evaluation"], {
            "eligible_predictions": 3, "skipped_gap_pairs": 0, "exact_hits": 0,
            "exact_match_rate": 0.0, "mae": "2", "historical_target_met": False,
            "target_rate": 0.9,
        })
        self.assertEqual(result["forecast"]["price"], "7")

    def test_gaps_are_never_bridged(self):
        result = self.run_payload(self.payload(("1", "2", "100", "100", "200"), [0, 5, 15, 20, 40]))
        self.assertEqual(result["evaluation"], {
            "eligible_predictions": 2, "skipped_gap_pairs": 2, "exact_hits": 1,
            "exact_match_rate": 0.5, "mae": "0.5", "historical_target_met": False,
            "target_rate": 0.9,
        })
        self.assertEqual(result["forecast"]["target_time"], "2024-01-01T00:45:00+00:00")

    def test_no_eligible_pairs(self):
        result = self.run_payload(self.payload(("1", "1", "1"), [0, 10, 20]))
        self.assertEqual(result["evaluation"], {
            "eligible_predictions": 0, "skipped_gap_pairs": 2, "exact_hits": 0,
            "exact_match_rate": None, "mae": None, "historical_target_met": False,
            "target_rate": 0.9,
        })

    def test_prefix_walk_forward_aggregate_has_no_lookahead(self):
        prices = ("2", "2", "5", "4", "4", "8")
        expected_hits, expected_error = 0, Decimal(0)
        previous_result = None
        for length in range(2, len(prices) + 1):
            result = self.run_payload(self.payload(prices[:length]))
            error = abs(Decimal(prices[length - 1]) - Decimal(prices[length - 2]))
            expected_hits += error == 0
            expected_error += error
            evaluation = result["evaluation"]
            self.assertEqual(evaluation["eligible_predictions"], length - 1)
            self.assertEqual(evaluation["exact_hits"], expected_hits)
            with localcontext() as context:
                context.prec = 128
                self.assertEqual(Decimal(evaluation["mae"]), expected_error / (length - 1))
            if previous_result:
                self.assertEqual(previous_result["forecast"]["price"], prices[length - 2])
                self.assertEqual(previous_result["forecast"]["target_time"], result["forecast"]["as_of"])
            previous_result = result

    def test_offset_timestamps_normalized_before_validation(self):
        payload = self.payload()
        payload["prices"][0]["timestamp"] = "2024-01-01T05:30:00+05:30"
        payload["prices"][1]["timestamp"] = "2023-12-31T19:05:00.0000000-05:00"
        result = self.run_payload(payload)
        self.assertEqual(result["forecast"]["as_of"], "2024-01-01T00:05:00+00:00")
        self.assertEqual(result["evaluation"]["eligible_predictions"], 1)
        payload["prices"][1]["timestamp"] = "2024-01-01T00:06:00+00:01"
        self.assertEqual(self.run_payload(payload)["evaluation"]["eligible_predictions"], 1)

    def test_invalid_timestamps(self):
        for timestamp in (
            None, 123, "", "2024-01-01", "2024-01-01T00:05:00", "2024-01-01T00:05:01Z",
            "2024-01-01T00:05:00.1Z", "2024-01-01T00:05:00.0000001Z", "2024-01-01T00:06:00Z",
            "2024-01-01T00:00:00Z", "2023-12-31T23:55:00Z", "2024-01-01T01:00:00+01:00",
            "2024-02-30T00:05:00Z", "2024-01-01T00:05:00+24:00", "2024-01-01T00:05:00+00:60",
            "2024-01-01T00:05:00+00:01", "0001-01-01T00:00:00+01:00",
            "9999-12-31T23:55:00Z", "x" * 65,
        ):
            with self.subTest(timestamp=timestamp):
                payload = self.payload()
                payload["prices"][1]["timestamp"] = timestamp
                self.assert_invalid(payload)

    def test_invalid_prices(self):
        for price in (1, 1.1, True, None, {}, [], "", "0", "-0", "-1", "NaN", "sNaN", "Infinity",
                      "-Infinity", "1e999999999999999999999", "1e-99999999999999999999", "1e13",
                      "1e-13", " 1", "1 ", "1_000", "١", "1" * 65, "1\x00"):
            with self.subTest(price=price):
                payload = self.payload()
                payload["prices"][1]["price"] = price
                self.assert_invalid(payload)

    def test_instrument_required_fields_types_bounds_and_assets(self):
        for field, limit in projection.INSTRUMENT_FIELDS.items():
            for value in (None, 1, True, {}, [], "", " ", "x" * (limit + 1), "a\x00", "\ud800"):
                with self.subTest(field=field, value=str(value)[:20]):
                    payload = self.payload()
                    payload["instrument"][field] = value
                    self.assert_invalid(payload)
            payload = self.payload()
            del payload["instrument"][field]
            self.assert_invalid(payload)
        for asset_class in projection.ASSET_CLASSES:
            payload = self.payload()
            payload["instrument"]["asset_class"] = asset_class
            self.assertEqual(self.run_payload(payload)["instrument"]["asset_class"], asset_class)
        for field, value in (("asset_class", "derivative"), ("entity_key", "fixture:unknown"),
                             ("source_url", "https://localhost/prices"), ("source_url", "not a source")):
            payload = self.payload()
            payload["instrument"][field] = value
            self.assert_invalid(payload)

    def test_exact_object_schemas(self):
        for location in ("top", "instrument", "price"):
            payload = self.payload()
            target = payload if location == "top" else payload["instrument"] if location == "instrument" else payload["prices"][0]
            target["unknown"] = "extra"
            self.assert_invalid(payload)
            for key in list(target):
                if key == "unknown":
                    continue
                changed = copy.deepcopy(self.payload())
                target = changed if location == "top" else changed["instrument"] if location == "instrument" else changed["prices"][0]
                del target[key]
                self.assert_invalid(changed)
        for invalid in ([], None, "text", 1):
            self.assert_invalid(invalid)
            payload = self.payload()
            payload["instrument"] = invalid
            self.assert_invalid(payload)
            payload = self.payload()
            payload["prices"][0] = invalid
            self.assert_invalid(payload)

    def test_malformed_duplicate_nonfinite_and_nested_json(self):
        valid = json.dumps(self.payload())
        inputs = [b"\xff", b"{", b"[]" * 2, b"[" * 2000 + b"]" * 2000,
                  valid.replace('"instrument":', '"instrument": {}, "instrument":').encode(),
                  valid.replace('"symbol":', '"symbol": "other", "symbol":').encode(),
                  valid.replace('"price":', '"price": "9", "price":', 1).encode()]
        inputs.extend(valid.replace('"10.00"', constant, 1).encode()
                      for constant in ("NaN", "Infinity", "-Infinity", "1e9999"))
        for raw in inputs:
            with self.subTest(raw=raw[:60]):
                self.path.write_bytes(raw)
                with patch.object(engine, "finmap") as context:
                    with self.assertRaises(ValueError):
                        projection.project(self.conn, self.eid, self.path)
                context.assert_not_called()
                self.assertFalse(self.conn.in_transaction)

    def test_file_and_point_bounds(self):
        for prices in ([], [{"price": "1", "timestamp": self.start.isoformat()}], None, {}, "bad"):
            payload = self.payload()
            payload["prices"] = prices
            self.assert_invalid(payload)
        payload = self.payload(["1"] * 10_000)
        self.assertEqual(self.run_payload(payload)["evaluation"]["eligible_predictions"], 9999)
        payload["prices"].append(payload["prices"][-1])
        self.assert_invalid(payload)
        raw = json.dumps(self.payload()).encode()
        self.path.write_bytes(raw + b" " * (projection.MAX_FILE_BYTES - len(raw)))
        self.assertEqual(projection.project(self.conn, self.eid, self.path)["provenance"]["input_bytes"],
                         projection.MAX_FILE_BYTES)
        self.path.write_bytes(raw + b" " * (projection.MAX_FILE_BYTES + 1 - len(raw)))
        with patch.object(engine, "finmap") as context:
            with self.assertRaisesRegex(ValueError, "2 MiB"):
                projection.project(self.conn, self.eid, self.path)
        context.assert_not_called()

    def test_unknown_or_invalid_entity_ids(self):
        self.run_payload(self.payload())
        for eid in (0, -1, True, 1.0, "1", None, 2**63, self.eid + 1):
            with self.subTest(eid=eid), patch.object(engine, "finmap") as context:
                with self.assertRaises(ValueError):
                    projection.project(self.conn, eid, self.path)
                context.assert_not_called()

    def test_missing_file_propagates_oserror(self):
        with self.assertRaises(FileNotFoundError):
            projection.project(self.conn, self.eid, self.path)

    def test_context_read_only_no_network_or_transactions(self):
        baseline = self.run_payload(self.payload())
        registry.add_metric(self.conn, self.eid, "future_price", "999999", "2099", "fixture")
        self.conn.commit()
        self.conn.execute("PRAGMA query_only=ON")
        before = self.conn.total_changes
        statements = []
        self.conn.set_trace_callback(statements.append)
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            with patch.object(engine, "finmap", wraps=engine.finmap) as context:
                result = self.run_payload(self.payload())
        context.assert_called_once_with(self.conn, [self.eid])
        self.assertEqual(result["research"]["entity"], dict(registry.get_entity(self.conn, self.eid)))
        self.assertEqual(result["research"]["context"], engine.finmap(self.conn, [self.eid]))
        self.assertFalse(result["research"]["used_in_forecast"])
        self.assertIn("lookahead", result["research"]["note"])
        self.assertEqual(result["forecast"], baseline["forecast"])
        self.assertEqual(result["evaluation"], baseline["evaluation"])
        self.assertTrue(all(sql.lstrip().upper().startswith("SELECT") for sql in statements))
        self.assertEqual(before, self.conn.total_changes)
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(baseline["research"]["context"]["issuers"][0]["groups"], [])

    def test_existing_transaction_is_not_committed_or_rolled_back(self):
        registry.set_attr(self.conn, self.eid, "pending", "yes")
        self.assertTrue(self.conn.in_transaction)
        self.run_payload(self.payload())
        self.assertTrue(self.conn.in_transaction)
        self.assert_invalid(self.payload(("1", "NaN")))
        self.conn.rollback()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM attributes").fetchone()[0], 0)

    def test_context_metric_limit_propagates_after_validation(self):
        for index in range(1001):
            registry.add_metric(self.conn, self.eid, f"metric{index}", "1", "2024", "fixture")
        self.conn.commit()
        self.assert_invalid(self.payload(("1", "bad")))
        before = self.conn.total_changes
        with self.assertRaisesRegex(ValueError, "1000 metric row limit"):
            self.run_payload(self.payload())
        self.assertEqual(self.conn.total_changes, before)
        self.assertFalse(self.conn.in_transaction)


if __name__ == "__main__":
    unittest.main()
