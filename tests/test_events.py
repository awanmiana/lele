import copy
import csv
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from lele.analysis import events
from lele.cli.main import main
from lele.core import registry


class EventTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "prices.json"
        self.evidence = self.root / "evidence.json"
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(registry._SCHEMA)
        self.addCleanup(self.conn.close)
        self.eid = registry.upsert_entity(self.conn, "instrument", "Synthetic", key="fixture:asset")
        self.payload = {
            "instrument": {"entity_key": "fixture:asset", "symbol": "TEST", "asset_class": "bitcoin",
                           "venue": "synthetic", "currency": "USD", "unit": "coin",
                           "price_type": "last", "source_url": "local:synthetic"},
            "prices": [{"timestamp": "2024-01-04T00:00:00Z", "price": "100"},
                       {"timestamp": "2024-01-05T00:00:00Z", "price": "112"}],
        }
        self.path.write_text(json.dumps(self.payload))

    def record(self, **changes):
        return {"id": "flow", "entity_key": "fixture:asset", "kind": "fund_flow",
                "observed_at": "2024-01-03T12:00:00Z", "available_at": "2024-01-03T13:00:00Z",
                "source_url": "local:synthetic-evidence", "description": "Synthetic withdrawal",
                "platform": "Fixture venue", "industry": None, "location": None,
                "measurement": {"value": "-10", "currency": "USD", "unit": "currency", "basis": "observed"},
                **changes}

    def run_study(self, rows=None, **kwargs):
        self.path.write_text(json.dumps(self.payload))
        if rows is not None:
            self.evidence.write_text(json.dumps({"observations": rows}))
        return events.study(self.conn, self.eid, self.path,
                            self.evidence if rows is not None else None, **kwargs)

    def test_thresholds_strict_and_cumulative(self):
        for price, expected in (("103", []), ("103.000000001", [3]), ("105", [3]),
                                ("111", [3, 5, 7]), ("112", [3, 5, 7, 11]),
                                ("88", [3, 5, 7, 11])):
            with self.subTest(price=price):
                self.payload["prices"][1]["price"] = price
                report = self.run_study()
                self.assertEqual(report["total_matches"], bool(expected))
                if expected:
                    self.assertEqual(report["events"][0]["exceeded_thresholds"], expected)
                    self.assertEqual(report["events"][0]["direction"], "down" if price == "88" else "up")

    def test_precursors_and_late_publications(self):
        rows = [self.record(), self.record(id="late", available_at="2024-01-04T01:00:00Z"),
                self.record(id="during", observed_at="2024-01-04T00:00:00Z",
                            available_at="2024-01-04T00:00:00Z")]
        report = self.run_study(rows)
        self.assertEqual(report["late_publication_event_pairs"], 1)
        self.assertEqual(set(report["evidence"]), {"flow"})
        self.assertEqual(report["evidence"]["flow"]["measurement"]["value"], "-10")
        for window in report["events"][0]["precursor_windows"]:
            self.assertEqual(window["observation_ids"], ["flow"])
            self.assertEqual(window["flow_attribution"], "not_established")

    def test_window_boundaries(self):
        rows = [self.record(id=str(h), observed_at=t, available_at=t) for h, t in (
            (24, "2024-01-03T00:00:00Z"), (48, "2024-01-02T00:00:00Z"),
            (72, "2024-01-01T00:00:00Z"), (73, "2023-12-31T23:00:00Z"))]
        windows = self.run_study(rows)["events"][0]["precursor_windows"]
        for window, ids in zip(windows, ({"24"}, {"24", "48"}, {"24", "48", "72"})):
            self.assertEqual(set(window["observation_ids"]), ids)

    def test_liquidation_levels_require_estimate_and_no_capitulation_claim(self):
        row = self.record(kind="liquidation_level", measurement={
            "value": "90", "currency": "USD", "unit": "coin_price", "basis": "estimated"})
        report = self.run_study([row, self.record(id="cap", kind="capitulation_indicator")])
        window = report["events"][0]["precursor_windows"][0]
        self.assertEqual(window["liquidation_levels_status"], "estimates_only")
        self.assertEqual(window["capitulation_status"], "not_established")
        for changes in ({"basis": "observed"}, {"value": "0"}, {"value": "-5"}):
            bad = copy.deepcopy(row)
            bad["measurement"].update(changes)
            with self.assertRaises(ValueError):
                self.run_study([bad])

    def test_missing_evidence_unknown(self):
        report = self.run_study()
        self.assertEqual(report["evidence_coverage"], "unknown")
        self.assertEqual(report["evidence"], {})
        self.assertFalse(report["coverage"]["all_history"])
        self.assertTrue(all(w["liquidation_levels_status"] == "unknown"
                            for w in report["events"][0]["precursor_windows"]))

    def test_horizon_gaps_and_newest_limit(self):
        self.payload["prices"] += [{"timestamp": "2024-01-06T00:00:00Z", "price": "140"},
                                   {"timestamp": "2024-01-08T00:00:00Z", "price": "90"}]
        report = self.run_study(limit=1)
        self.assertEqual(report["total_matches"], 2)
        self.assertTrue(report["truncated"])
        self.assertEqual(report["events"][0]["end"], "2024-01-06T00:00:00+00:00")
        self.assertEqual(report["coverage"]["missing_start_points"], 1)
        self.assertEqual(self.run_study(move_hours=48)["coverage"]["eligible_intervals"], 2)

    def test_invalid_options_and_identity(self):
        for kwargs in ({"move_hours": 0}, {"move_hours": True}, {"move_hours": 73},
                       {"limit": 101}, {"limit": 0}, {"thresholds": []},
                       {"thresholds": [3, 3]}, {"thresholds": [True]}, {"thresholds": [1001]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.run_study(**kwargs)
        with self.assertRaises(ValueError):
            events.study(self.conn, 999, self.path)
        self.payload["instrument"]["entity_key"] = "wrong"
        with self.assertRaises(ValueError):
            self.run_study()

    def test_evidence_validation(self):
        for changes in ({"entity_key": "other"}, {"kind": "invented"},
                        {"source_url": "http://localhost"}, {"available_at": "2024-01-01T00:00:00Z"},
                        {"observed_at": "2024-01-03T00:00:00"}, {"measurement": {}}, {"extra": 1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.run_study([self.record(**changes)])
        with self.assertRaises(ValueError):
            self.run_study([self.record(), self.record()])
        for value in ("NaN", "Infinity", "1e9999", "--1", " 1", 1, True):
            row = self.record()
            row["measurement"]["value"] = value
            with self.assertRaises(ValueError):
                self.run_study([row])

    def test_evidence_size_duplicate_json_and_count(self):
        for raw in ('{"observations":[],"observations":[]}', " " * (2 * 1024 * 1024 + 1)):
            self.evidence.write_text(raw)
            with self.assertRaises(ValueError):
                events.study(self.conn, self.eid, self.path, self.evidence)
        with self.assertRaises(ValueError):
            self.run_study([self.record(id=str(i)) for i in range(1001)])

    def mapping_flow(self, **mapping):
        return self.record(measurement={"value": "10", "currency": "USD", "unit": "currency",
                                        "basis": "observed"}, mapping=mapping)

    def decision_row(self, **changes):
        row = {"id": "decision", "entity_key": "fixture:asset", "kind": "decision",
               "observed_at": "2024-01-03T12:30:00Z", "available_at": "2024-01-03T14:00:00Z",
               "source_url": "local:synthetic-decision", "description": "Published decision",
               "platform": None, "industry": None, "location": None, "measurement": None}
        row.update(changes)
        return row

    ROUTE = {"from": "Fixture venue", "to": "External desk", "actor": None, "action": None,
             "reason": None, "reason_basis": "unknown", "related_ids": ["decision"],
             "source_locator": None}
    DECISION_MAP = {"from": None, "to": None, "actor": "Fixture manager",
                    "action": "Withdraw inventory", "reason": "Risk reduction",
                    "reason_basis": "stated_reason", "related_ids": ["flow"],
                    "source_locator": "p. 1"}

    def test_routing_and_decision_mappings_round_trip(self):
        report = self.run_study([self.mapping_flow(**self.ROUTE),
                                 self.decision_row(mapping=self.DECISION_MAP)])
        flow_mapping = report["evidence"]["flow"]["mapping"]
        decision_mapping = report["evidence"]["decision"]["mapping"]
        self.assertEqual(flow_mapping["from"], "Fixture venue")
        self.assertEqual(flow_mapping["to"], "External desk")
        self.assertEqual(decision_mapping["reason"], "Risk reduction")
        self.assertEqual(decision_mapping["reason_basis"], "stated_reason")
        self.assertEqual(decision_mapping["source_locator"], "p. 1")
        for window in report["events"][0]["precursor_windows"]:
            self.assertEqual(window["observation_ids"], ["flow", "decision"])
        json.dumps(report, allow_nan=False)

    def test_mapping_validation(self):
        bad = ({"reason_basis": "invented"}, {"reason_basis": "unknown", "reason": "guess"},
               {"reason_basis": "stated_reason", "reason": None},
               {"reason": None, "reason_basis": None, "actor": "Someone"},
               {"from": "A", "to": "B", "reason_basis": "stated_reason",
                "reason": "guess", "actor": "X", "action": "buy"},
               {"from": "A", "to": None}, {"from": None, "to": "B"},
               {"from": "A", "to": "B", "reason": None, "reason_basis": "unknown",
                "related_ids": ["flow"], "source_locator": None})
        for changes in bad:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.run_study([self.mapping_flow(**{**self.ROUTE, **changes}),
                                self.decision_row(mapping=self.DECISION_MAP)])
        with self.assertRaises(ValueError):
            self.run_study([self.record(kind="decision", measurement=None,
                                        mapping={**self.ROUTE, "from": None, "to": None})])
        with self.assertRaises(ValueError):
            self.run_study([self.record(measurement={"value": "10", "currency": "USD",
                                                     "unit": "currency", "basis": "observed"},
                                        mapping=self.ROUTE)])
        with self.assertRaises(ValueError):
            self.run_study([self.mapping_flow(**{**self.ROUTE, "related_ids": ["missing"]}),
                            self.decision_row(mapping=self.DECISION_MAP)])

    def test_undirected_and_signed_rows_reject_routes(self):
        with self.assertRaises(ValueError):
            self.run_study([self.mapping_flow(**{**self.ROUTE, "from": None, "to": None})])
        negative = self.mapping_flow(**self.ROUTE)
        negative["measurement"]["value"] = "-10"
        with self.assertRaises(ValueError):
            self.run_study([negative, self.decision_row(mapping=self.DECISION_MAP)])

    def test_csv_mapping_round_trip_and_optional_blank(self):
        ordered = sorted(events.CSV_FIELDS)
        flow_row = {**dict.fromkeys(ordered, ""), "id": "flow", "entity_key": "fixture:asset",
                    "observed_at": "2024-01-03T12:00:00Z", "available_at": "2024-01-03T13:00:00Z",
                    "kind": "fund_flow", "source_url": "local:synthetic-evidence",
                    "description": "Directed transfer", "platform": "Fixture venue",
                    "value": "10", "unit": "currency", "currency": "USD", "basis": "observed",
                    "from": "Fixture venue", "to": "External desk",
                    "reason_basis": "unknown", "related_ids": "decision"}
        decision = {**dict.fromkeys(ordered, ""), "id": "decision", "entity_key": "fixture:asset",
                    "observed_at": "2024-01-03T12:30:00Z", "available_at": "2024-01-03T14:00:00Z",
                    "kind": "decision", "source_url": "local:synthetic-decision",
                    "description": "Published decision", "actor": "Fixture manager",
                    "action": "Withdraw inventory", "reason": "Risk reduction",
                    "reason_basis": "stated_reason", "related_ids": "flow",
                    "source_locator": "p. 1"}
        context = {**dict.fromkeys(ordered, ""), "id": "context", "entity_key": "fixture:asset",
                   "observed_at": "2024-01-03T15:00:00Z", "available_at": "2024-01-03T15:00:00Z",
                   "kind": "market_context", "source_url": "local:synthetic-context",
                   "description": "Plain observation", "value": "1", "unit": "index",
                   "basis": "observed"}
        self.csv = self.root / "evidence.csv"
        with self.csv.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=ordered)
            writer.writeheader()
            writer.writerows([flow_row, decision, context])
        report = events.study(self.conn, self.eid, self.path, self.csv)
        self.assertEqual(report["evidence"]["flow"]["mapping"]["to"], "External desk")
        self.assertEqual(report["evidence"]["decision"]["mapping"]["reason_basis"], "stated_reason")
        self.assertIsNone(report["evidence"]["context"]["mapping"])

    def test_csv_missing_columns_rejected(self):
        for header in ("id,entity_key,extra", *sorted(
                ",".join(sorted(events.CSV_FIELDS - {missing})) for missing in events.CSV_FIELDS)):
            self.evidence.write_text(header + "\nflow," + ",".join(
                "x" for _ in range(header.count(",") - 0)) + "\n")
            with self.subTest(header=header[:40]), self.assertRaises(ValueError):
                events.study(self.conn, self.eid, self.path, self.evidence)


    def test_read_only_and_no_network(self):
        self.conn.commit()
        self.conn.execute("PRAGMA query_only=ON")
        before = self.conn.total_changes
        raw = self.path.read_bytes()
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            report = events.study(self.conn, self.eid, self.path)
        self.assertEqual(self.conn.total_changes, before)
        self.assertEqual(self.path.read_bytes(), raw)
        json.dumps(report, allow_nan=False)

    def test_cli_end_to_end(self):
        db = self.root / "test.db"
        with registry.get_conn(str(db)) as conn:
            eid = registry.upsert_entity(conn, "instrument", "Synthetic", key="fixture:asset")
        self.evidence.write_text(json.dumps({"observations": [self.record()]}))
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            status = main(["--db", str(db), "--json", "events", str(eid), str(self.path),
                           "--evidence", str(self.evidence)])
        self.assertEqual((status, errors.getvalue()), (0, ""))
        result = json.loads(output.getvalue())
        self.assertEqual(result["events"][0]["exceeded_thresholds"], [3, 5, 7, 11])
        self.assertEqual(set(result["evidence"]), {"flow"})

    def test_cli_rejects_bad_horizon(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["events", "1", str(self.path), "--move-hours", "0"]), 2)
