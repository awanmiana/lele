import copy
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from lele.analysis import engine
from lele.cli.main import main
from lele.core import importer, registry
from lele.fetchers import sources
from test_fetchers import sec_submissions


CIK = "0000320193"
END = "2024-12-31"
RETRIEVED = "2025-02-01T00:00:00Z"
F03_TAGS = {
    "inventory_net": "InventoryNet",
    "long_term_debt_current": "LongTermDebtCurrent",
    "long_term_debt_noncurrent": "LongTermDebtNoncurrent",
    "short_term_borrowings": "ShortTermBorrowings",
    "net_cash_from_operating_activities": "NetCashProvidedByUsedInOperatingActivities",
    "net_cash_from_investing_activities": "NetCashProvidedByUsedInInvestingActivities",
    "net_cash_from_financing_activities": "NetCashProvidedByUsedInFinancingActivities",
}
F03_CASH_FLOWS = (
    "net_cash_from_operating_activities",
    "net_cash_from_investing_activities",
    "net_cash_from_financing_activities",
)


def fact(value, **changes):
    return {"val": value, "end": END, "filed": "2025-01-30",
            "accn": "0000320193-25-000001", "form": "10-K", **changes}


def facts():
    return {"cik": 320193, "facts": {"us-gaap": {
        tag: {"units": {"USD": [fact(value, **changes)]}}
        for tag, value, changes in (
            ("Assets", 100, {}), ("Liabilities", 60, {}),
            ("StockholdersEquity", 40, {}),
            ("NetIncomeLoss", 10, {"start": "2024-01-01"}),
            ("Revenues", 50, {"start": "2024-01-01"}),
            ("CashAndCashEquivalentsAtCarryingValue", 20, {}),
        )
    }}}


def f03_facts(second=False):
    payload = facts()
    for (metric, tag), value in zip(F03_TAGS.items(), (7, 11, 29, 3, 18, -9, 0)):
        changes = {"start": "2024-01-01"} if metric in F03_CASH_FLOWS else {}
        payload["facts"]["us-gaap"][tag] = {"units": {"USD": [fact(value, **changes)]}}
    if second:
        payload["cik"] = 202
        for item in payload["facts"]["us-gaap"].values():
            row = item["units"]["USD"][0]
            row.update(val=row["val"] * 2 + 1, end="2023-09-30", filed="2023-11-02",
                       accn="0000000202-23-000009", form="10-Q")
            if "start" in row:
                row["start"] = "2023-01-01"
    return payload


def f03_fixture(second=False):
    payload = f03_facts(second)
    cik = str(payload["cik"]).zfill(10)
    source_url = "local:synthetic-f03-companyfacts-" + cik
    retrieved = "2023-11-03T12:00:00Z" if second else RETRIEVED
    rows, _ = sources._sec_facts(payload, cik, source_url, retrieved)
    return {"key": "cik:" + cik, "kind": "company", "name": "F03 synthetic issuer " + cik,
            "attributes": {"source_url": source_url, "sec.cik": cik},
            "metrics": [{"k": key, "v": json.loads(value), "period": period, "source": "sec"}
                        for key, value, period in rows]}


def selected(payload=None):
    rows, _ = sources._sec_facts(payload or facts(), CIK, "local:companyfacts", RETRIEVED)
    return {key: json.loads(value) for key, value, _ in rows}


class SECProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(registry._SCHEMA)
        self.addCleanup(self.conn.close)
        self.eid = registry.upsert_entity(self.conn, "company", "Issuer", key="cik:" + CIK)

    def group(self, values=None, source="sec", period=END):
        if values is not None:
            self.conn.execute("DELETE FROM metrics")
            for key, value in values.items():
                registry.add_metric(self.conn, self.eid, key,
                                    json.dumps(value) if isinstance(value, dict) else value,
                                    period, source)
        return engine.analyze_entity(self.conn, self.eid)["fundamentals"]["groups"][0]

    def indicator(self, group, name):
        return next(item for item in group["indicators"] if item["name"] == name)

    def test_complete_evidence_and_end_balance_returns(self):
        values = selected()
        group = self.group(values)
        for name, expected in (("net_margin", 0.2), ("return_on_assets", 0.1),
                               ("return_on_equity", 0.25), ("liabilities_to_assets", 0.6)):
            item = self.indicator(group, name)
            self.assertEqual(item["value"], expected)
            for key, evidence in item["inputs"].items():
                self.assertEqual(evidence["observation"], values[key]["observation"])
            if name.startswith("return"):
                self.assertIn("end-balance", item["note"])
                self.assertIn("nonannualized", item["note"])
        self.assertEqual(group["warnings"], [])

    def test_quarter_ytd_and_accession_form_mismatch(self):
        for field, value in (("start", "2024-10-01"),
                             ("accession", "0000320193-25-000002"), ("form", "10-Q")):
            values = selected()
            values["net_income"]["observation"][field] = value
            group = self.group(values)
            self.assertIsNone(self.indicator(group, "net_margin")["value"])
            if field != "start":
                self.assertIsNone(self.indicator(group, "return_on_assets")["value"])
        values = selected()
        values["total_assets"]["observation"]["accession"] = "0000320193-25-000002"
        self.assertIsNone(self.indicator(self.group(values), "liabilities_to_assets")["value"])

    def test_unknown_provenance_retains_numbers_but_blocks(self):
        for key, field in (("net_income", "start"), ("net_income", "accession"),
                           ("net_income", "form"), ("total_assets", "accession")):
            values = selected()
            values[key]["observation"][field] = None
            importer.validate_metric_envelope(values[key], END)
            group = self.group(values)
            self.assertEqual(group["inputs"][key], values[key]["value"])
            self.assertIsNone(self.indicator(group, "return_on_assets")["value"])
            self.assertTrue(group["warnings"])

    def test_malformed_observation_never_legacy_fallback(self):
        for observation in (None, [], "sec_fact_v1", 1, True, {}, {"schema": "sec_fact_v1"}):
            for source in ("sec", "manual"):
                values = selected()
                values["total_assets"]["observation"] = observation
                group = self.group(values, source)
                self.assertIsNone(self.indicator(group, "liabilities_to_assets")["value"])
                self.assertIn("provenance", " ".join(group["warnings"]))

    def test_bad_dates_types_schema_and_units_block_analysis(self):
        changes = [("end", "2024-12-30"), ("start", "2025-01-01"),
                   ("start", "2024-02-30"), ("filed", "bad"), ("schema", "other"),
                   ("taxonomy", "ifrs"), ("period_type", "instant"),
                   ("accession", "bad"), ("form", []), ("form", "arbitrary"),
                   ("retrieved_at", "yesterday"), ("source_url", ""),
                   ("ambiguous", "false"), ("tag", "Assets")]
        for field, value in changes:
            with self.subTest(field=field, value=value):
                values = selected()
                values["net_income"]["observation"][field] = value
                self.assertIsNone(self.indicator(self.group(values), "net_margin")["value"])
        for field, value in (("currency", "EUR"), ("scale", 1000), ("unit", "shares")):
            values = selected()
            values["net_income"][field] = value
            self.assertIsNone(self.indicator(self.group(values), "net_margin")["value"])

    def test_legacy_sec_blocked_without_writes_manual_warns(self):
        values = {"net_income": 10, "revenue": 50, "total_assets": 100, "total_liabilities": 60}
        group = self.group(values)
        self.assertTrue(all(item["value"] is None for item in group["indicators"]))
        self.assertIn("refetch", " ".join(group["warnings"]))
        before = self.conn.total_changes
        engine.analyze_entity(self.conn, self.eid)
        self.assertEqual(before, self.conn.total_changes)
        self.assertEqual(registry.entity_payload(self.conn, self.eid)["metrics"][0]["v"], "10")
        group = self.group(values, "manual")
        self.assertEqual(self.indicator(group, "net_margin")["value"], 0.2)
        self.assertIn("scope/duration not verified", " ".join(group["warnings"]))
        self.assertIn("assumes", " ".join(group["warnings"]))

    def test_invalid_source_dates_accessions_forms_skipped(self):
        for change in ({"start": "2025-01-01"}, {"start": "2024-02-30"},
                       {"end": "bad"}, {"filed": None}, {"accn": "bad"},
                       {"form": "invented"}, {"form": 10}, {"val": float("nan")}):
            payload = facts()
            payload["facts"]["us-gaap"]["NetIncomeLoss"]["units"]["USD"] = [
                fact(99, **{"start": "2024-01-01", **change})]
            self.assertNotIn("net_income", selected(payload))

    def test_missing_source_fields_are_null_not_invented(self):
        payload = facts()
        row = payload["facts"]["us-gaap"]["NetIncomeLoss"]["units"]["USD"][0]
        for key in ("accn", "form", "start"):
            del row[key]
        values = selected(payload)
        observation = values["net_income"]["observation"]
        self.assertTrue(all(observation[key] is None for key in ("accession", "form", "start")))
        self.assertIsNone(self.indicator(self.group(values), "net_margin")["value"])

    def test_instant_start_marked_invalid_not_flow(self):
        payload = facts()
        payload["facts"]["us-gaap"]["Assets"]["units"]["USD"][0]["start"] = "2024-01-01"
        values = selected(payload)
        observation = values["total_assets"]["observation"]
        self.assertEqual(observation["period_type"], "instant")
        self.assertTrue(observation["invalid"])
        self.assertEqual(observation["start"], "2024-01-01")
        self.assertIsNone(self.indicator(self.group(values), "return_on_assets")["value"])

    def test_conflicting_tie_deterministic_and_blocked(self):
        payload = facts()
        rows = payload["facts"]["us-gaap"]["NetIncomeLoss"]["units"]["USD"]
        rows.append(dict(rows[0], val=11, frame="CY2024"))
        first = selected(payload)
        rows.reverse()
        self.assertEqual(first, selected(payload))
        self.assertTrue(first["net_income"]["observation"]["ambiguous"])
        group = self.group(first)
        self.assertIsNone(self.indicator(group, "net_margin")["value"])
        self.assertIn("ambiguous", " ".join(group["warnings"]))
        rows[0]["val"] = rows[1]["val"]
        self.assertNotIn("ambiguous", selected(payload)["net_income"]["observation"])

    def test_selection_freshness_duration_tag_priority_non_usd(self):
        payload = facts()
        gaap = payload["facts"]["us-gaap"]
        gaap["Revenues"]["units"]["USD"] += [
            fact(500, start="2024-10-01"),
            fact(999, start="2023-01-01", end="2023-12-31", filed="2026-01-01")]
        gaap["Revenues"]["units"]["EUR"] = [fact(1000, end="2026-01-01")]
        gaap["RevenueFromContractWithCustomerExcludingAssessedTax"] = {
            "units": {"USD": [fact(60, start="2024-01-01")]}}
        self.assertEqual(selected(payload)["revenue"]["value"], 50)
        gaap["Revenues"]["units"]["USD"].append(fact(70, start="2024-01-01", filed="2025-02-01"))
        self.assertEqual(selected(payload)["revenue"]["value"], 70)

    def test_refresh_stable_count_old_numeric_rows_untouched(self):
        registry.add_metric(self.conn, self.eid, "total_assets", 80, "2023-12-31", "sec")
        payload = facts()
        rows = payload["facts"]["us-gaap"]["NetIncomeLoss"]["units"]["USD"]
        rows.append(dict(rows[0], val=11))
        client = Mock(warnings=[], retrieved_at=RETRIEVED)
        with patch.object(sources, "HTTPClient", return_value=client):
            for _ in range(2):
                client.get_json.side_effect = [sec_submissions(), payload]
                result = sources.fetch_source(self.conn, "sec", query=CIK, financials=True)
                self.assertEqual(registry.stats(self.conn)["metrics"], 7)
                self.assertIn("not all-history", " ".join(result["warnings"]))
                self.assertIn("ambiguous", " ".join(result["warnings"]))
        old = self.conn.execute("SELECT v FROM metrics WHERE period='2023-12-31'").fetchone()[0]
        self.assertEqual(old, "80")

    def import_payload(self, values):
        return {"entities": [{"key": "cik:" + CIK, "kind": "company", "name": "Issuer",
                              "attributes": {"source_url": "local:companyfacts"},
                              "metrics": [{"k": key, "v": value, "source": "sec", "period": END}
                                          for key, value in values.items()]}]}

    def test_import_sqlite_payload_show_analyze_round_trip(self):
        values = selected()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "import.json"
            db = str(Path(directory) / "registry.db")
            path.write_text(json.dumps(self.import_payload(values)))
            with registry.get_conn(db) as conn:
                importer.import_json(conn, str(path))
                importer.import_json(conn, str(path))
                self.assertEqual(registry.stats(conn)["metrics"], 6)
                for row in registry.entity_payload(conn, 1)["metrics"]:
                    self.assertEqual(json.loads(row["v"]), values[row["k"]])
                    self.assertEqual(row["v"], importer.serialize_metric_envelope(values[row["k"]]))
                report = engine.analyze_entity(conn, 1)
                self.assertEqual(self.indicator(report["fundamentals"]["groups"][0], "net_margin")["value"], 0.2)
                self.assertEqual(report["provenance"]["metrics"], sorted(
                    registry.entity_payload(conn, 1)["metrics"], key=lambda row: row["k"]))
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--db", db, "--json", "show", "1"]), 0)
            shown = json.loads(output.getvalue())
            self.assertEqual(json.loads(shown["metrics"][0]["v"]), values[shown["metrics"][0]["k"]])
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--db", db, "--json", "analyze", "1"]), 0)
            analyzed = json.loads(output.getvalue())
            margin = self.indicator(analyzed["fundamentals"]["groups"][0], "net_margin")
            self.assertEqual(margin["value"], 0.2)
            self.assertEqual(margin["inputs"]["net_income"]["observation"],
                             values["net_income"]["observation"])

    def test_import_validation_before_write_and_allowlist(self):
        changes = [lambda v: v.update(extra=1), lambda v: v.pop("currency"),
                   lambda v: v.update(value=float("nan")), lambda v: v.update(value=True),
                   lambda v: v.update(scale=True), lambda v: v.update(observation=[]),
                   lambda v: v["observation"].update(extra=1),
                   lambda v: v["observation"].update(source_url=""),
                   lambda v: v["observation"].update(retrieved_at=""),
                   lambda v: v["observation"].update(selection="x" * 1025),
                   lambda v: v["observation"].update(accession="bad"),
                   lambda v: v["observation"].update(start="2025-01-01"),
                   lambda v: v["observation"].update(end="2024-12-30")]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "import.json"
            for change in changes:
                values = selected()
                change(values["net_income"])
                path.write_text(json.dumps(self.import_payload(values)))
                before = self.conn.total_changes
                with self.assertRaises(ValueError):
                    importer.import_json(self.conn, str(path))
                self.assertEqual(before, self.conn.total_changes)

    def test_import_unknown_start_and_atomic_savepoint(self):
        values = selected()
        values["net_income"]["observation"]["start"] = None
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "import.json"
            path.write_text(json.dumps(self.import_payload(values)))
            with patch.object(importer, "add_metric", side_effect=sqlite3.IntegrityError("forced")):
                with self.assertRaises(sqlite3.IntegrityError):
                    importer.import_json(self.conn, str(path))
            self.assertTrue(self.conn.in_transaction)
            self.assertEqual(registry.stats(self.conn)["metrics"], 0)
            importer.import_json(self.conn, str(path))
            self.assertIsNone(self.indicator(self.group(), "net_margin")["value"])

    def test_alias_provenance_conflicts_and_partial_generic_evidence(self):
        values = selected()
        values["assets"] = copy.deepcopy(values["total_assets"])
        values["assets"]["observation"]["accession"] = "0000320193-25-000002"
        self.assertIsNone(self.indicator(self.group(values), "return_on_assets")["value"])
        values = selected()
        values["total_assets"].pop("observation")
        self.assertIsNone(self.indicator(self.group(values, "manual"), "return_on_assets")["value"])


def finmap_fixture(second=False):
    payload = facts()
    cik, end, start, filed, accession, retrieved, values, name = (
        ("0000000202", "2023-09-30", "2023-01-01", "2023-11-02",
         "0000000202-23-000009", "2023-11-03T12:00:00Z",
         (713, 281, 432, -17, 193, 89), "Second issuer (synthetic fixture)")
        if second else
        ("0000000101", "2024-06-30", "2024-01-01", "2024-08-02",
         "0000000101-24-000007", "2024-08-03T12:00:00Z",
         (347, 129, 218, 23, 157, 61), "Apple (synthetic fixture)")
    )
    payload["cik"] = int(cik)
    for tag, value in zip(("Assets", "Liabilities", "StockholdersEquity", "NetIncomeLoss",
                           "Revenues", "CashAndCashEquivalentsAtCarryingValue"), values):
        row = payload["facts"]["us-gaap"][tag]["units"]["USD"][0]
        row.update(val=value, end=end, filed=filed, accn=accession, form="10-Q")
        if "start" in row:
            row["start"] = start
    source_url = "local:synthetic-companyfacts-" + cik
    rows, _ = sources._sec_facts(payload, cik, source_url, retrieved)
    return {"key": "cik:" + cik, "kind": "company", "name": name,
            "attributes": {"source_url": source_url, "sec.cik": cik},
            "metrics": [{"k": key, "v": json.loads(value), "period": period, "source": "sec"}
                        for key, value, period in rows]}


class FinmapTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(registry._SCHEMA)
        self.addCleanup(self.conn.close)
        self.eid = registry.upsert_entity(self.conn, "company", "Apple (synthetic fixture)",
                                          key="cik:0000000101")
        network = patch("socket.create_connection", side_effect=AssertionError("offline test"))
        self.addCleanup(network.stop)
        network.start()
        client = patch.object(sources, "HTTPClient", side_effect=AssertionError("offline test"))
        self.addCleanup(client.stop)
        client.start()

    def store(self, metrics=None, eid=None):
        eid = self.eid if eid is None else eid
        metrics = finmap_fixture()["metrics"] if metrics is None else metrics
        self.conn.execute("DELETE FROM metrics WHERE entity_id=?", (eid,))
        for row in metrics:
            value = row["v"]
            registry.add_metric(self.conn, eid, row["k"],
                                json.dumps(value) if isinstance(value, dict) else value,
                                row["period"], row["source"])

    def group(self):
        report = engine.finmap(self.conn, [self.eid])
        self.assert_report(report, [self.eid])
        self.assertEqual(len(report["issuers"][0]["groups"]), 1)
        return report["issuers"][0]["groups"][0]

    def position(self, group, metric):
        return next(item for item in group["positions"] if item["metric"] == metric)

    def assert_unknown(self, item):
        self.assertEqual(item["status"], "unknown")
        self.assertIsNone(item["value"])
        self.assertIsInstance(item["reason"], str)
        self.assertTrue(item["reason"].strip())

    def assert_alignment(self, group, kind, status):
        alignment = group["alignment"][kind]
        self.assertEqual(alignment["status"], status)
        self.assertIn("reason", alignment)
        if status == "unknown":
            self.assertIsInstance(alignment["reason"], str)
            self.assertTrue(alignment["reason"].strip())

    def assert_report(self, report, ids):
        self.assertIsInstance(report, dict)
        self.assertEqual(report["method"], "stored_financial_positions_v1")
        self.assertIsInstance(report["issuers"], list)
        self.assertEqual([issuer["entity"]["id"] for issuer in report["issuers"]], ids)
        self.assertIsInstance(report["limitations"], list)
        self.assertTrue(report["limitations"])
        forbidden = {"totals", "total", "transfers", "edges", "aggregate", "aggregates"}
        self.assertFalse(forbidden & report.keys())
        self.assertEqual(len(engine.SUPPORTED_METRICS), 14)
        self.assertIsInstance(engine.FLOW_METRICS, frozenset)
        self.assertEqual(engine.FLOW_METRICS, frozenset(("net_income", "revenue", *F03_CASH_FLOWS)))
        for issuer in report["issuers"]:
            self.assertIsInstance(issuer["entity"], dict)
            self.assertIsInstance(issuer["warnings"], list)
            self.assertIsInstance(issuer["groups"], list)
            self.assertFalse(forbidden & issuer.keys())
            for group in issuer["groups"]:
                self.assertFalse(forbidden & group.keys())
                self.assertIsInstance(group["period"], str)
                self.assertIsInstance(group["source"], str)
                self.assertIsInstance(group["warnings"], list)
                self.assertIsInstance(group["raw_metrics"], list)
                self.assertIsInstance(group["positions"], list)
                self.assertCountEqual([p["metric"] for p in group["positions"]],
                                      engine.SUPPORTED_METRICS)
                self.assertEqual(set(group["alignment"]), {"stocks", "flows", "cash_flows"})
                for kind in ("stocks", "flows", "cash_flows"):
                    self.assertIn(group["alignment"][kind]["status"], ("ok", "unknown"))
                    self.assertIn("reason", group["alignment"][kind])
                for position in group["positions"]:
                    self.assertTrue({"metric", "kind", "value", "status", "reason", "currency",
                                     "unit", "scale", "observation", "flow_type", "scope"} <= position.keys())
                    metric = position["metric"]
                    flow_type = ("cash_flow" if metric in F03_CASH_FLOWS else
                                 "accounting_flow" if metric in ("revenue", "net_income") else None)
                    self.assertEqual(position["kind"], "flow" if flow_type else "stock")
                    self.assertEqual(position["flow_type"], flow_type)
                    self.assertIsInstance(position["scope"], str)
                    self.assertTrue(position["scope"].strip())
                    self.assertIn(position["status"], ("ok", "unknown"))
                    self.assertTrue(position["observation"] is None or
                                    isinstance(position["observation"], dict))
                    if position["status"] == "unknown":
                        self.assert_unknown(position)
                    else:
                        self.assertIsInstance(position["value"], (int, float))
                        self.assertNotIsInstance(position["value"], bool)
                        self.assertIsInstance(position["observation"], dict)
        json.dumps(report, allow_nan=False)

    def assert_raw(self, group, rows):
        self.assertCountEqual(
            [{key: row[key] for key in ("k", "v", "period", "source")}
             for row in group["raw_metrics"]], rows)
        self.assertTrue(all(isinstance(row["v"], str) for row in group["raw_metrics"]))

    def cli(self, args):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            status = main(args)
        return status, output.getvalue(), errors.getvalue()

    def test_two_independent_synthetic_fixtures_import_sqlite_cli_round_trip(self):
        fixtures = [finmap_fixture(), finmap_fixture(second=True)]
        self.assertNotEqual(fixtures[0]["key"], fixtures[1]["key"])
        for first, second in zip(fixtures[0]["metrics"], fixtures[1]["metrics"]):
            self.assertNotEqual(first["v"]["value"], second["v"]["value"])
            for field in ("accession", "start", "end", "filed", "retrieved_at", "source_url"):
                if field != "start" or first["k"] in ("net_income", "revenue"):
                    self.assertNotEqual(first["v"]["observation"][field],
                                        second["v"]["observation"][field])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic-issuers.json"
            db = str(Path(directory) / "registry.db")
            path.write_text(json.dumps({"entities": fixtures}), encoding="utf-8")
            with registry.get_conn(db) as conn:
                imported = importer.import_json(conn, str(path))
                self.assertEqual(imported["metrics"], 12)
                ids = [conn.execute("SELECT id FROM entities WHERE key=?", (f["key"],)).fetchone()[0]
                       for f in fixtures]
            with registry.get_conn(db) as conn:
                before, changes = list(conn.iterdump()), conn.total_changes
                report = engine.finmap(conn, ids[::-1])
                self.assert_report(report, ids[::-1])
                self.assertEqual(conn.total_changes, changes)
                self.assertEqual(list(conn.iterdump()), before)
                for issuer, fixture in zip(report["issuers"], fixtures[::-1]):
                    self.assertEqual(issuer["entity"]["name"], fixture["name"])
                    self.assertEqual(issuer["entity"]["key"], fixture["key"])
                    self.assertEqual(len(issuer["groups"]), 1)
                    group = issuer["groups"][0]
                    self.assertEqual((group["period"], group["source"]),
                                     (fixture["metrics"][0]["period"], "sec"))
                    for row in fixture["metrics"]:
                        metric = "cash_and_equivalents" if row["k"] == "cash" else row["k"]
                        position = self.position(group, metric)
                        self.assertEqual(position["status"], "ok")
                        self.assertEqual(position["value"], row["v"]["value"])
                        self.assertEqual(position["observation"], row["v"]["observation"])
                        self.assertEqual(position["currency"], "USD")
                        self.assertEqual(position["unit"], "currency")
                        self.assertEqual(float(position["scale"]), 1)
                    self.assert_unknown(self.position(group, "total_debt"))
                    self.assert_alignment(group, "stocks", "unknown")
                    self.assert_alignment(group, "flows", "ok")
                    self.assert_raw(group, [{**row, "v": importer.serialize_metric_envelope(row["v"])}
                                            for row in fixture["metrics"]])
                persisted = list(conn.iterdump())
            for flags in (["--json"], []):
                status, output, errors = self.cli(["--db", db, *flags, "finmap", *map(str, ids[::-1])])
                self.assertEqual(status, 0, errors)
                self.assertEqual(errors, "")
                self.assertEqual(json.loads(output), report)
                if not flags:
                    self.assertEqual(output, json.dumps(report, ensure_ascii=True, allow_nan=False,
                                                       indent=2) + "\n")
            with closing(sqlite3.connect(db)) as conn:
                self.assertEqual(list(conn.iterdump()), persisted)

    def test_api_is_read_only_and_repeatable(self):
        self.store()
        self.conn.commit()
        before, changes = list(self.conn.iterdump()), self.conn.total_changes
        self.conn.execute("PRAGMA query_only=ON")
        first = engine.finmap(self.conn, [self.eid])
        self.assert_report(first, [self.eid])
        self.assertEqual(engine.finmap(self.conn, [self.eid]), first)
        self.assertEqual(self.conn.total_changes, changes)
        self.assertEqual(list(self.conn.iterdump()), before)

    def test_empty_entity_has_no_invented_periods(self):
        report = engine.finmap(self.conn, [self.eid])
        self.assert_report(report, [self.eid])
        issuer = report["issuers"][0]
        self.assertEqual(issuer["groups"], [])
        self.assertTrue(issuer["warnings"])

    def test_missing_positions_are_unknown_and_not_zero(self):
        metrics = finmap_fixture()["metrics"]
        self.store([row for row in metrics if row["k"] == "total_assets"])
        group = self.group()
        self.assertEqual(self.position(group, "total_assets")["value"], 347)
        for metric in set(engine.SUPPORTED_METRICS) - {"total_assets"}:
            position = self.position(group, metric)
            self.assert_unknown(position)
            self.assertIsNone(position["observation"])
        for kind in ("stocks", "flows"):
            self.assert_alignment(group, kind, "unknown")

    def test_zero_and_negative_valid_flows_are_not_missing(self):
        metrics = finmap_fixture()["metrics"]
        for row in metrics:
            if row["k"] in ("revenue", "net_income"):
                row["v"]["value"] = 0 if row["k"] == "revenue" else -23
        self.store(metrics)
        group = self.group()
        for metric, value in (("revenue", 0), ("net_income", -23)):
            self.assertEqual(self.position(group, metric)["value"], value)
            self.assertEqual(self.position(group, metric)["status"], "ok")
        self.assert_alignment(group, "flows", "ok")

    def test_legacy_sec_and_manual_inputs_never_gain_provenance(self):
        for source in ("sec", "manual"):
            for raw in ("23", json.dumps({"value": 23, "currency": "USD", "unit": "currency", "scale": 1})):
                with self.subTest(source=source, raw=raw):
                    metrics = finmap_fixture()["metrics"]
                    for row in metrics:
                        row["source"] = source
                        if row["k"] == "net_income":
                            row["v"] = raw
                    self.store(metrics)
                    group = self.group()
                    position = self.position(group, "net_income")
                    self.assert_unknown(position)
                    self.assertIsNone(position["observation"])
                    self.assert_alignment(group, "flows", "unknown")
                    self.assertTrue(group["warnings"])
                    self.assert_raw(group, registry.entity_payload(self.conn, self.eid)["metrics"])

    def test_malformed_observations_and_json_preserve_raw_inputs(self):
        for observation in (None, [], "sec_fact_v1", 1, True, {}, {"schema": "sec_fact_v1"}):
            for source in ("sec", "manual"):
                with self.subTest(observation=observation, source=source):
                    metrics = finmap_fixture()["metrics"]
                    for row in metrics:
                        row["source"] = source
                        if row["k"] == "net_income":
                            row["v"]["observation"] = observation
                    self.store(metrics)
                    group = self.group()
                    self.assert_unknown(self.position(group, "net_income"))
                    self.assert_alignment(group, "flows", "unknown")
                    self.assert_raw(group, registry.entity_payload(self.conn, self.eid)["metrics"])
        for raw in ('{"value":23,', '{"value":23,"value":24}', '[]', 'null', 'NaN', 'Infinity'):
            with self.subTest(raw=raw):
                self.store([{"k": "net_income", "v": raw, "period": "2024-06-30", "source": "sec"}])
                group = self.group()
                self.assert_unknown(self.position(group, "net_income"))
                self.assert_raw(group, registry.entity_payload(self.conn, self.eid)["metrics"])

    def test_invalid_or_missing_observation_fields_block_values(self):
        changes = (("accession", None), ("form", None), ("start", None),
                   ("accession", "bad"), ("form", "invented"), ("schema", "other"),
                   ("end", "2024-06-29"), ("start", "2024-07-01"), ("filed", "bad"),
                   ("period_type", "instant"), ("taxonomy", "other"), ("tag", "Assets"),
                   ("retrieved_at", "yesterday"), ("source_url", ""),
                   ("ambiguous", True), ("ambiguous", "false"), ("invalid", True))
        for field, value in changes:
            with self.subTest(field=field, value=value):
                metrics = finmap_fixture()["metrics"]
                next(row for row in metrics if row["k"] == "net_income")["v"]["observation"][field] = value
                self.store(metrics)
                group = self.group()
                self.assert_unknown(self.position(group, "net_income"))
                self.assert_alignment(group, "flows", "unknown")
                self.assertEqual(self.position(group, "revenue")["status"], "ok")
                self.assert_raw(group, registry.entity_payload(self.conn, self.eid)["metrics"])

    def test_invalid_envelope_units_scale_and_values_block_positions(self):
        for field, value in (("currency", "EUR"), ("unit", "shares"), ("scale", 1000),
                             ("scale", True), ("value", True), ("value", float("nan")),
                             ("value", float("inf")), ("value", "23")):
            with self.subTest(field=field, value=value):
                metrics = finmap_fixture()["metrics"]
                next(row for row in metrics if row["k"] == "net_income")["v"][field] = value
                self.store(metrics)
                group = self.group()
                self.assert_unknown(self.position(group, "net_income"))
                self.assert_alignment(group, "flows", "unknown")
                self.assert_raw(group, registry.entity_payload(self.conn, self.eid)["metrics"])

    def test_ambiguous_source_selection_blocks_only_affected_position(self):
        payload = facts()
        rows = payload["facts"]["us-gaap"]["NetIncomeLoss"]["units"]["USD"]
        rows.append(dict(rows[0], val=11))
        values = selected(payload)
        self.assertTrue(values["net_income"]["observation"]["ambiguous"])
        self.store([{"k": key, "v": value, "source": "sec", "period": END}
                    for key, value in values.items()])
        group = self.group()
        position = self.position(group, "net_income")
        self.assert_unknown(position)
        self.assertEqual(position["observation"], values["net_income"]["observation"])
        self.assertEqual(self.position(group, "revenue")["value"], 50)
        self.assert_alignment(group, "flows", "unknown")
        self.assertTrue(group["warnings"])
        self.assert_raw(group, registry.entity_payload(self.conn, self.eid)["metrics"])

    def test_conflicting_alias_values_or_evidence_are_unknown(self):
        for field in ("value", "accession"):
            for metric, alias in (("total_assets", "assets"), ("net_income", "net_profit")):
                with self.subTest(field=field, metric=metric):
                    metrics = finmap_fixture()["metrics"]
                    row = copy.deepcopy(next(row for row in metrics if row["k"] == metric))
                    row["k"] = alias
                    if field == "value":
                        row["v"]["value"] += 1
                    else:
                        row["v"]["observation"]["accession"] = "0000000101-24-000008"
                    metrics.append(row)
                    self.store(metrics)
                    first = self.group()
                    self.assert_unknown(self.position(first, metric))
                    self.assert_alignment(first, "flows" if metric == "net_income" else "stocks", "unknown")
                    self.assertTrue(first["warnings"])
                    self.assert_raw(first, registry.entity_payload(self.conn, self.eid)["metrics"])
                    self.store(metrics[::-1])
                    self.assertEqual(self.group(), first)

    def test_identical_aliases_do_not_duplicate_or_conflict(self):
        metrics = finmap_fixture()["metrics"]
        alias = copy.deepcopy(next(row for row in metrics if row["k"] == "net_income"))
        alias["k"] = "net_profit"
        metrics.append(alias)
        self.store(metrics)
        group = self.group()
        self.assertEqual(self.position(group, "net_income")["status"], "ok")
        self.assertEqual(self.position(group, "net_income")["value"], 23)
        self.assert_alignment(group, "flows", "ok")
        self.assert_raw(group, registry.entity_payload(self.conn, self.eid)["metrics"])

    def test_quarter_ytd_accession_and_form_mismatch_do_not_align_flows(self):
        for field, value in (("start", "2024-04-01"), ("accession", "0000000101-24-000008"),
                             ("form", "10-K")):
            with self.subTest(field=field):
                metrics = finmap_fixture()["metrics"]
                next(row for row in metrics if row["k"] == "net_income")["v"]["observation"][field] = value
                self.store(metrics)
                group = self.group()
                self.assert_alignment(group, "flows", "unknown")
                for metric, expected in (("net_income", 23), ("revenue", 157)):
                    position = self.position(group, metric)
                    self.assertEqual(position["status"], "ok")
                    self.assertEqual(position["value"], expected)
                self.assertEqual(self.position(group, "net_income")["observation"][field], value)
                self.assert_raw(group, registry.entity_payload(self.conn, self.eid)["metrics"])

    def test_missing_either_flow_prevents_alignment(self):
        for missing in ("revenue", "net_income"):
            with self.subTest(missing=missing):
                self.store([row for row in finmap_fixture()["metrics"] if row["k"] != missing])
                group = self.group()
                self.assert_unknown(self.position(group, missing))
                self.assert_alignment(group, "flows", "unknown")

    def test_alignment_error_is_not_ignored(self):
        self.store()
        with patch.object(engine, "_alignment_error", return_value="synthetic alignment mismatch") as alignment:
            group = self.group()
        alignment.assert_called()
        self.assert_alignment(group, "flows", "unknown")

    def test_stock_and_flow_scopes_are_checked_separately(self):
        metrics = finmap_fixture()["metrics"]
        for row in metrics:
            if row["k"] not in ("net_income", "revenue"):
                row["v"]["observation"]["accession"] = "0000000101-24-000008"
                row["v"]["observation"]["form"] = "10-K"
        self.store(metrics)
        group = self.group()
        self.assert_alignment(group, "flows", "ok")
        self.assert_alignment(group, "stocks", "unknown")
        present = {"cash_and_equivalents" if row["k"] == "cash" else row["k"] for row in metrics}
        for position in group["positions"]:
            if position["metric"] in present:
                self.assertEqual(position["status"], "ok")
            else:
                self.assert_unknown(position)
        self.assert_alignment(group, "cash_flows", "unknown")

    def test_debt_cannot_borrow_a_supported_sec_tag(self):
        metrics = finmap_fixture()["metrics"]
        debt = copy.deepcopy(next(row for row in metrics if row["k"] == "total_liabilities"))
        debt["k"] = "total_debt"
        metrics.append(debt)
        self.store(metrics)
        group = self.group()
        self.assert_unknown(self.position(group, "total_debt"))
        self.assertEqual(self.position(group, "total_liabilities")["value"], 129)
        self.assert_alignment(group, "stocks", "unknown")
        self.assert_raw(group, registry.entity_payload(self.conn, self.eid)["metrics"])

    def test_period_end_and_source_groups_never_merge(self):
        metrics = finmap_fixture()["metrics"]
        for row in metrics:
            if row["k"] == "net_income":
                row["period"] = "2024-03-31"
                row["v"]["observation"]["end"] = "2024-03-31"
            if row["k"] in ("total_assets", "total_equity"):
                row["source"] = "manual"
        self.store(metrics)
        report = engine.finmap(self.conn, [self.eid])
        self.assert_report(report, [self.eid])
        groups = {(g["period"], g["source"]): g for g in report["issuers"][0]["groups"]}
        self.assertEqual(len(report["issuers"][0]["groups"]), 3)
        self.assertEqual(set(groups), {("2024-03-31", "sec"), ("2024-06-30", "sec"),
                                       ("2024-06-30", "manual")})
        stored = registry.entity_payload(self.conn, self.eid)["metrics"]
        for identity, group in groups.items():
            rows = [row for row in stored if (row["period"], row["source"]) == identity]
            self.assert_raw(group, rows)
            present = {engine.METRIC_ALIASES[row["k"]] for row in rows}
            for metric in set(engine.SUPPORTED_METRICS) - present:
                self.assert_unknown(self.position(group, metric))
            for metric in present:
                self.assertEqual(self.position(group, metric)["status"], "ok")
            self.assert_alignment(group, "flows", "unknown")
            self.assert_alignment(group, "stocks", "unknown")
        self.assertEqual(self.position(groups[("2024-03-31", "sec")], "net_income")["value"], 23)
        self.assert_unknown(self.position(groups[("2024-06-30", "sec")], "net_income"))

    def test_api_rejects_invalid_entity_lists_without_writes(self):
        invalid = (None, 1, "1", {}, {1}, [], [self.eid, self.eid], list(range(1, 12)),
                   [0], [-1], [True], [False], [1.0], ["1"], [None], [[]], [{}],
                   [2**63], [self.eid, 0], [self.eid, True])
        before, changes = list(self.conn.iterdump()), self.conn.total_changes
        for ids in invalid:
            with self.subTest(ids=ids):
                with self.assertRaises(ValueError):
                    engine.finmap(self.conn, ids)
                self.assertEqual(self.conn.total_changes, changes)
                self.assertEqual(list(self.conn.iterdump()), before)

    def test_api_missing_entity_raises_without_partial_result(self):
        for ids in ([999], [self.eid, 999], [999, self.eid]):
            with self.subTest(ids=ids):
                before = self.conn.total_changes
                with self.assertRaises(ValueError):
                    engine.finmap(self.conn, ids)
                self.assertEqual(self.conn.total_changes, before)

    def test_one_ten_and_maximum_sqlite_ids_are_accepted(self):
        ids = [self.eid] + [registry.upsert_entity(self.conn, "company", f"Synthetic {index}")
                            for index in range(9)]
        for requested in ([self.eid], ids[::-1]):
            self.assert_report(engine.finmap(self.conn, requested), requested)
        maximum = 2**63 - 1
        self.conn.execute("INSERT INTO entities(id, key, kind, name) VALUES(?, ?, ?, ?)",
                          (maximum, "synthetic:max", "company", "Maximum ID synthetic fixture"))
        self.assert_report(engine.finmap(self.conn, [maximum]), [maximum])

    def test_metric_row_bound_is_per_issuer_and_counts_unsupported_rows(self):
        other = registry.upsert_entity(self.conn, "company", "Second issuer (synthetic fixture)")
        metrics = [{"k": f"unsupported_{index:04d}", "v": str(index),
                    "period": "2024-06-30", "source": "manual"} for index in range(1000)]
        for eid in (self.eid, other):
            self.store(metrics, eid=eid)
        report = engine.finmap(self.conn, [other, self.eid])
        self.assert_report(report, [other, self.eid])
        for issuer in report["issuers"]:
            self.assertEqual(sum(len(group["raw_metrics"]) for group in issuer["groups"]), 1000)
            self.assert_raw(issuer["groups"][0], metrics)
            for position in issuer["groups"][0]["positions"]:
                self.assert_unknown(position)
        registry.add_metric(self.conn, other, "one_too_many", "1", "2023-12-31", "sec")
        self.assert_report(engine.finmap(self.conn, [self.eid]), [self.eid])
        for requested in ([other], [self.eid, other], [other, self.eid]):
            with self.subTest(requested=requested):
                before = self.conn.total_changes
                with self.assertRaises(ValueError):
                    engine.finmap(self.conn, requested)
                self.assertEqual(self.conn.total_changes, before)

    def test_cli_invalid_ids_and_excess_issuers_exit_two(self):
        invalid = ([], ["0"], ["-1"], ["1.5"], ["abc"], [str(2**63)], ["1", "1"],
                   list(map(str, range(1, 12))), ["1", "--unknown"], ["1", "--json"])
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / "registry.db")
            for ids in invalid:
                with self.subTest(ids=ids):
                    status, output, errors = self.cli(["--db", db, "--json", "finmap", *ids])
                    self.assertEqual(status, 2, errors)
                    self.assertEqual(output, "")
                    self.assertTrue(errors)

    def test_cli_missing_entity_exit_one_without_partial_json(self):
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / "registry.db")
            with registry.get_conn(db) as conn:
                eid = registry.upsert_entity(conn, "company", "Apple (synthetic fixture)")
            for ids in (["999"], [str(eid), "999"]):
                with self.subTest(ids=ids):
                    status, output, errors = self.cli(["--db", db, "--json", "finmap", *ids])
                    self.assertEqual(status, 1, errors)
                    self.assertEqual(output, "")
                    self.assertTrue(errors)

    def test_cli_one_and_ten_issuers_preserve_request_order(self):
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / "registry.db")
            with registry.get_conn(db) as conn:
                ids = [registry.upsert_entity(conn, "company", f"Synthetic {index}") for index in range(10)]
            for requested in ([ids[0]], ids[::-1]):
                with self.subTest(requested=requested):
                    status, output, errors = self.cli(["--db", db, "--json", "finmap", *map(str, requested)])
                    self.assertEqual(status, 0, errors)
                    report = json.loads(output)
                    self.assert_report(report, requested)
                    self.assertTrue(all(issuer["groups"] == [] and issuer["warnings"]
                                        for issuer in report["issuers"]))

    def test_f03_exact_allowlist_and_period_types(self):
        self.assertEqual(importer.SEC_CASH_FLOW_METRICS, F03_CASH_FLOWS)
        self.assertIsInstance(importer.SEC_CASH_FLOW_METRICS, tuple)
        self.assertEqual(set(importer.SEC_METRIC_TAGS), set(selected()) | set(F03_TAGS))
        for metric, tag in F03_TAGS.items():
            self.assertEqual(importer.SEC_METRIC_TAGS[metric], (tag,))
        self.assertEqual(importer.SEC_DURATION_TAGS, frozenset({
            "NetIncomeLoss", "Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
            *(F03_TAGS[key] for key in F03_CASH_FLOWS),
        }))
        payload = f03_facts()
        before = copy.deepcopy(payload)
        rows, missing = sources._sec_facts(payload, CIK, "local:companyfacts", RETRIEVED)
        self.assertEqual(payload, before)
        self.assertEqual(missing, [])
        self.assertEqual(len(rows), 13)
        values = {key: json.loads(value) for key, value, _ in rows}
        self.assertEqual({period for _, _, period in rows}, {END})
        for metric, tag in F03_TAGS.items():
            with self.subTest(metric=metric):
                envelope = values[metric]
                self.assertEqual(envelope["value"], payload["facts"]["us-gaap"][tag]["units"]["USD"][0]["val"])
                self.assertEqual((envelope["currency"], envelope["unit"], envelope["scale"]),
                                 ("USD", "currency", 1))
                self.assertEqual(envelope["observation"], {
                    "schema": "sec_fact_v1", "taxonomy": "us-gaap", "tag": tag,
                    "accession": "0000320193-25-000001", "form": "10-K", "filed": "2025-01-30",
                    "start": "2024-01-01" if metric in F03_CASH_FLOWS else None, "end": END,
                    "period_type": "duration" if metric in F03_CASH_FLOWS else "instant",
                    "source_url": "local:companyfacts", "retrieved_at": RETRIEVED,
                    "selection": importer.SEC_SELECTION,
                })
                importer.validate_metric_envelope(envelope, END)
        self.assertEqual(len(facts()["facts"]["us-gaap"]), 6)
        self.assertEqual(len(finmap_fixture()["metrics"]), 6)
        self.assertEqual(len(finmap_fixture(second=True)["metrics"]), 6)

    def test_f03_missing_exact_tags_never_use_broader_or_continuing_scopes(self):
        alternatives = {
            "inventory_net": ("InventoryGross",),
            "long_term_debt_current": ("LongTermDebt", "LongTermDebtAndCapitalLeaseObligationsCurrent"),
            "long_term_debt_noncurrent": ("LongTermDebt", "LongTermDebtAndCapitalLeaseObligations"),
            "short_term_borrowings": ("ShortTermBorrowingsAndCurrentPortionOfLongTermDebt",),
            **{metric: ("NetCashProvidedByUsedIn" + activity + "ActivitiesContinuingOperations",)
               for metric, activity in zip(F03_CASH_FLOWS, ("Operating", "Investing", "Financing"))},
        }
        for metric, tag in F03_TAGS.items():
            for alternative in (None, *alternatives[metric]):
                with self.subTest(metric=metric, alternative=alternative):
                    payload = f03_facts()
                    gaap = payload["facts"]["us-gaap"]
                    original = gaap.pop(tag)
                    if alternative:
                        gaap[alternative] = original
                    rows, missing = sources._sec_facts(payload, CIK, "local:companyfacts", RETRIEVED)
                    self.assertEqual(missing, [tag])
                    self.assertEqual({key for key, _, _ in rows},
                                     (set(selected()) | set(F03_TAGS)) - {metric})
                    self.store([{"k": key, "v": value, "period": period, "source": "sec"}
                                for key, value, period in rows])
                    group = self.group()
                    self.assert_unknown(self.position(group, metric))
                    self.assertIsNone(self.position(group, metric)["observation"])
                    self.assert_unknown(self.position(group, "total_debt"))
                    self.assert_alignment(group, "flows", "ok")
                    self.assert_alignment(group, "cash_flows", "unknown" if metric in F03_CASH_FLOWS else "ok")
                    self.assert_raw(group, registry.entity_payload(self.conn, self.eid)["metrics"])

    def test_f03_exact_tags_win_even_when_broader_tags_are_newer(self):
        payload = f03_facts()
        gaap = payload["facts"]["us-gaap"]
        for tag in ("InventoryGross", "LongTermDebt", "LongTermDebtAndCapitalLeaseObligations",
                    "ShortTermBorrowingsAndCurrentPortionOfLongTermDebt",
                    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
                    "NetCashProvidedByUsedInInvestingActivitiesContinuingOperations",
                    "NetCashProvidedByUsedInFinancingActivitiesContinuingOperations"):
            gaap[tag] = {"units": {"USD": [fact(999, end="2025-12-31", filed="2026-01-30")]}}
        self.assertEqual(selected(payload), selected(f03_facts()))

    def test_f03_each_metric_rejects_borrowed_supported_tags(self):
        for metric in (*F03_TAGS, "total_debt"):
            with self.subTest(metric=metric):
                metrics = f03_fixture()["metrics"]
                donor = copy.deepcopy(next(row for row in metrics if row["k"] == "total_liabilities"))
                donor["k"] = metric
                self.store([row for row in metrics if row["k"] != metric] + [donor])
                group = self.group()
                self.assert_unknown(self.position(group, metric))
                self.assert_raw(group, registry.entity_payload(self.conn, self.eid)["metrics"])

    def test_f03_scopes_identify_exact_stock_and_cash_flow_categories(self):
        self.store(f03_fixture()["metrics"])
        group = self.group()
        terms = {
            "inventory_net": ("inventor", "net"),
            "long_term_debt_current": ("long.term", "debt", "current", "excluding lease"),
            "long_term_debt_noncurrent": ("long.term", "debt", "non.current|noncurrent", "excluding lease"),
            "short_term_borrowings": ("short.term", "borrow", "initial terms"),
            **{metric: ("cash", activity, "including discontinued operations") for metric, activity in
               zip(F03_CASH_FLOWS, ("operating", "investing", "financing"))},
        }
        for metric, patterns in terms.items():
            with self.subTest(metric=metric):
                scope = self.position(group, metric)["scope"].lower()
                self.assertNotIn("_", scope)
                for pattern in patterns:
                    self.assertRegex(scope, pattern)
        scopes = [self.position(group, metric)["scope"] for metric in F03_TAGS]
        self.assertEqual(len(set(scopes)), 7)

    def test_f03_supported_debt_and_cash_tags_are_not_interchangeable(self):
        pairs = (("long_term_debt_current", "long_term_debt_noncurrent"),
                 ("long_term_debt_noncurrent", "short_term_borrowings"),
                 ("short_term_borrowings", "long_term_debt_current"),
                 *zip(F03_CASH_FLOWS, F03_CASH_FLOWS[1:] + F03_CASH_FLOWS[:1]))
        for metric, donor in pairs:
            with self.subTest(metric=metric, donor=donor):
                metrics = f03_fixture()["metrics"]
                row = next(row for row in metrics if row["k"] == metric)
                row["v"]["observation"]["tag"] = F03_TAGS[donor]
                importer.validate_metric_envelope(row["v"], END)
                self.store(metrics)
                group = self.group()
                self.assert_unknown(self.position(group, metric))
                self.assertEqual(self.position(group, donor)["status"], "ok")
                self.assert_raw(group, registry.entity_payload(self.conn, self.eid)["metrics"])

    def test_f03_components_never_compute_total_debt_or_debt_ratios(self):
        self.store(f03_fixture()["metrics"])
        before, changes = list(self.conn.iterdump()), self.conn.total_changes
        group = self.group()
        self.assert_unknown(self.position(group, "total_debt"))
        analysis = engine.analyze_entity(self.conn, self.eid)["fundamentals"]["groups"][0]
        self.assertEqual(analysis["missing_metrics"], ["total_debt"])
        self.assertNotIn("total_debt", analysis["inputs"])
        self.assertEqual(analysis["unsupported_metric_keys"], [])
        indicators = {item["name"]: item for item in analysis["indicators"]}
        self.assertEqual(set(indicators), {"liabilities_to_assets", "equity_to_assets", "debt_to_assets",
                                          "debt_to_equity", "net_margin", "return_on_assets",
                                          "return_on_equity", "cash_to_assets"})
        for name in ("debt_to_assets", "debt_to_equity"):
            self.assertEqual(indicators[name]["status"], "unknown")
            self.assertIsNone(indicators[name]["value"])
            self.assertIsNone(indicators[name]["numerator_value"])
            self.assertEqual(indicators[name]["missing_metrics"], ["total_debt"])
        self.assertEqual(indicators["net_margin"]["value"], 0.2)
        self.assertEqual(indicators["liabilities_to_assets"]["value"], 0.6)
        self.assertEqual(list(self.conn.iterdump()), before)
        self.assertEqual(self.conn.total_changes, changes)

    def test_f03_negative_and_zero_cash_flows_are_valid_not_imputed(self):
        for metric in F03_CASH_FLOWS:
            for value in (-27, 0):
                with self.subTest(metric=metric, value=value):
                    payload = f03_facts()
                    payload["facts"]["us-gaap"][F03_TAGS[metric]]["units"]["USD"][0]["val"] = value
                    values = selected(payload)
                    self.assertIn(metric, values)
                    self.assertEqual(values[metric]["value"], value)
                    self.store([{"k": key, "v": envelope, "source": "sec", "period": END}
                                for key, envelope in values.items()])
                    group = self.group()
                    position = self.position(group, metric)
                    self.assertEqual((position["status"], position["value"]), ("ok", value))
                    self.assertEqual(position["observation"], values[metric]["observation"])
                    self.assert_alignment(group, "cash_flows", "ok")
                    self.assert_alignment(group, "flows", "ok")

    def test_f03_cash_flow_selection_keeps_ytd_not_quarter_or_non_usd(self):
        for metric in F03_CASH_FLOWS:
            with self.subTest(metric=metric):
                payload = f03_facts()
                units = payload["facts"]["us-gaap"][F03_TAGS[metric]]["units"]
                expected = units["USD"][0]["val"]
                units["USD"] += [fact(999, start="2024-10-01"),
                                 fact(777, start="2023-01-01", end="2023-12-31", filed="2026-01-01")]
                units["EUR"] = [fact(888, start="2024-01-01", end="2025-12-31")]
                values = selected(payload)
                self.assertIn(metric, values)
                self.assertEqual(values[metric]["value"], expected)
                self.assertEqual(values[metric]["observation"]["start"], "2024-01-01")
                units["USD"].reverse()
                self.assertEqual(selected(payload), values)

    def test_f03_cash_flow_alignment_mismatch_does_not_change_accounting_flows(self):
        for metric in F03_CASH_FLOWS:
            for field, value in (("start", "2024-10-01"), ("accession", "0000320193-25-000002"),
                                 ("form", "10-Q")):
                with self.subTest(metric=metric, field=field):
                    metrics = f03_fixture()["metrics"]
                    row = next(row for row in metrics if row["k"] == metric)
                    row["v"]["observation"][field] = value
                    self.store(metrics)
                    group = self.group()
                    self.assert_alignment(group, "cash_flows", "unknown")
                    self.assert_alignment(group, "flows", "ok")
                    for key in F03_CASH_FLOWS:
                        self.assertEqual(self.position(group, key)["status"], "ok")
                    self.assertEqual(self.position(group, metric)["observation"], row["v"]["observation"])
                    self.assert_raw(group, registry.entity_payload(self.conn, self.eid)["metrics"])

    def test_f03_accounting_and_cash_flow_scopes_align_independently(self):
        for cash in (False, True):
            metrics = f03_fixture()["metrics"]
            for row in metrics:
                if row["k"] in (F03_CASH_FLOWS if cash else ("net_income", "revenue")):
                    row["v"]["observation"].update(start="2024-10-01", accession="0000320193-25-000002",
                                                    form="10-Q")
            self.store(metrics)
            group = self.group()
            self.assert_alignment(group, "cash_flows", "ok")
            self.assert_alignment(group, "flows", "ok")
            self.assert_alignment(group, "stocks", "unknown")
        metrics = f03_fixture()["metrics"]
        next(row for row in metrics if row["k"] == "net_income")["v"]["observation"]["start"] = "2024-10-01"
        self.store(metrics)
        group = self.group()
        self.assert_alignment(group, "flows", "unknown")
        self.assert_alignment(group, "cash_flows", "ok")

    def test_f03_cash_flow_period_groups_do_not_merge(self):
        for metric in F03_CASH_FLOWS:
            with self.subTest(metric=metric):
                metrics = f03_fixture()["metrics"]
                row = next(row for row in metrics if row["k"] == metric)
                row["period"] = row["v"]["observation"]["end"] = "2024-09-30"
                self.store(metrics)
                report = engine.finmap(self.conn, [self.eid])
                self.assert_report(report, [self.eid])
                groups = {group["period"]: group for group in report["issuers"][0]["groups"]}
                self.assertEqual(set(groups), {END, "2024-09-30"})
                self.assert_unknown(self.position(groups[END], metric))
                self.assertEqual(self.position(groups["2024-09-30"], metric)["status"], "ok")
                for group in groups.values():
                    self.assert_alignment(group, "cash_flows", "unknown")
                    self.assert_raw(group, [row for row in registry.entity_payload(self.conn, self.eid)["metrics"]
                                            if row["period"] == group["period"]])
                self.assert_alignment(groups[END], "flows", "ok")

    def test_f03_missing_start_and_ambiguous_selection_retain_evidence(self):
        for metric in F03_CASH_FLOWS:
            for ambiguous in (False, True):
                with self.subTest(metric=metric, ambiguous=ambiguous):
                    payload = f03_facts()
                    rows = payload["facts"]["us-gaap"][F03_TAGS[metric]]["units"]["USD"]
                    if ambiguous:
                        rows.append(dict(rows[0], val=rows[0]["val"] + 1))
                    else:
                        del rows[0]["start"]
                    values = selected(payload)
                    self.assertIn(metric, values)
                    observation = values[metric]["observation"]
                    if ambiguous:
                        self.assertTrue(observation["ambiguous"])
                        rows.reverse()
                        self.assertEqual(selected(payload), values)
                    else:
                        self.assertIsNone(observation["start"])
                    importer.validate_metric_envelope(values[metric], END)
                    self.store([{"k": key, "v": value, "period": END, "source": "sec"}
                                for key, value in values.items()])
                    group = self.group()
                    self.assert_unknown(self.position(group, metric))
                    self.assertEqual(self.position(group, metric)["observation"], observation)
                    self.assert_alignment(group, "cash_flows", "unknown")
                    self.assert_alignment(group, "flows", "ok")
                    self.assert_raw(group, registry.entity_payload(self.conn, self.eid)["metrics"])

    def test_f03_invalid_cash_flow_metadata_is_unknown_with_raw_evidence(self):
        changes = (("start", None), ("start", "2024-02-30"), ("start", "2025-01-01"),
                   ("end", "2024-12-30"), ("accession", None), ("accession", "bad"),
                   ("form", None), ("form", "invented"), ("filed", "bad"),
                   ("period_type", "instant"), ("taxonomy", "ifrs"), ("schema", "other"),
                   ("retrieved_at", "yesterday"), ("source_url", ""),
                   ("ambiguous", True), ("ambiguous", "false"), ("invalid", True))
        for metric in F03_CASH_FLOWS:
            for field, value in changes:
                with self.subTest(metric=metric, field=field, value=value):
                    metrics = f03_fixture()["metrics"]
                    row = next(row for row in metrics if row["k"] == metric)
                    row["v"]["observation"][field] = value
                    self.store(metrics)
                    before, changes_before = list(self.conn.iterdump()), self.conn.total_changes
                    group = self.group()
                    self.assert_unknown(self.position(group, metric))
                    self.assert_alignment(group, "cash_flows", "unknown")
                    self.assert_alignment(group, "flows", "ok")
                    self.assertTrue(group["warnings"])
                    self.assert_raw(group, registry.entity_payload(self.conn, self.eid)["metrics"])
                    self.assertEqual(list(self.conn.iterdump()), before)
                    self.assertEqual(self.conn.total_changes, changes_before)

    def test_f03_stock_start_is_invalid_not_a_cash_flow(self):
        for metric in set(F03_TAGS) - set(F03_CASH_FLOWS):
            with self.subTest(metric=metric):
                payload = f03_facts()
                payload["facts"]["us-gaap"][F03_TAGS[metric]]["units"]["USD"][0]["start"] = "2024-01-01"
                values = selected(payload)
                self.assertIn(metric, values)
                observation = values[metric]["observation"]
                self.assertEqual(observation["period_type"], "instant")
                self.assertTrue(observation["invalid"])
                self.store([{"k": key, "v": value, "period": END, "source": "sec"}
                            for key, value in values.items()])
                group = self.group()
                self.assert_unknown(self.position(group, metric))
                self.assertEqual(self.position(group, metric)["observation"], observation)
                self.assert_alignment(group, "cash_flows", "ok")
                self.assert_raw(group, registry.entity_payload(self.conn, self.eid)["metrics"])

    def test_f03_two_issuer_import_show_analyze_finmap_evidence_without_writes(self):
        fixtures = [f03_fixture(), f03_fixture(second=True)]
        self.assertNotEqual(fixtures[0]["key"], fixtures[1]["key"])
        for fixture in fixtures:
            self.assertEqual(len(fixture["metrics"]), 13)
        for first, second in zip(fixtures[0]["metrics"], fixtures[1]["metrics"]):
            self.assertNotEqual(first["v"]["value"], second["v"]["value"])
            for field in ("accession", "end", "filed", "retrieved_at", "source_url"):
                self.assertNotEqual(first["v"]["observation"][field], second["v"]["observation"][field])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "f03-issuers.json"
            db = str(Path(directory) / "registry.db")
            path.write_text(json.dumps({"entities": fixtures}), encoding="utf-8")
            with registry.get_conn(db) as conn:
                for _ in range(2):
                    self.assertEqual(importer.import_json(conn, str(path))["metrics"], 26)
                    self.assertEqual(registry.stats(conn)["metrics"], 26)
                ids = [conn.execute("SELECT id FROM entities WHERE key=?", (f["key"],)).fetchone()[0]
                       for f in fixtures]
            with registry.get_conn(db) as conn:
                before, changes = list(conn.iterdump()), conn.total_changes
                conn.execute("PRAGMA query_only=ON")
                report = engine.finmap(conn, ids[::-1])
                self.assert_report(report, ids[::-1])
                self.assertEqual(engine.finmap(conn, ids[::-1]), report)
                for eid, fixture, issuer in zip(ids, fixtures, report["issuers"][::-1]):
                    shown = registry.entity_payload(conn, eid)
                    analyzed = engine.analyze_entity(conn, eid)
                    group = issuer["groups"][0]
                    self.assertEqual(issuer["entity"]["key"], fixture["key"])
                    self.assertEqual(len(issuer["groups"]), 1)
                    expected = [{**row, "v": importer.serialize_metric_envelope(row["v"])}
                                for row in fixture["metrics"]]
                    self.assertCountEqual(shown["metrics"], expected)
                    self.assertCountEqual(analyzed["provenance"]["metrics"], expected)
                    self.assert_raw(group, expected)
                    fundamental = analyzed["fundamentals"]["groups"][0]
                    self.assertEqual(fundamental["missing_metrics"], ["total_debt"])
                    for row in fixture["metrics"]:
                        metric = "cash_and_equivalents" if row["k"] == "cash" else row["k"]
                        position = self.position(group, metric)
                        self.assertEqual(position["status"], "ok")
                        self.assertEqual(position["value"], row["v"]["value"])
                        self.assertEqual(position["observation"], row["v"]["observation"])
                        self.assertEqual((position["currency"], position["unit"], float(position["scale"])),
                                         ("USD", "currency", 1))
                        self.assertEqual(fundamental["inputs"][metric], row["v"]["value"])
                        self.assertEqual(fundamental["input_evidence"][metric]["observation"],
                                         row["v"]["observation"])
                    self.assert_unknown(self.position(group, "total_debt"))
                    self.assert_alignment(group, "flows", "ok")
                    self.assert_alignment(group, "cash_flows", "ok")
                    self.assert_alignment(group, "stocks", "unknown")
                    for command, expected_report in (("show", shown), ("analyze", analyzed)):
                        status, output, errors = self.cli(["--db", db, "--json", command, str(eid)])
                        self.assertEqual(status, 0, errors)
                        self.assertEqual(errors, "")
                        self.assertEqual(json.loads(output), expected_report)
                self.assertEqual(conn.total_changes, changes)
                self.assertEqual(list(conn.iterdump()), before)
            for flags in (["--json"], []):
                status, output, errors = self.cli(["--db", db, *flags, "finmap", *map(str, ids[::-1])])
                self.assertEqual(status, 0, errors)
                self.assertEqual(errors, "")
                self.assertEqual(json.loads(output), report)
            with closing(sqlite3.connect(db)) as conn:
                self.assertEqual(list(conn.iterdump()), before)

    def test_cli_help_lists_finmap_and_bounded_ids(self):
        status, output, errors = self.cli(["--help"])
        self.assertEqual(status, 0, errors)
        self.assertIn("finmap", output)
        status, output, errors = self.cli(["finmap", "--help"])
        self.assertEqual(status, 0, errors)
        self.assertIn("finmap", output)
        self.assertIn("ID", output)
        self.assertIn("10", output)
