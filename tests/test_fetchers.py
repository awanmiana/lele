import copy
import io
import json
import os
import sqlite3
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from finworld.core.constants import SOURCES
from finworld.core.registry import _SCHEMA, entity_payload, upsert_entity
from finworld.fetchers import http, sources
from finworld.fetchers.sources import SourceError, fetch_source, list_sources


LEI = "01ERPZV3DOLNXY2MLB90"
INDICATOR = "NY.GDP.MKTP.CD"
GLEIF_ROW = {
    "id": LEI,
    "attributes": {
        "lei": LEI,
        "entity": {
            "legalName": {"name": "Example Trust"},
            "legalAddress": {"country": "US"},
            "jurisdiction": "US-IL", "category": "GENERAL", "status": "ACTIVE",
        },
        "registration": {"status": "ISSUED", "lastUpdateDate": "2025-01-01T00:00:00Z"},
    },
}
FDIC_ROW = {
    "data": {"CERT": 14, "NAME": "Example Bank", "ACTIVE": 1, "INSFDIC": 1,
             "STALP": "MA", "CITY": "Boston", "WEBADDR": "www.example.test",
             "BKCLASS": "SM", "REGAGNT": "FED", "DATEUPDT": "11/18/2024"},
}
WB_ROW = {
    "indicator": {"id": INDICATOR, "value": "GDP (current US$)"},
    "country": {"id": "US", "value": "United States"},
    "countryiso3code": "USA", "date": "2024", "value": 100.5,
    "unit": "", "obs_status": "", "decimal": 0,
}


def page(source, rows, total=None):
    total = len(rows) if total is None else total
    if source == "gleif":
        return {"meta": {"pagination": {"total": total}}, "data": rows}
    if source == "fdic":
        return {"meta": {"total": total}, "data": rows}
    return [{"page": 1, "pages": 1, "total": total, "lastupdated": "2025-01-01"}, rows]


def sec_submissions():
    return {
        "cik": 320193, "name": "Example Issuer", "entityType": "operating",
        "tickers": ["AAPL"], "website": " https://www.example.test ",
        "filings": {"recent": {
            "form": ["10-K", "8-K", "10-Q"],
            "filingDate": ["2024-11-01", "2025-02-02", "2025-01-01"],
            "accessionNumber": ["0000320193-24-000001", "0000320193-25-000003", "0000320193-25-000002"],
            "primaryDocument": ["annual.htm", "xslF345X06/form4.xml", "quarter.htm"],
        }, "files": []},
    }


def sec_fact(value, end="2024-12-31", filed="2025-02-01", **kwargs):
    return {"val": value, "end": end, "filed": filed, **kwargs}


def fdic_row(cert):
    row = copy.deepcopy(FDIC_ROW)
    row["data"]["CERT"] = cert
    return row


class FetcherTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.addCleanup(self.conn.close)
        self.client = Mock(warnings=[], retrieved_at="2025-02-01T00:00:00+00:00")
        patcher = patch.object(sources, "HTTPClient", return_value=self.client)
        self.factory = patcher.start()
        self.addCleanup(patcher.stop)

    def fetch(self, source, rows, **kwargs):
        self.client.get_json.return_value = page(source, rows)
        return fetch_source(self.conn, source, **kwargs)

    def payload(self):
        row = self.conn.execute("SELECT id FROM entities ORDER BY id").fetchone()
        return entity_payload(self.conn, row["id"])

    def params(self, call=-1):
        url = self.client.get_json.call_args_list[call].args[0]
        return parse_qs(urlsplit(url).query)

    def test_catalog(self):
        catalog = list_sources()
        self.assertEqual({s["id"] for s in catalog}, {"gleif", "fdic", "worldbank", "osfi", "sec"})
        for item in catalog:
            self.assertEqual(set(item), {"id", "name", "coverage", "status", "url"})
            self.assertEqual(item["status"], "available")
            http.validate_url(item["url"])
        catalog[0]["id"] = "changed"
        self.assertEqual(list_sources()[0]["id"], "gleif")

    def test_gleif_mapping_provenance_and_idempotence(self):
        result = self.fetch("gleif", [GLEIF_ROW], query="Example & Trust", country="us")
        self.assertEqual((result["fetched"], result["stored"], result["truncated"]), (1, 1, False))
        data = self.payload()
        self.assertEqual((data["key"], data["kind"], data["lei"]), (f"lei:{LEI}", "legal_entity", LEI))
        self.assertEqual(data["attributes"]["iso_jurisdiction"], "US-IL")
        self.assertEqual(data["attributes"]["source_id"], LEI)
        self.assertEqual(data["attributes"]["source"], "gleif")
        self.assertNotIn("gleif.sub_category", data["attributes"])
        self.assertNotIn("gleif.associated_lei", data["attributes"])
        self.assertNotIn("gleif.associated_name", data["attributes"])
        self.assertTrue(data["attributes"]["retrieved_at"])
        self.assertEqual(data["metrics"], [])
        self.assertEqual(self.params()["filter[entity.legalName]"], ["Example & Trust"])
        self.assertEqual(self.params()["filter[entity.legalAddress.country]"], ["US"])
        row = copy.deepcopy(GLEIF_ROW)
        row["attributes"]["entity"]["legalName"]["name"] = "Renamed Trust"
        self.fetch("gleif", [row], query=LEI.lower())
        self.assertEqual(self.params()["filter[lei]"], [LEI])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM entities").fetchone()[0], 1)
        self.assertEqual(self.payload()["name"], "Renamed Trust")

    def test_gleif_category_website_set_clear_and_reclassification(self):
        row = copy.deepcopy(GLEIF_ROW)
        self.fetch("gleif", [row])
        for category, kind in (("FUND", "fund"), ("SOLE_PROPRIETOR", "legal_entity")):
            row["attributes"]["entity"].update(category=category, website=" https://example.test/path ")
            self.fetch("gleif", [row], category=category)
            self.assertEqual(self.params()["filter[entity.category]"], [category])
            self.assertEqual(self.payload()["kind"], kind)
            self.assertEqual(self.payload()["website"], "example.test/path")
            self.assertEqual(self.payload()["attributes"]["gleif.website"], "https://example.test/path")
        for value in (None, 12, {}, "example.test", "ftp://example.test", "https://", "https://bad host", "http://[broken"):
            with self.subTest(value=value):
                row["attributes"]["entity"]["website"] = value
                self.fetch("gleif", [row])
                self.assertIsNone(self.payload()["website"])
        del row["attributes"]["entity"]["website"]
        self.fetch("gleif", [row])
        self.assertNotIn("gleif.website", self.payload()["attributes"])
        row["attributes"]["entity"]["website"] = " HTTP://example.test "
        self.fetch("gleif", [row])
        self.assertEqual(self.payload()["website"], "example.test")
        row["attributes"]["entity"]["subCategory"] = "Private Equity"
        row["attributes"]["entity"]["associatedEntity"] = {"lei": LEI, "name": " Manager GmbH "}
        self.fetch("gleif", [row])
        attributes = self.payload()["attributes"]
        self.assertEqual(attributes["gleif.sub_category"], "Private Equity")
        self.assertEqual(attributes["gleif.associated_lei"], LEI)
        self.assertEqual(attributes["gleif.associated_name"], "Manager GmbH")

    def test_sec_submissions_filings_limit_provenance_and_ticker(self):
        self.client.get_json.return_value = sec_submissions()
        result = fetch_source(self.conn, "sec", query="0000320193", limit=2)
        self.assertEqual(set(result), {"source", "fetched", "stored", "warnings", "truncated", "total", "pages", "coverage"})
        self.assertEqual((result["fetched"], result["stored"], result["pages"], result["total"]), (1, 1, 1, 1))
        self.assertTrue(result["truncated"])
        data = self.payload()
        self.assertEqual((data["key"], data["name"], data["kind"]), ("cik:0000320193", "Example Issuer", "company"))
        self.assertEqual(data["website"], "https://www.example.test")
        self.assertIsNone(data["country"])
        self.assertEqual(data["attributes"]["sec.tickers"], "AAPL")
        self.assertEqual(data["attributes"]["sec.website"], "https://www.example.test")
        self.assertEqual(data["attributes"]["source_url"], SOURCES["SEC_SUBMISSIONS"] + "/CIK0000320193.json")
        self.assertEqual(data["filings"], [
            {"form": "8-K", "date": "2025-02-02", "title": "xslF345X06/form4.xml", "url": "https://www.sec.gov/Archives/edgar/data/320193/000032019325000003/xslF345X06/form4.xml", "source": "sec"},
            {"form": "10-Q", "date": "2025-01-01", "title": "quarter.htm", "url": "https://www.sec.gov/Archives/edgar/data/320193/000032019325000002/quarter.htm", "source": "sec"},
        ])
        self.assertEqual(data["metrics"], [])
        self.factory.assert_called_once_with()
        fetch_source(self.conn, "sec", query="aapl", limit=1)
        self.assertEqual(len(self.payload()["filings"]), 2)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM entities").fetchone()[0], 1)

    def test_sec_nonoperating_types_and_website_clear(self):
        payload = sec_submissions()
        for entity_type in ("operating", "asset-backed", "etf", "investment_trust", "fund", "issuer", "unknown"):
            payload["entityType"] = entity_type
            payload["website"] = "https://example.test" if entity_type == "operating" else None
            payload["tickers"] = []
            self.client.get_json.return_value = payload
            fetch_source(self.conn, "sec", query="0000320193")
            self.assertEqual(self.payload()["kind"], "company" if entity_type == "operating" else "issuer")
            self.assertEqual(self.payload()["attributes"]["sec.entity_type"], entity_type)
            self.assertEqual(self.payload()["attributes"]["sec.tickers"], "")
            if entity_type != "operating":
                self.assertIsNone(self.payload()["website"])
                self.assertNotIn("sec.website", self.payload()["attributes"])
        payload["website"] = "www.example.test"
        fetch_source(self.conn, "sec", query="0000320193")
        self.assertIsNone(self.payload()["website"])
        self.assertEqual(self.payload()["attributes"]["sec.website"], "www.example.test")

    def test_sec_financials_latest_usd_missing_and_exact_ties(self):
        gaap = {
            "Assets": {"units": {"USD": [sec_fact(100, form="10-K"), sec_fact(200, filed="2025-02-02", form="10-Q", frame="CY2024Q4I"), sec_fact(999, end="2023-12-31", filed="2026-01-01")], "EUR": [sec_fact(9000, end="2026-01-01")]}},
            "Liabilities": {"units": {"EUR": [sec_fact(99)]}},
            "StockholdersEquity": {"units": {"USD": [sec_fact(40)]}},
            "NetIncomeLoss": {"units": {"USD": [sec_fact(-5), sec_fact(-4)]}},
            "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [sec_fact(80)]}},
            "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [sec_fact(0), sec_fact(True, end="2026-01-01"), sec_fact(float("nan"))]}},
        }
        facts = {"cik": 320193, "facts": {"us-gaap": gaap}}
        self.client.get_json.side_effect = [sec_submissions(), facts]
        result = fetch_source(self.conn, "sec", query="0000320193", financials=True)
        self.assertEqual(result["pages"], 2)
        self.assertEqual(self.factory.call_args.kwargs, {"max_bytes": 20 * 1024 * 1024})
        data = self.payload()
        values = {m["k"]: m["v"] for m in data["metrics"]}
        self.assertEqual(values, {"total_assets": "200", "total_equity": "40", "net_income": "-5", "revenue": "80", "cash": "0"})
        self.assertTrue(all(m["source"] == "sec" and m["period"] == "2024-12-31" for m in data["metrics"]))
        self.assertEqual(data["attributes"]["sec.facts_missing"], "Liabilities")
        self.assertEqual(data["attributes"]["sec.facts_retrieved"], "1")
        self.assertEqual(data["attributes"]["sec.facts_url"], SOURCES["SEC_FACTS"] + "/CIK0000320193.json")
        gaap["NetIncomeLoss"]["units"]["USD"].reverse()
        gaap["Revenues"] = {"units": {"USD": [sec_fact(90)]}}
        self.client.get_json.side_effect = [sec_submissions(), facts]
        fetch_source(self.conn, "sec", query="0000320193", financials=True)
        values = {m["k"]: m["v"] for m in self.payload()["metrics"]}
        self.assertEqual((values["net_income"], values["revenue"]), ("-5", "90"))
        self.assertEqual(len(values), 5)

    def test_sec_revenue_prefers_current_tag_and_longest_duration(self):
        gaap = {
            "Assets": {"units": {"USD": [sec_fact(383266000000, end="2026-06-27", filed="2026-07-31")]}},
            "NetIncomeLoss": {"units": {"USD": [
                sec_fact(101464000000, start="2025-09-28", end="2026-06-27", filed="2026-07-31"),
                sec_fact(29789000000, start="2026-03-29", end="2026-06-27", filed="2026-07-31"),
            ]}},
            "Revenues": {"units": {"USD": [sec_fact(62900000000, start="2018-07-01", end="2018-09-29", filed="2018-11-05")]}},
            "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
                sec_fact(364357000000, start="2025-09-28", end="2026-06-27", filed="2026-07-31"),
                sec_fact(109417000000, start="2026-03-29", end="2026-06-27", filed="2026-07-31"),
            ]}},
        }
        facts = {"cik": 320193, "facts": {"us-gaap": gaap}}
        self.client.get_json.side_effect = [sec_submissions(), facts]
        fetch_source(self.conn, "sec", query="0000320193", financials=True)
        metrics = {m["k"]: (m["v"], m["period"]) for m in self.payload()["metrics"]}
        self.assertEqual(metrics["revenue"], ("364357000000", "2026-06-27"))
        self.assertEqual(metrics["net_income"], ("101464000000", "2026-06-27"))
        self.assertNotIn("total_liabilities", metrics)
        self.assertEqual(set(metrics), {"total_assets", "net_income", "revenue"})

    def test_sec_invalid_arguments_and_unresolved_tickers_no_http(self):
        cases = [dict(query=q) for q in ("", "BRK.B", "A1", "ABCDEFGHIJK", "320193", "../AAPL")]
        cases += [dict(query="AAPL", country="US"), dict(query="AAPL"), dict(query="0000320193", financials=1)]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(SourceError):
                fetch_source(self.conn, "sec", **kwargs)
        for kwargs in (dict(source="fdic", category="FUND"), dict(source="gleif", category="VC"),
                       dict(source="gleif", financials=True), dict(source="gleif", category=None)):
            with self.subTest(kwargs=kwargs), self.assertRaises(SourceError):
                fetch_source(self.conn, **kwargs)
        self.factory.assert_not_called()

    def test_sec_malformed_submissions_and_facts_are_atomic(self):
        upsert_entity(self.conn, "bank", "Caller", key="manual:1")
        malformed = [None, {}, dict(sec_submissions(), cik=1), dict(sec_submissions(), tickers="AAPL"),
                     dict(sec_submissions(), name=""), dict(sec_submissions(), filings={"recent": {}})]
        mismatch = sec_submissions()
        mismatch["filings"]["recent"]["form"].pop()
        malformed.append(mismatch)
        bad_row = sec_submissions()
        bad_row["filings"]["recent"]["filingDate"][2] = "2025-02-30"
        malformed.append(bad_row)
        for payload in malformed:
            self.client.get_json.return_value = payload
            with self.subTest(payload=payload), self.assertRaisesRegex(SourceError, "no ingestion writes applied"):
                fetch_source(self.conn, "sec", query="0000320193", limit=1)
        for error in (SourceError("response exceeds byte limit"), {"cik": 1, "facts": {}}, {}):
            self.client.get_json.side_effect = [sec_submissions(), error]
            with self.subTest(error=error), self.assertRaisesRegex(SourceError, "no ingestion writes applied"):
                fetch_source(self.conn, "sec", query="0000320193", financials=True)
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM entities").fetchone()[0], 1)
        for table in ("attributes", "filings", "metrics"):
            self.assertEqual(self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)

    def test_sec_write_failure_rolls_back_and_historical_warning(self):
        upsert_entity(self.conn, "bank", "Caller", key="manual:1")
        payload = sec_submissions()
        payload["filings"]["files"] = [{"name": "older.json"}]
        self.client.get_json.return_value = payload
        with patch.object(sources, "add_filing", side_effect=sqlite3.OperationalError("locked")):
            with self.assertRaisesRegex(SourceError, "rolled back"):
                fetch_source(self.conn, "sec", query="0000320193")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM entities").fetchone()[0], 1)
        result = fetch_source(self.conn, "sec", query="0000320193", limit=1000)
        self.assertTrue(result["truncated"])
        self.assertTrue(any("historical" in w for w in result["warnings"]))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM filings").fetchone()[0], 3)

    def test_fdic_mapping_filter_and_no_website_guess(self):
        self.fetch("fdic", [FDIC_ROW], query="00014", country="USA")
        data = self.payload()
        self.assertEqual((data["key"], data["kind"], data["country"]), ("fdic:14", "bank", "US"))
        self.assertIsNone(data["website"])
        self.assertEqual(data["attributes"]["fdic.reported_website"], "www.example.test")
        self.assertEqual(data["attributes"]["fdic.regulator"], "FED")
        self.assertEqual(data["attributes"]["source_updated_at"], "11/18/2024")
        self.assertEqual(self.params()["filters"], ["ACTIVE:1 AND INSFDIC:1 AND CERT:14"])
        self.assertEqual(data["metrics"], [])
        self.fetch("fdic", [FDIC_ROW], query='First "Bank" OR ACTIVE:0')
        self.assertEqual(self.params()["filters"], ['ACTIVE:1 AND INSFDIC:1 AND NAME:"First \\"Bank\\" OR ACTIVE\\:0"'])

    def test_worldbank_macro_null_zero_and_upsert(self):
        missing = dict(WB_ROW, date="2025", value=None)
        zero = dict(WB_ROW, date="2023", value=0)
        result = self.fetch("worldbank", [missing, WB_ROW, zero], country="US")
        self.assertEqual((result["fetched"], result["stored"]), (3, 2))
        self.assertTrue(any("null" in w for w in result["warnings"]))
        data = self.payload()
        self.assertEqual((data["key"], data["kind"]), ("wb:USA", "economy"))
        self.assertEqual(data["attributes"]["data_scope"], "macro")
        self.assertEqual({m["k"] for m in data["metrics"]}, {"macro:" + INDICATOR})
        self.assertEqual({m["v"] for m in data["metrics"]}, {"100.5", "0"})
        self.fetch("worldbank", [dict(WB_ROW, value=101)], country="USA")
        self.assertEqual(len(self.payload()["metrics"]), 2)
        values = {metric["period"]: metric["v"] for metric in self.payload()["metrics"]}
        self.assertEqual(values, {"2023": "0", "2024": "101"})

    def test_worldbank_aggregate_not_a_bank(self):
        row = dict(WB_ROW, country={"id": "1W", "value": "World"}, countryiso3code="WLD")
        self.fetch("worldbank", [row])
        self.assertEqual(self.payload()["key"], "wb:WLD")
        self.assertEqual(self.payload()["kind"], "economy")
        self.assertIn("/country/all/indicator/", self.client.get_json.call_args.args[0])

    def test_source_keys_do_not_merge_same_name(self):
        gleif = copy.deepcopy(GLEIF_ROW)
        gleif["attributes"]["entity"]["legalName"]["name"] = FDIC_ROW["data"]["NAME"]
        self.fetch("gleif", [gleif])
        self.fetch("fdic", [FDIC_ROW])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM entities").fetchone()[0], 2)

    def test_invalid_arguments_make_no_http_requests(self):
        cases = [dict(source="sec"), dict(source="fdic", country="CA"),
                 dict(source="gleif", country="USA"), dict(source="worldbank", query="bank"),
                 dict(source="worldbank", country="US/../../evil"),
                 dict(source="worldbank", indicator="X?format=xml"),
                 dict(source="fdic", query="a\nb"), dict(source="gleif", query="a" * 201),
                 dict(source=None)]
        cases += [dict(source="fdic", limit=v) for v in (0, -1, 1001, True, "25", 1.5)]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(SourceError):
                fetch_source(self.conn, **kwargs)
        self.factory.assert_not_called()

    def test_empty_sources(self):
        for source in ("gleif", "fdic", "worldbank"):
            result = self.fetch(source, [])
            self.assertEqual(result["stored"], 0)
            self.assertFalse(result["truncated"])
        self.client.get_json.return_value = [{"total": 0}, None]
        self.assertEqual(fetch_source(self.conn, "worldbank")["fetched"], 0)

    def test_malformed_and_api_error_responses(self):
        for source, payload in [("gleif", {"errors": [{"detail": "bad"}]}),
                                ("fdic", {"data": []}), ("fdic", {"meta": {"total": -1}, "data": []}),
                                ("worldbank", [{"message": [{"id": "120"}]}]),
                                ("worldbank", [{"total": 1}, None])]:
            with self.subTest(source=source, payload=payload), self.assertRaises(SourceError):
                self.client.get_json.return_value = payload
                fetch_source(self.conn, source)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM entities").fetchone()[0], 0)

    def test_bad_rows_skipped(self):
        inactive = fdic_row(15)
        inactive["data"]["ACTIVE"] = 0
        result = self.fetch("fdic", [{}, None, inactive, FDIC_ROW, FDIC_ROW])
        self.assertEqual((result["fetched"], result["stored"]), (5, 1))
        self.assertIn("Skipped 4", result["warnings"][0])
        result = self.fetch("gleif", [{"attributes": {"lei": "bad"}}])
        self.assertEqual(result["stored"], 0)
        result = self.fetch("worldbank", [dict(WB_ROW, value=float("nan")), dict(WB_ROW, value=True),
                                          dict(WB_ROW, indicator={"id": "wrong"})])
        self.assertEqual(result["stored"], 0)

    def test_fdic_pagination_and_limit(self):
        self.client.get_json.side_effect = [page("fdic", [fdic_row(1), fdic_row(2)], 10),
                                            page("fdic", [fdic_row(3), fdic_row(4)], 10)]
        with patch.object(sources, "FETCH_PAGE_SIZE", 2):
            result = fetch_source(self.conn, "fdic", limit=3)
        self.assertEqual((result["fetched"], result["stored"], result["pages"]), (3, 3, 2))
        self.assertTrue(result["truncated"])
        self.assertEqual(self.params(0)["offset"], ["0"])
        self.assertEqual(self.params(1)["offset"], ["2"])

    def test_gleif_and_worldbank_page_numbers(self):
        for source, row in (("gleif", GLEIF_ROW), ("worldbank", WB_ROW)):
            self.client.get_json.reset_mock(side_effect=True)
            second = copy.deepcopy(row)
            if source == "gleif":
                second["attributes"]["lei"] = "529900T8BM49AURSDO55"
            else:
                second["date"] = "2023"
            self.client.get_json.side_effect = [page(source, [row], 2), page(source, [second], 2)]
            with patch.object(sources, "FETCH_PAGE_SIZE", 1):
                result = fetch_source(self.conn, source, limit=2)
            field = "page[number]" if source == "gleif" else "page"
            self.assertEqual(self.params(1)[field], ["2"])
            self.assertFalse(result["truncated"])

    def test_repeated_empty_and_page_budget(self):
        self.client.get_json.return_value = page("fdic", [FDIC_ROW], 5)
        result = fetch_source(self.conn, "fdic", limit=5)
        self.assertEqual(result["pages"], 2)
        self.assertTrue(result["truncated"])
        self.assertTrue(any("repeated" in w for w in result["warnings"]))
        self.client.get_json.return_value = page("fdic", [], 5)
        self.assertTrue(fetch_source(self.conn, "fdic")["truncated"])
        self.client.get_json.return_value = page("fdic", [FDIC_ROW], 5)
        with patch.object(sources, "FETCH_MAX_PAGES", 1):
            result = fetch_source(self.conn, "fdic", limit=5)
        self.assertTrue(any("budget" in w for w in result["warnings"]))

    def test_later_failure_does_not_write_or_commit_caller_data(self):
        upsert_entity(self.conn, "bank", "Caller data", key="manual:1")
        self.client.get_json.side_effect = [page("fdic", [FDIC_ROW], 2), SourceError("HTTP 503")]
        with self.assertRaisesRegex(SourceError, "no ingestion writes applied"):
            fetch_source(self.conn, "fdic", limit=2)
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM entities").fetchone()[0], 1)

    def test_database_failure_rolls_back_only_ingestion(self):
        upsert_entity(self.conn, "bank", "Caller data", key="manual:1")
        self.client.get_json.return_value = page("fdic", [FDIC_ROW])
        with patch.object(sources, "set_attr", side_effect=sqlite3.OperationalError("locked")):
            with self.assertRaisesRegex(SourceError, "rolled back"):
                fetch_source(self.conn, "fdic")
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(self.conn.execute("SELECT key FROM entities").fetchone()[0], "manual:1")

    def test_maximum_limit_and_page_budget(self):
        self.client.get_json.side_effect = [
            page("fdic", [fdic_row(n) for n in range(start, start + 100)], 2000)
            for start in range(1, 1001, 100)
        ]
        result = fetch_source(self.conn, "fdic", limit=1000)
        self.assertEqual((result["fetched"], result["stored"], result["pages"]), (1000, 1000, 10))
        self.assertTrue(result["truncated"])

    def test_short_numbered_page_reports_partial_coverage(self):
        self.client.get_json.return_value = page("gleif", [GLEIF_ROW], 200)
        result = fetch_source(self.conn, "gleif", limit=150)
        self.assertEqual(result["pages"], 1)
        self.assertTrue(result["truncated"])
        self.assertTrue(any("short numbered page" in w for w in result["warnings"]))

    def test_cache_warnings_propagate(self):
        self.client.warnings = ["HTTP cache write failed"]
        result = self.fetch("fdic", [FDIC_ROW])
        self.assertIn("HTTP cache write failed", result["warnings"])


class Response(io.BytesIO):
    def __init__(self, raw=b'{}', headers=None, status=200):
        super().__init__(raw)
        self.headers = headers or {}
        self.status = status


class HTTPTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.client = http.HTTPClient(cache_dir=self.temp.name, rate=0)
        self.client.opener = Mock()
        self.url = SOURCES["FDIC"] + "?limit=1"
        http._last_request.clear()

    def respond(self, raw=b'{"data":[]}', headers=None):
        response = Response(raw, headers)
        self.client.opener.open.return_value = response
        return response

    def error(self, code, retry=None):
        headers = Message()
        if retry is not None:
            headers["Retry-After"] = retry
        body = io.BytesIO(b"error")
        return HTTPError(self.url, code, "failure", headers, body), body

    def test_https_and_host_restrictions_before_network(self):
        for url in ("http://api.fdic.gov/banks/institutions", "https://example.org/",
                    "https://api.fdic.gov.evil.test/banks/institutions",
                    "https://api.fdic.gov:444/banks/institutions",
                    "https://user@api.fdic.gov/banks/institutions",
                    "https://api.fdic.gov/banks/institutions-other",
                    "https://api.fdic.gov/banks/institutions#fragment"):
            with self.subTest(url=url), self.assertRaises(SourceError):
                self.client.get_json(url)
        self.client.opener.open.assert_not_called()

    def test_sec_allowlist_headers_pacing_and_source_byte_override(self):
        for base in (SOURCES["SEC_SUBMISSIONS"], SOURCES["SEC_FACTS"]):
            self.assertEqual(http.validate_url(base + "/CIK0000320193.json"), "data.sec.gov")
            with self.assertRaises(SourceError):
                http.validate_url(base + "-other/CIK0000320193.json")
        with self.assertRaises(SourceError):
            http.validate_url("https://www.sec.gov/company_tickers.json")
        self.url = SOURCES["SEC_SUBMISSIONS"] + "/CIK0000320193.json"
        http._last_request["data.sec.gov"] = 10
        self.respond()
        with patch.object(http.time, "monotonic", return_value=10.5), patch.object(http.time, "sleep") as sleep:
            self.client.get_json(self.url)
        sleep.assert_called_once_with(1.0)
        request = self.client.opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Accept"), "application/json")
        self.assertEqual(request.get_header("User-agent"), "finworld/0.1 (public financial data research CLI)")
        self.assertEqual(self.client.max_bytes, 2 * 1024 * 1024)
        larger = http.HTTPClient(cache_dir=None, max_bytes=20 * 1024 * 1024)
        larger.opener = Mock()
        raw = json.dumps({"padding": "x" * (3 * 1024 * 1024)}).encode()
        larger.opener.open.return_value = Response(raw)
        with patch.object(http.time, "sleep"):
            self.assertEqual(len(larger.get_json(SOURCES["SEC_FACTS"] + "/CIK0000320193.json")["padding"]), 3 * 1024 * 1024)
        larger.opener.open.return_value = Response(headers={"Content-Length": str(20 * 1024 * 1024 + 1)})
        with patch.object(http.time, "sleep"), self.assertRaisesRegex(SourceError, "byte limit"):
            larger.get_json(SOURCES["SEC_FACTS"] + "/CIK0000320193.json")

    def test_response_closes_and_cache_hit_does_not_request(self):
        response = self.respond()
        self.assertEqual(self.client.get_json(self.url), {"data": []})
        self.assertTrue(response.closed)
        retrieved_at = self.client.retrieved_at
        self.assertEqual(self.client.get_json(self.url), {"data": []})
        self.assertEqual(self.client.retrieved_at, retrieved_at)
        self.assertEqual(self.client.opener.open.call_count, 1)
        request = self.client.opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Accept-encoding"), "identity")
        self.assertNotIn("contact:", request.get_header("User-agent"))
        self.assertEqual(self.client.opener.open.call_args.kwargs["timeout"], 20)

    def test_corrupt_oversized_and_expired_cache(self):
        path = self.client._cache_path(self.url)
        for raw in (b"broken", b"x" * (self.client.max_bytes + 4097),
                    json.dumps({"url": self.url, "time": 0, "payload": {"old": True}}).encode()):
            path.write_bytes(raw)
            self.respond()
            self.assertEqual(self.client.get_json(self.url), {"data": []})
        self.assertEqual(self.client.opener.open.call_count, 3)
        self.assertTrue(self.client.warnings)

    def test_unwritable_cache_is_nonfatal(self):
        self.respond()
        with patch.object(http.os, "replace", side_effect=PermissionError("denied")):
            self.assertEqual(self.client.get_json(self.url), {"data": []})
        self.assertTrue(any("write failed" in w for w in self.client.warnings))
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_cache_read_permission_error(self):
        self.respond()
        with patch.object(Path, "open", side_effect=PermissionError("denied")):
            self.assertEqual(self.client.get_json(self.url), {"data": []})
        self.assertTrue(any("unreadable" in w for w in self.client.warnings))

    def test_disabled_cache(self):
        self.client.ttl = 0
        self.respond()
        self.client.get_json(self.url)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_cache_storage_has_bounded_slots(self):
        paths = {self.client._cache_path(self.url + f"&offset={i}") for i in range(1000)}
        self.assertLessEqual(len(paths), 128)

    def test_byte_limits_and_content_encoding(self):
        self.client.max_bytes = 10
        for raw, headers in ((b"x" * 11, {}), (b"{}", {"Content-Length": "11"}),
                             (b"{}", {"Content-Length": "bad"}),
                             (b"{}", {"Content-Encoding": "gzip"})):
            response = self.respond(raw, headers)
            with self.subTest(headers=headers), self.assertRaises(SourceError):
                self.client.get_json(self.url)
            self.assertTrue(response.closed)

    def test_invalid_json_not_cached(self):
        for raw in (b"<html>blocked</html>", b"\xff", b"null", b"42", b'{"x":NaN}'):
            response = self.respond(raw)
            with self.subTest(raw=raw), self.assertRaisesRegex(SourceError, "invalid JSON"):
                self.client.get_json(self.url)
            self.assertTrue(response.closed)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_http_errors_closed_and_not_retried_for_403(self):
        error, body = self.error(403)
        self.client.opener.open.side_effect = error
        with self.assertRaisesRegex(SourceError, "HTTP 403"):
            self.client.get_json(self.url)
        self.assertTrue(body.closed)
        self.assertEqual(self.client.opener.open.call_count, 1)

    def test_rate_limit_retries_are_bounded(self):
        errors = [self.error(429, "999999") for _ in range(3)]
        self.client.opener.open.side_effect = [entry[0] for entry in errors]
        with patch.object(http.time, "sleep") as sleep:
            with self.assertRaisesRegex(SourceError, "429"):
                self.client.get_json(self.url)
        self.assertEqual(sleep.call_args_list[0].args, (30,))
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(self.client.opener.open.call_count, 3)
        self.assertTrue(all(body.closed for _, body in errors))

    def test_503_retry_then_success(self):
        error, body = self.error(503)
        response = Response()
        self.client.opener.open.side_effect = [error, response]
        with patch.object(http.time, "sleep"):
            self.assertEqual(self.client.get_json(self.url), {})
        self.assertTrue(body.closed and response.closed)

    def test_timeout_and_network_errors(self):
        for error in (TimeoutError("timed out"), URLError("offline")):
            self.client.opener.open.side_effect = error
            with self.subTest(error=error), self.assertRaisesRegex(SourceError, "network request failed"):
                self.client.get_json(self.url)

    def test_response_deadline(self):
        response = self.respond()
        with patch.object(http.time, "monotonic", side_effect=[0, 0, 0, 21]):
            with self.assertRaisesRegex(SourceError, "deadline"):
                self.client.get_json(self.url)
        self.assertTrue(response.closed)

    def test_host_politeness(self):
        self.client.rate = 1.5
        http._last_request[urlsplit(self.url).hostname] = 10
        self.respond()
        with patch.object(http.time, "monotonic", return_value=10.5), patch.object(http.time, "sleep") as sleep:
            self.client.get_json(self.url)
        sleep.assert_called_once_with(1.0)

    def test_redirect_refused_and_closed(self):
        body = io.BytesIO()
        with self.assertRaisesRegex(SourceError, "redirect refused"):
            http._NoRedirect().redirect_request(None, body, 302, "", {}, "https://example.org/")
        self.assertTrue(body.closed)

    def test_invalid_configuration_and_custom_user_agent(self):
        for kwargs in ({"timeout": 0}, {"max_bytes": 0}, {"rate": -1}, {"ttl": float("nan")}):
            with self.subTest(kwargs=kwargs), self.assertRaises(SourceError):
                http.HTTPClient(**kwargs)
        with patch.dict(os.environ, {"FINWORLD_RATE_LIMIT": "invalid"}), self.assertRaises(SourceError):
            http.HTTPClient()
        with patch.dict(os.environ, {"FINWORLD_USER_AGENT": "research-cli/1.0"}):
            client = http.HTTPClient(cache_dir=None)
        self.assertEqual(client.user_agent, "research-cli/1.0")
        with patch.dict(os.environ, {"FINWORLD_USER_AGENT": "bad\nheader"}), self.assertRaises(SourceError):
            http.HTTPClient()


if __name__ == "__main__":
    unittest.main()
