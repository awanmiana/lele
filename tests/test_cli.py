import csv
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from lele.analysis import engine
from lele.cli.main import COMMANDS, MENU_LABELS, main
from lele.core import registry
from lele.fetchers.http import SourceError


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "registry.db"
        self.fixture = self.root / "import fixture.json"
        self.fixture.write_text(json.dumps({
            "entities": [
                {"key": "local:bank", "kind": "bank", "name": "Example Bank", "country": "US",
                 "attributes": {"source_url": "local:report", "currency": "USD"},
                 "metrics": [{"k": "assets", "v": 100, "period": "2025", "source": "local:report"},
                             {"k": "liabilities", "v": 75, "period": "2025", "source": "local:report"}]},
                {"key": "local:authority", "kind": "regulator", "name": "Authority", "country": "GB",
                 "attributes": {"source_url": "local:register"}},
            ],
            "relationships": [{"src": "local:bank", "rel": "regulated_by", "dst": "local:authority",
                               "source_url": "local:register", "evidence": "Listed in register"}],
        }), encoding="utf-8")
        guard = patch("lele.fetchers.sources.HTTPClient", side_effect=AssertionError("Live network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    def invoke(self, *args, machine=True):
        argv = ["--db", str(self.db)] + (["--json"] if machine else []) + list(args)
        return self.capture(argv)

    def capture(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = main(argv)
        self.assertIs(type(status), int)
        return status, out.getvalue(), err.getvalue()

    def success(self, *args):
        status, out, err = self.invoke(*args)
        self.assertEqual((status, err), (0, ""), out)
        return json.loads(out)

    def imported(self):
        self.assertEqual(self.success("import", str(self.fixture))["entities"], 2)
        return str(self.success("list", "--kind", "bank")[0]["id"])

    def test_end_to_end(self):
        self.assertEqual(self.success("init"), {"initialized": True})
        eid = self.imported()
        rows = self.success("list", "--query", "Example", "--kind", "bank", "--country", "US", "--limit", "1")
        self.assertEqual([row["name"] for row in rows], ["Example Bank"])
        payload = self.success("show", eid)
        self.assertEqual(payload["attributes"]["source_url"], "local:report")
        report = self.success("analyze", eid)
        self.assertTrue(report["found"])
        ratios = report["fundamentals"]["groups"][0]["indicators"]
        self.assertEqual(next(r["value"] for r in ratios if r["name"] == "liabilities_to_assets"), 0.75)
        self.assertEqual(self.success("relationships", eid)[0]["other_name"], "Authority")
        self.assertEqual(self.success("countries"), ["GB", "US"])
        self.assertEqual(self.success("stats")["entities"], 2)
        signal = self.success("sentiment", eid, "--text", "The bank reported strong profit growth.", "--source", "local:report", "--ref", "page 2")
        self.assertTrue(signal["recorded"])
        self.assertEqual(signal["ref"], "page 2")
        self.assertEqual(self.success("analyze", eid)["sentiment"]["scored_signals"], 1)
        text = self.root / "sentiment text.txt"
        text.write_text("The bank reported weak losses.", encoding="utf-8")
        self.assertTrue(self.success("sentiment", eid, "--file", str(text), "--source", "local:news")["recorded"])
        self.assertEqual(len(self.success("show", eid)["signals"]), 2)
        output = self.root / "export.json"
        self.assertEqual(self.success("export", "--format", "json", "--output", str(output), "--kind", "bank")["exported"], 1)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), self.success("list", "--kind", "bank"))
        self.assertEqual(self.success("import", str(self.fixture))["entities"], 2)
        self.assertEqual(self.success("stats")["entities"], 2)

    def test_sources_catalog_without_database(self):
        catalog = self.success("sources")
        self.assertEqual([item["id"] for item in catalog], ["gleif", "fdic", "worldbank", "osfi", "sec"])
        self.assertFalse(self.db.exists())
        self.assertEqual(set(COMMANDS), {"init", "sources", "version", "kinds", "list", "show", "stats", "countries", "fetch", "fetch-prices", "fetch-evidence", "fetch-cot", "fetch-short", "fetch-news", "fetch-form4", "fetch-13f", "fetch-material", "fetch-formd", "fetch-nport", "fetch-awards", "fetch-lobbying", "fetch-treasury", "fetch-political", "fetch-formadv", "fetch-formadv-individual", "fetch-comtrade", "fetch-census-trade", "fetch-eia", "fetch-bls", "fetch-opensky", "runs", "edges", "import", "import-history", "analyze", "finmap", "project", "compare", "prospective", "worldstate", "rag", "indicators", "episodes", "events", "volatility-analyze", "fetch-history", "fetch-sentiment",
                      "fetch-stablecoins", "fetch-market-activity", "fetch-news-feed",
"moves", "causes", "scan", "explain", "capital", "store-evidence",
                  "instruments", "volatility", "framework",
                  "sentiment", "relationships", "tree", "backup", "doctor", "resolve", "sanctions", "flows", "observations", "links", "export", "menu"})
        self.assertEqual(set(MENU_LABELS), (set(COMMANDS) - {"menu"}) | {"help", "back", "quit"})

    def test_kinds_catalog_without_database(self):
        catalog = self.success("kinds")
        by_kind = {item["kind"]: item for item in catalog}
        self.assertTrue({"bank", "fund", "legal_entity", "instrument"} <= set(by_kind))
        self.assertTrue(all(item["definition"] for item in catalog))
        self.assertFalse(self.db.exists())
        status, out, err = self.invoke("kinds", machine=False)
        self.assertEqual(status, 0)

    def test_bounded_limits(self):
        with registry.get_conn(str(self.db)) as conn:
            for index in range(60):
                registry.upsert_entity(conn, "bank", f"Bank {index:03}")
        self.assertEqual(len(self.success("list")), 50)
        self.assertEqual(len(self.success("list", "--limit", "1")), 1)
        self.assertEqual(len(self.success("list", "--limit", "1000")), 60)
        for command in (["list"], ["fetch", "gleif"], ["export", "--format", "json", "--output", str(self.root / "out.json")]):
            for limit in ("0", "-1", "1001", "1.5", "bad", "9" * 5000):
                with self.subTest(command=command, limit=limit[:20]):
                    status, out, err = self.invoke(*command, "--limit", limit)
                    self.assertEqual((status, out), (2, ""))
                    self.assertIn("limit", err)
                    self.assertNotIn("Traceback", err)
        output = self.root / "default.json"
        self.assertEqual(self.success("export", "--format", "json", "--output", str(output))["exported"], 50)

    def test_invalid_usage_and_missing_entities(self):
        for args in (["unknown"], ["show"], ["show", "0"], ["show", "-1"], ["show", str(2**63)],
                     ["show", "NaN"], ["list", "--json"], ["list", "--db", "elsewhere"],
                     ["--js", "list"], ["list", "--lim", "2"], ["--db", "", "init"],
                     ["fetch", "other"], ["export", "--format", "xml", "--output", "out"],
                     ["export", "--format", "json"], ["import"],
                     ["sentiment", "1", "--source", "test"],
                     ["sentiment", "1", "--text", "x", "--file", "x", "--source", "test"],
                     ["sentiment", "1", "--text", "x"]):
            with self.subTest(args=args):
                status, out, err = self.invoke(*args)
                self.assertEqual((status, out), (2, ""))
                self.assertTrue(err)
                self.assertNotIn("Traceback", err)
        for command in ("show", "analyze", "relationships", "sentiment"):
            extra = ["--text", "strong profit", "--source", "test"] if command == "sentiment" else []
            status, out, err = self.invoke(command, "999", *extra)
            self.assertEqual((status, out), (1, ""))
            self.assertIn("not found", err)

    def test_fetch_dispatch_and_validation(self):
        for source in ("gleif", "fdic", "worldbank"):
            with self.subTest(source=source), patch("lele.fetchers.sources.fetch_source", return_value={"source": source, "stored": 1}) as fetch:
                extra = ["--indicator", "NY.GDP.MKTP.CD"] if source == "worldbank" else ["--query", "Example"]
                result = self.success("fetch", source, "--country", "US", "--limit", "10", *extra)
                self.assertEqual(result["stored"], 1)
                self.assertEqual(fetch.call_args.args[1], source)
                self.assertEqual(fetch.call_args.kwargs, {"query": "" if source == "worldbank" else "Example", "country": "US", "limit": 10, "indicator": "NY.GDP.MKTP.CD", "category": "", "financials": False, "resume": False})
        for args in (["gleif", "--country", "USA"], ["fdic", "--country", "GB"],
                     ["worldbank", "--query", "bank"], ["worldbank", "--country", "US;GB"],
                     ["worldbank", "--indicator", "../x"], ["worldbank", "--indicator", ""],
                     ["fdic", "--indicator", "NY.GDP.MKTP.CD"], ["gleif", "--query", "x" * 201]):
            with self.subTest(args=args), patch("lele.fetchers.sources.fetch_source") as fetch:
                status, out, err = self.invoke("fetch", *args)
                self.assertEqual((status, out), (2, ""))
                self.assertTrue(err)
                fetch.assert_not_called()

    def test_edges_dispatch_json_and_human_output(self):
        result = {"processed": 3, "linked": 2, "missing": 1, "retracted": 0, "skipped": 4,
                  "warnings": ["Optional corroboration unavailable"], "edges": []}
        with patch("lele.fetchers.sources.edge_fund_managers", return_value=result) as edges:
            output = self.success("edges", "--kind", "fund", "--source", "gleif")
            self.assertEqual(set(output), {"source", "kind", "processed", "linked", "missing",
                                           "retracted", "skipped", "warnings"})
            self.assertEqual(output, {"source": "gleif", "kind": "fund", **{k: v for k, v in result.items() if k != "edges"}})
            self.assertIsInstance(edges.call_args.args[0], sqlite3.Connection)
            self.assertEqual(edges.call_args.kwargs, {"limit": 25, "refresh": False})
            status, out, err = self.invoke("edges", "--kind", "fund", "--source", "gleif", "--limit", "1", machine=False)
            self.assertEqual(status, 0)
            self.assertEqual(out.splitlines()[0].split(),
                             ["PROCESSED", "LINKED", "MISSING", "RETRACTED", "SKIPPED"])
            self.assertEqual(out.splitlines()[1].split(), ["3", "2", "1", "0", "4"])
            self.assertIn("Optional corroboration unavailable", err)
            self.assertEqual(edges.call_args.kwargs, {"limit": 1, "refresh": False})
        self.assertEqual(COMMANDS["edges"], "Build sourced relationships from stored entities")

    def test_edges_inactive_relationship_rejected_with_mock_http(self):
        fund_lei, manager_lei = "00000000000000000001", "01ERPZV3DOLNXY2MLB90"
        with registry.get_conn(str(self.db)) as conn:
            fund_id = registry.upsert_entity(conn, "fund", "Fund", key=f"lei:{fund_lei}")
        manager = {"data": {"id": manager_lei, "type": "lei-records", "attributes": {
            "lei": manager_lei, "entity": {"legalName": {"name": "Manager"}},
        }}}
        relationship = {"data": {"attributes": {"relationship": {
            "startNode": {"id": fund_lei}, "endNode": {"id": manager_lei},
            "type": "IS_FUND-MANAGED_BY", "status": "INACTIVE",
        }}}}
        with patch("lele.fetchers.sources.HTTPClient") as factory:
            client = factory.return_value
            client.warnings = []
            client.retrieved_at = "2026-09-17T00:00:00Z"
            for machine in (True, False):
                with self.subTest(machine=machine):
                    client.get_json.side_effect = [manager, relationship]
                    status, out, err = self.invoke("edges", "--kind", "fund", "--source", "gleif", machine=machine)
                    self.assertEqual(status, 0)
                    if machine:
                        result = json.loads(out)
                        self.assertEqual([result[k] for k in ("processed", "linked", "missing", "skipped")], [1, 0, 1, 0])
                        self.assertEqual(len(result["warnings"]), 1)
                        self.assertIn("conflict", result["warnings"][0])
                        self.assertEqual(err, "")
                    else:
                        self.assertEqual(out.splitlines()[1].split(), ["1", "0", "1", "0", "0"])
                        self.assertIn("relationship corroboration", err)
                    self.assertEqual(self.success("relationships", str(fund_id)), [])
                    self.assertEqual(self.success("show", str(fund_id))["attributes"], {})
                    self.assertEqual(self.success("stats")["entities"], 1)
            self.assertEqual(client.get_json.call_count, 4)
            self.assertTrue(client.get_json.call_args.args[0].endswith("/fund-manager-relationship"))

    def test_edges_invalid_arguments_and_help(self):
        for args in ([], ["--source", "gleif"], ["--kind", "fund"],
                     ["--kind", "bank", "--source", "gleif"], ["--kind", "fund", "--source", "sec"],
                     ["--kind", "fund", "--source", "gleif", "--limit", "0"],
                     ["--kind", "fund", "--source", "gleif", "--limit", "1001"]):
            with self.subTest(args=args), patch("lele.fetchers.sources.edge_fund_managers") as edges:
                status, out, err = self.invoke("edges", *args)
                self.assertEqual((status, out), (2, ""))
                self.assertTrue(err)
                edges.assert_not_called()
                self.assertFalse(self.db.exists())
        status, out, err = self.invoke("edges", "--help")
        self.assertEqual((status, err), (0, ""))
        self.assertIn("default: 25", out)
        self.assertIn("corroboration is optional", " ".join(out.split()))

    def test_edges_empty_registry_json(self):
        result = self.success("edges", "--kind", "fund", "--source", "gleif")
        self.assertEqual(result, {"source": "gleif", "kind": "fund", "processed": 0,
                                  "linked": 0, "missing": 0, "retracted": 0, "skipped": 0,
                                  "warnings": []})

    def test_list_exact_country_dimension_filters(self):
        self.success("init")
        self.success("import", str(self.fixture))
        with registry.get_conn(str(self.db)) as conn:
            bank = conn.execute("SELECT id FROM entities WHERE key='local:bank'").fetchone()[0]
            authority = conn.execute("SELECT id FROM entities WHERE key='local:authority'").fetchone()[0]
            registry.set_attr(conn, bank, "iso_jurisdiction", "US-NY")
            registry.set_attr(conn, bank, "gleif.headquarters_country", "GB")
            registry.set_attr(conn, authority, "iso_jurisdiction", "GB-ENG")
            registry.set_attr(conn, authority, "gleif.headquarters_country", "US")
            registry.set_attr(conn, bank, "source", "gleif")
            registry.set_attr(conn, authority, "source", "fdic")
        self.assertEqual([r["name"] for r in self.success("list", "--legal-country", "US")],
                         ["Example Bank"])
        self.assertEqual([r["name"] for r in self.success("list", "--jurisdiction", "GB-ENG")],
                         ["Authority"])
        self.assertEqual([r["name"] for r in self.success("list", "--hq-country", "US")],
                         ["Authority"])
        self.assertEqual(self.success("list", "--legal-country", "us"), [])
        self.assertEqual([r["name"] for r in self.success("list", "--source", "fdic")],
                         ["Authority"])

    def test_flows_summary_command(self):
        self.success("init")
        bank = int(self.imported())
        with registry.get_conn(str(self.db)) as conn:
            counterparty = registry.upsert_entity(conn, "bank", "Counterparty", key="local:cp",
                                                  country="US")
            registry.add_money_flow(conn, counterparty, bank, "loan", "250", "USD", "2026-02-01",
                                    "https://example.gov/loan", "Loan agreement")
        result = self.success("flows", "summary", str(bank))
        usd = next(item for item in result["currencies"] if item["currency"] == "USD")
        self.assertEqual((usd["inflow"], usd["outflow"], usd["net"]), ("250", "0", "250"))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--db", str(self.db), "flows", "summary"]), 2)

    def test_links_command(self):
        self.success("init")
        eid = int(self.imported())
        record = self.success("links", "set", str(eid), "https://example.com/site",
                              "--type", "website", "--source-url", "https://register.example/1")
        self.assertEqual(record["url"], "https://example.com/site")
        links = self.success("links", "list", str(eid))
        self.assertEqual([(item["link_type"], item["url"]) for item in links],
                         [("website", "https://example.com/site")])
        self.assertEqual(self.success("links", "remove", str(eid),
                                      "https://example.com/site")["removed"], 1)
        status, out, err = self.invoke("links", "set", str(eid), "http://insecure.example",
                                       "--type", "website", "--source-url",
                                       "https://register.example/1")
        self.assertEqual(status, 1)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--db", str(self.db), "links", "set", str(eid),
                                   "https://x.example"]), 2)

    def test_flows_import_and_list(self):
        self.success("init")
        bank = int(self.imported())
        with registry.get_conn(str(self.db)) as conn:
            registry.upsert_entity(conn, "bank", "Counterparty", key="local:cp", country="US")
        source_file = self.root / "flows.json"
        source_file.write_text(json.dumps({"flows": [
            {"src": "local:bank", "dst": "local:cp", "type": "loan", "amount": "500000",
             "currency": "USD", "occurred_at": "2026-02-01",
             "source_url": "https://example.gov/loan", "evidence": "Loan agreement"}]}),
            encoding="utf-8")
        report = self.success("flows", "import", str(source_file))
        self.assertEqual((report["imported"], report["amounts_summed_across_currencies"]),
                         (1, False))
        flows = self.success("flows", "list")
        self.assertEqual((flows[0]["src_key"], flows[0]["dst_key"], flows[0]["amount"]),
                         ("local:bank", "local:cp", "500000"))
        self.assertEqual(len(self.success("flows", "list", str(bank))), 1)
        bad = self.root / "bad-flows.json"
        bad.write_text(json.dumps({"flows": [
            {"src": "local:bank", "dst": "local:missing", "type": "loan", "amount": "1",
             "currency": "USD", "source_url": "https://example.gov/x"}]}), encoding="utf-8")
        status, out, err = self.invoke("flows", "import", str(bad))
        self.assertEqual(status, 1)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--db", str(self.db), "flows", "import"]), 2)

    def test_observations_import_list_and_evidence(self):
        self.success("init")
        bank = int(self.imported())
        source_file = self.root / "observations.json"
        source_file.write_text(json.dumps({"observations": [
            {"source": "sec-form4", "external_id": "t1", "kind": "insider_trade",
             "description": "Open-market sale", "instrument": "local:bank",
             "actor": "local:authority", "amount": "10", "unit": "shares", "currency": "USD",
             "observed_at": "2026-01-02T12:00:00+00:00",
             "available_at": "2026-01-02T12:05:00+00:00",
             "source_url": "https://www.sec.gov/Archives/aapl"}]}), encoding="utf-8")
        report = self.success("observations", "import", str(source_file))
        self.assertEqual((report["imported"], report["kinds"]), (1, ["insider_trade"]))
        rows = self.success("observations", "list")
        self.assertEqual((rows[0]["instrument_key"], rows[0]["actor_key"]),
                         ("local:bank", "local:authority"))
        self.assertEqual(len(self.success("observations", "list", str(bank))), 1)
        output = self.root / "projected.json"
        result = self.success("observations", "evidence", str(bank), "--output", str(output))
        self.assertEqual(result["observations"], 1)
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["observations"][0]["kind"], "insider_trade")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--db", str(self.db), "observations", "import"]), 2)
            self.assertEqual(main(["--db", str(self.db), "observations", "evidence", str(bank)]), 2)

    def test_sanctions_fetch_dispatch(self):
        with patch("lele.analysis.sanctions.fetch_sanctions",
                   return_value={"source": "un-consolidated", "imported": 3}) as fetch:
            result = self.success("sanctions", "fetch", "un-consolidated")
            self.assertEqual(result["imported"], 3)
            self.assertEqual(fetch.call_args.args[1], "un-consolidated")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--db", str(self.db), "sanctions", "fetch"]), 2)

    def test_sanctions_import_candidates_and_link(self):
        self.success("init")
        bank = int(self.imported())
        source_file = self.root / "sdn.json"
        source_file.write_text(json.dumps({"listings": [
            {"key": "SDN-1", "name": "Example Bank", "type": "Entity", "program": "SDGT",
             "country": "US", "basis": "designation", "status": "active",
             "published_at": "2026-01-01", "effective_at": "2026-01-02", "evidence": "entry"}]}),
            encoding="utf-8")
        report = self.success("sanctions", "import", "ofac-sdn", str(source_file),
                              "--source-url", "https://example.gov/sdn")
        self.assertEqual((report["imported"], report["source"]), (1, "ofac-sdn"))
        candidates = self.success("sanctions", "candidates")
        self.assertEqual((candidates[0]["entity_id"], candidates[0]["review"]),
                         (bank, "unreviewed_candidate"))
        self.assertTrue(self.success("sanctions", "candidates", str(bank), "--fuzzy"))
        listing_id = candidates[0]["listing_id"]
        linked = self.success("sanctions", "link", str(listing_id), str(bank), "--reason", "reviewed")
        self.assertEqual(linked["entity_id"], bank)
        relationships = self.success("relationships", str(bank))
        self.assertIn("sanctioned_by", [item["rel"] for item in relationships])
        self.assertEqual(self.success("sanctions", "candidates"), [])
        self.assertEqual(self.success("sanctions", "links")[0]["listing_id"], listing_id)
        self.assertEqual(len(self.success("sanctions", "listings", "--active-only")), 1)
        self.assertEqual(self.success("sanctions", "unlink", str(listing_id), str(bank))["unlinked"], 1)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--db", str(self.db), "sanctions", "import", "ofac-sdn"]), 2)

    def test_resolve_command(self):
        self.success("init")
        bank = int(self.imported())
        with registry.get_conn(str(self.db)) as conn:
            duplicate = registry.upsert_entity(conn, "bank", "Example Bank", key="local:dup",
                                               country="US")
        candidates = self.success("resolve", "candidates")
        self.assertTrue(candidates)
        self.assertTrue(all(item["review"] == "unreviewed_candidate" for item in candidates))
        self.assertTrue(self.success("resolve", "candidates", str(bank), "--fuzzy"))
        merged = self.success("resolve", "merge", str(duplicate), str(bank), "--reason", "same bank")
        self.assertEqual((merged["alias_id"], merged["canonical_id"]), (duplicate, bank))
        self.assertEqual(self.success("resolve", "list")[0]["alias_id"], duplicate)
        self.assertEqual(self.success("resolve", "unmerge", str(duplicate))["unmerged"], 1)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--db", str(self.db), "resolve", "merge", str(duplicate)]), 2)

    def test_doctor_command(self):
        self.success("init")
        self.imported()
        result = self.success("doctor")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["registry"]["entities"], 2)
        self.assertEqual(result["registry"]["integrity_check"], "ok")
        missing = self.root / "absent.db"
        status, out, err = self.capture(["--db", str(missing), "--json", "doctor"])
        self.assertEqual((status, err), (0, ""))
        self.assertFalse(json.loads(out)["registry"]["present"])
        self.assertFalse(missing.exists())

    def test_backup_command(self):
        self.success("init")
        self.imported()
        target = self.root / "backup.db"
        result = self.success("backup", str(target))
        self.assertEqual(result["entities"], 2)
        self.assertTrue(target.exists())
        status, out, err = self.invoke("backup", str(target))
        self.assertEqual(status, 1)
        self.assertTrue(err)
        missing = self.root / "absent.db"
        other = self.root / "other.db"
        status, out, err = self.capture(["--db", str(missing), "backup", str(other)])
        self.assertEqual(status, 1)
        self.assertFalse(missing.exists())
        self.assertFalse(other.exists())

    def test_tree_dispatch_and_bounds(self):
        self.success("init")
        eid = int(self.imported())
        result = self.success("tree", str(eid), "--depth", "1")
        self.assertEqual(result["method"], "gleif_consolidation_tree_v1")
        self.assertEqual(result["root_id"], eid)
        self.assertEqual([node["direction"] for node in result["nodes"]], ["self"])
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--db", str(self.db), "tree", str(eid), "--depth", "0"]), 2)

    def test_edges_parent_dispatch(self):
        result = {"processed": 1, "linked": 1, "no_parent": 0, "exceptions": 0, "missing": 0,
                  "retracted": 0, "skipped": 0, "warnings": [], "edges": []}
        with patch("lele.fetchers.sources.edge_parents", return_value=result) as parents:
            output = self.success("edges", "--kind", "parent", "--source", "gleif")
            self.assertEqual(output["kind"], "parent")
            self.assertEqual(output["linked"], 1)
            self.assertIn("exceptions", output)
            self.assertNotIn("edges", output)
            self.assertEqual(parents.call_args.kwargs,
                             {"limit": 25, "refresh": False, "level": "direct"})
        with patch("lele.fetchers.sources.edge_parents", return_value=result) as parents:
            self.success("edges", "--kind", "parent", "--source", "gleif", "--level", "ultimate")
            self.assertEqual(parents.call_args.kwargs["level"], "ultimate")

    def test_fetch_category_financials_choices_and_help(self):
        for source, flags, category, financials in (
            ("gleif", ["--category", "FUND"], "FUND", False),
            ("gleif", ["--category", "SOLE_PROPRIETOR"], "SOLE_PROPRIETOR", False),
            ("sec", ["--query", "aapl", "--financials"], "", True),
            ("sec", ["--query", "0000320193"], "", False),
        ):
            with self.subTest(source=source, flags=flags), patch("lele.fetchers.sources.fetch_source", return_value={}) as fetch:
                self.success("fetch", source, *flags)
                self.assertEqual(fetch.call_args.kwargs["category"], category)
                self.assertEqual(fetch.call_args.kwargs["financials"], financials)
        for flags in (["gleif", "--category", "VC"], ["fdic", "--category", "FUND"],
                      ["sec", "--query", "AAPL", "--category", "FUND"],
                      ["gleif", "--financials"], ["osfi", "--financials"],
                      ["sec"], ["sec", "--query", "BRK.B"],
                      ["sec", "--query", "AAPL", "--country", "US"]):
            with self.subTest(flags=flags), patch("lele.fetchers.sources.fetch_source") as fetch:
                status, out, err = self.invoke("fetch", *flags)
                self.assertEqual((status, out), (2, ""))
                self.assertTrue(err)
                fetch.assert_not_called()
        status, out, err = self.invoke("fetch", "--help")
        self.assertEqual((status, err), (0, ""))
        for text in ("sec", "--category", "SOLE_PROPRIETOR", "--financials", "20 MiB", "2 MiB", "previously stored", "maximum recent filings"):
            self.assertIn(text, " ".join(out.split()))

    def test_errors_do_not_leak_exception_secrets(self):
        for target, error, args in (
            ("lele.fetchers.sources.fetch_source", SourceError("token=SECRET"), ["fetch", "fdic"]),
            ("lele.core.registry.init_db", sqlite3.OperationalError("password=SECRET"), ["init"]),
            ("lele.core.importer.import_json", OSError("SECRET"), ["import", str(self.fixture)]),
            ("lele.core.importer.import_json", ValueError("SECRET"), ["import", str(self.fixture)]),
        ):
            with self.subTest(target=target), patch(target, side_effect=error):
                status, out, err = self.invoke(*args)
                self.assertEqual((status, out), (1, ""))
                self.assertTrue(err)
                self.assertNotIn("SECRET", err)
                self.assertNotIn("Traceback", err)
        self.assertEqual(self.invoke("import", str(self.root / "missing"))[0], 1)
        self.fixture.write_text('{"entities": [invalid}', encoding="utf-8")
        self.assertEqual(self.invoke("import", str(self.fixture))[0], 1)
        self.assertEqual(self.success("stats")["entities"], 0)
        self.assertEqual(self.capture(["--db", str(self.root), "init"])[0], 1)

    def test_sentiment_validation_and_insufficient_evidence(self):
        eid = self.imported()
        for text in ("", " ", "x" * (engine.MAX_TEXT_LEN + 1)):
            self.assertEqual(self.invoke("sentiment", eid, "--text", text, "--source", "test")[0], 2)
        for option, value in (("--source", " "), ("--source", "x" * (engine.MAX_SOURCE_LEN + 1)), ("--ref", "x" * (engine.MAX_REF_LEN + 1))):
            self.assertEqual(self.invoke("sentiment", eid, "--text", "strong profit", "--source", "test", option, value)[0], 2)
        path = self.root / "text.txt"
        for content in (b"\xff", b"x" * (engine.MAX_TEXT_LEN + 1)):
            path.write_bytes(content)
            self.assertEqual(self.invoke("sentiment", eid, "--file", str(path), "--source", "test")[0], 2)
        self.assertEqual(self.invoke("sentiment", eid, "--file", str(self.root / "absent"), "--source", "test")[0], 1)
        result = self.success("sentiment", eid, "--text", "The bank has assets.", "--source", "test")
        self.assertFalse(result["recorded"])
        self.assertEqual(self.success("stats")["signals"], 0)

    def test_export_csv_injection_and_quoting(self):
        names = ["=SUM(1,2)", "+1", "-1", "@example", "  =1", "\tplain", "\rplain", "\nplain", "Ordinary, \"Bank\"", "normal\nname"]
        with registry.get_conn(str(self.db)) as conn:
            for index, name in enumerate(names):
                registry.upsert_entity(conn, "bank", name, key=f"local:{index}", notes="\t=1")
        output = self.root / "entities.csv"
        self.assertEqual(self.success("export", "--format", "csv", "--output", str(output))["exported"], len(names))
        with output.open(encoding="utf-8", newline="") as stream:
            rows = {row["key"]: row for row in csv.DictReader(stream)}
        for index, name in enumerate(names):
            row = rows[f"local:{index}"]
            self.assertEqual(row["name"], ("'" if index < 8 else "") + name)
            self.assertEqual(row["notes"], "'\t=1")
            self.assertEqual(row["country"], "")
        self.assertEqual({r["name"] for r in self.success("list")}, set(names))

    def test_export_no_overwrite_force_parent_and_database_guard(self):
        self.imported()
        output = self.root / "existing.json"
        output.write_text("original", encoding="utf-8")
        args = ["export", "--format", "json", "--output", str(output)]
        self.assertEqual(self.invoke(*args)[0], 1)
        self.assertEqual(output.read_text(encoding="utf-8"), "original")
        self.assertEqual(self.success(*args, "--force")["exported"], 2)
        self.assertEqual(len(json.loads(output.read_text(encoding="utf-8"))), 2)
        absent = self.root / "missing" / "out.json"
        self.assertEqual(self.invoke("export", "--format", "json", "--output", str(absent))[0], 1)
        self.assertFalse(absent.parent.exists())
        self.assertEqual(self.invoke("export", "--format", "json", "--output", str(self.root), "--force")[0], 1)
        for destination in (self.db, Path(str(self.db) + "-wal"), Path(str(self.db) + "-shm")):
            self.assertEqual(self.invoke("export", "--format", "json", "--output", str(destination), "--force")[0], 1)
        link = self.root / "database-link"
        os.link(self.db, link)
        self.assertEqual(self.invoke("export", "--format", "json", "--output", str(link), "--force")[0], 1)
        self.assertEqual(self.success("stats")["entities"], 2)
        dangling = self.root / "dangling"
        dangling.symlink_to(self.root / "absent-target")
        self.assertEqual(self.invoke("export", "--format", "json", "--output", str(dangling))[0], 1)
        self.assertTrue(dangling.is_symlink())

    def test_export_atomic_failure_and_concurrent_creation(self):
        self.imported()
        output = self.root / "atomic.json"
        output.write_text("original", encoding="utf-8")
        args = ["export", "--format", "json", "--output", str(output)]
        for target in ("lele.cli.main.json.dump", "lele.cli.main.os.replace", "lele.cli.main.os.fsync"):
            with self.subTest(target=target), patch(target, side_effect=OSError("SECRET")):
                status, out, err = self.invoke(*args, "--force")
                self.assertEqual((status, out), (1, ""))
                self.assertNotIn("SECRET", err)
            self.assertEqual(output.read_text(encoding="utf-8"), "original")
            self.assertEqual(list(self.root.glob(".lele-*")), [])
        output.unlink()

        def race(src, dst):
            output.write_text("competitor", encoding="utf-8")
            raise FileExistsError

        with patch("lele.cli.main.os.link", side_effect=race):
            self.assertEqual(self.invoke(*args)[0], 1)
        self.assertEqual(output.read_text(encoding="utf-8"), "competitor")
        self.assertEqual(list(self.root.glob(".lele-*")), [])

    def test_export_parent_checked_before_registry_creates_directories(self):
        parent = self.root / "absent"
        status, out, err = self.capture([
            "--db", str(parent / "registry.db"), "export", "--format", "json",
            "--output", str(parent / "out.json"),
        ])
        self.assertEqual((status, out), (1, ""))
        self.assertIn("parent directory", err)
        self.assertFalse(parent.exists())

    def test_no_link_runtime_fails_safely(self):
        with patch("lele.cli.main.os.link", create=True):
            del os.link
            status, out, err = self.invoke("export", "--format", "json", "--output", str(self.root / "out.json"))
        self.assertEqual((status, out), (1, ""))
        self.assertIn("runtime", err)
        self.assertFalse(self.db.exists())
        self.assertFalse((self.root / "out.json").exists())

    def test_non_tty_help_and_global_help(self):
        with patch("sys.stdin", io.StringIO("")):
            status, out, err = self.invoke(machine=False)
        self.assertEqual((status, err), (0, ""))
        self.assertIn("usage: lele", out)
        self.assertIn("must precede the command", out)
        for command in COMMANDS:
            self.assertIn(command, out)
        self.assertFalse(self.db.exists())
        self.assertEqual(set(self.success()["commands"]), set(COMMANDS))
        self.assertEqual(self.invoke("--help", machine=False)[0], 0)

    def test_human_output_and_control_sanitization(self):
        self.imported()
        with registry.get_conn(str(self.db)) as conn:
            registry.upsert_entity(conn, "bank", "Bank\x1b[31m\nlong")
        status, out, err = self.invoke("list", machine=False)
        self.assertEqual((status, err), (0, ""))
        self.assertIn("ID", out)
        self.assertIn("Example Bank", out)
        self.assertNotIn("\x1b", out)
        self.assertIn("No results", self.invoke("list", "--query", "absent", machine=False)[1])
        for command in ("stats", "sources", "countries"):
            self.assertEqual(self.invoke(command, machine=False)[0], 0)

    def test_menu_eof_interrupt_and_default_tty(self):
        for interrupt in (EOFError, KeyboardInterrupt):
            with self.subTest(interrupt=interrupt), patch("builtins.input", side_effect=interrupt):
                status, out, err = self.invoke("menu", machine=False)
                self.assertEqual((status, out), (0, ""))
                self.assertIn("Menu", err)
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=EOFError):
            self.assertEqual(self.invoke(machine=False)[0], 0)
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=AssertionError("JSON must not prompt")):
            self.assertIn("commands", self.success())

    def test_menu_reuses_dispatch_and_does_not_execute_shell(self):
        output = self.root / "menu export.json"
        inputs = ["bad", "help", "back", "show", "back", "list", "'", "import", f'"{self.fixture}"',
                  "list", "--kind bank", "export", f'--format json --output "{output}"', "stats", "quit"]
        with patch("builtins.input", side_effect=inputs), patch("os.system", side_effect=AssertionError("No shell")), patch("subprocess.run", side_effect=AssertionError("No subprocess")):
            status, out, err = self.invoke("menu")
        self.assertEqual(status, 0)
        results = [json.loads(line) for line in out.splitlines()]
        self.assertEqual(results[0]["entities"], 2)
        self.assertEqual(results[1][0]["name"], "Example Bank")
        self.assertEqual(results[2]["exported"], 2)
        self.assertEqual(results[3]["entities"], 2)
        self.assertIn("Invalid quoting", err)
        self.assertTrue(output.exists())
        for label in MENU_LABELS.values():
            self.assertIn(label, err)
        with patch("builtins.input", side_effect=["1", "quit"]):
            self.assertEqual(self.invoke("menu")[0], 0)
        with patch("builtins.input", side_effect=["import", "quit"]):
            self.assertEqual(self.invoke("menu")[0], 0)

    def test_cancelled_operation_returns_one(self):
        with patch("lele.core.registry.init_db", side_effect=KeyboardInterrupt):
            status, out, err = self.invoke("init")
        self.assertEqual((status, out), (1, ""))
        self.assertIn("cancelled", err)

    def test_module_smoke(self):
        for args in (["--help"], ["--json", "init"], ["--json", "stats"], ["show", "999"], ["show", "bad"]):
            with self.subTest(args=args):
                result = subprocess.run([sys.executable, "-m", "lele", "--db", str(self.db), *args],
                                        cwd=Path(__file__).resolve().parents[1], input="", capture_output=True, text=True, timeout=15)
                expected = 1 if args == ["show", "999"] else 2 if args == ["show", "bad"] else 0
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                if "--json" in args:
                    self.assertIsInstance(json.loads(result.stdout), dict)


if __name__ == "__main__":
    unittest.main()
