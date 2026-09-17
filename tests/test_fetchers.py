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
        self.assertEqual({s["id"] for s in catalog}, {"gleif", "fdic", "worldbank", "osfi"})
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
