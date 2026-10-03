import hashlib
import json
import sqlite3
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from lele.core import db
from lele.core.constants import OSFI_DATASET_URL, OSFI_RESOURCE_ID, SOURCES
from lele.core.registry import _SCHEMA, entity_payload, upsert_entity
from lele.fetchers import http, sources
from lele.fetchers.sources import SourceError, fetch_source


FEDERAL = "Federally Regulated Financial Institutions"
REP = "Foreign Bank Representative Offices"
ROW = {
    "_id": 2100, "Company Name": "Example Bank", "FI Type Name": FEDERAL,
    "FI Group Name": "Banks", "FI Industry Name": "Domestic Banks",
    "Canadian Trade Company Name": None, "City": "Toronto", "Province State": "Ontario",
}


def row(name="Example Bank", group="Banks", industry="Domestic Banks", fi_type=FEDERAL, row_id=2100):
    return dict(ROW, **{"Company Name": name, "FI Group Name": group,
                        "FI Industry Name": industry, "FI Type Name": fi_type, "_id": row_id})


def page(rows, total=None, estimated=False):
    return {"success": True, "result": {"resource_id": OSFI_RESOURCE_ID,
            "records": rows, "total": len(rows) if total is None else total,
            "total_was_estimated": estimated}}


class OSFITests(unittest.TestCase):
    def setUp(self):
        # The project's connection type, so the fetch can register a failure recorder
        # the way every real caller's connection lets it.
        self.conn = sqlite3.connect(":memory:", factory=db.Connection)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(_SCHEMA)
        self.addCleanup(self.conn.close)
        self.client = Mock(warnings=[], retrieved_at="2026-09-17T00:00:00+00:00")
        patcher = patch.object(sources, "HTTPClient", return_value=self.client)
        self.factory = patcher.start()
        self.addCleanup(patcher.stop)

    def fetch(self, rows, **kwargs):
        self.client.get_json.return_value = page(rows)
        return fetch_source(self.conn, "osfi", **kwargs)

    def entities(self):
        return [entity_payload(self.conn, r[0]) for r in self.conn.execute(
            "SELECT id FROM entities WHERE key LIKE 'osfi:%' ORDER BY id")]

    def params(self, index=-1):
        return parse_qs(urlsplit(self.client.get_json.call_args_list[index].args[0]).query)

    def test_mapping_and_exact_evidence(self):
        rows = [row(industry=industry) for industry in (
            "Domestic Banks", "Foreign Banks", "Foreign Bank Branches - Full Service",
            "Foreign Bank Branches - Lending")]
        rows += [row(group=REP, industry=REP, fi_type=REP),
                 row("ACTRA Fraternal Benefit Society", "Fraternal Benefit Societies", "Canadian Fraternal Benefit Societies"),
                 row("Example Insurer", "Property & Casualty Insurance Companies", "Canadian Mortgage Insurers"),
                 row("Example Life", "Life Insurance Companies", "Foreign Life Insurance Companies"),
                 row("Example Trust", "Trust Companies", "Trust Companies"),
                 row("Example Loan", "Loan Companies", "Loan Companies")]
        result = self.fetch(rows)
        self.assertEqual(set(result), {"source", "fetched", "stored", "warnings", "truncated", "total", "pages", "coverage"})
        self.assertEqual((result["stored"], result["fetched"], result["total"]), (10, 10, 10))
        self.assertFalse(result["truncated"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM entities").fetchone()[0], 11)
        authority = self.conn.execute("SELECT * FROM entities WHERE key='authority:ca:osfi'").fetchone()
        self.assertEqual((authority["name"], authority["kind"], authority["country"]),
                         ("Office of the Superintendent of Financial Institutions", "regulator", "CA"))
        entities = self.entities()
        self.assertEqual([e["kind"] for e in entities], ["bank", "bank", "bank_branch", "bank_branch",
                         "representative_office", "fraternal_benefit_society", "insurance_company",
                         "insurance_company", "trust_company", "loan_company"])
        for entity, original in zip(entities, rows):
            attrs = entity["attributes"]
            self.assertEqual((entity["country"], attrs["country_basis"]), ("CA", "regulatory_jurisdiction"))
            self.assertEqual(attrs["source"], "osfi")
            self.assertEqual(attrs["source_dataset"], OSFI_DATASET_URL)
            self.assertEqual(attrs["source_license"], "ca-ogl-lgo")
            self.assertEqual(attrs["source_frequency"], "monthly")
            self.assertEqual(attrs["osfi.resource_id"], OSFI_RESOURCE_ID)
            self.assertEqual(attrs["osfi.row_id"], "2100")
            self.assertEqual(attrs["osfi.city"], "Toronto")
            self.assertEqual(attrs["osfi.province_state"], "Ontario")
            self.assertIn("sha256", attrs["identity_basis"])
            self.assertEqual(attrs["source_url"], self.client.get_json.call_args.args[0])
            self.assertIsNone(entity["website"])
            self.assertNotIn("source_id", attrs)
            if entity["kind"] == "representative_office":
                self.assertEqual(entity["relationships"], [])
                continue
            edge, = entity["relationships"]
            self.assertEqual((edge["dir"], edge["rel"], edge["other_id"]), ("out", "regulated_by", authority["id"]))
            self.assertEqual(edge["source_url"], OSFI_DATASET_URL)
            self.assertEqual(edge["observed_at"], attrs["retrieved_at"])
            self.assertIn("OSFI list entry", edge["evidence"])
            self.assertIn("not universal", edge["evidence"])
            for field in ("FI Type Name", "FI Group Name", "FI Industry Name"):
                self.assertIn(original[field], edge["evidence"])
        attrs = entity_payload(self.conn, authority["id"])["attributes"]
        self.assertEqual(attrs["source_license"], "ca-ogl-lgo")
        self.assertEqual(attrs["source_dataset"], OSFI_DATASET_URL)
        self.assertTrue(any("excluding the regulator" in w for w in result["warnings"]))
        self.assertTrue(any("no automatic supervision" in w for w in result["warnings"]))

    def test_identity_row_ids_duplicates_and_renames(self):
        self.fetch([ROW, row(group=REP, industry=REP, fi_type=REP)])
        first, representative = self.entities()
        identity = json.dumps(tuple(ROW[k].lower() for k in (
            "Company Name", "FI Type Name", "FI Group Name", "FI Industry Name")), separators=(",", ":"))
        self.assertEqual(first["key"], "osfi:" + hashlib.sha256(identity.encode()).hexdigest())
        self.assertNotEqual(first["key"], representative["key"])
        result = self.fetch([row(" EXAMPLE   BANK ", row_id=999), row(row_id=1000)])
        self.assertEqual(result["stored"], 1)
        updated = self.entities()[0]
        self.assertEqual(updated["id"], first["id"])
        self.assertEqual(updated["attributes"]["osfi.row_id"], "999")
        self.assertEqual(len(updated["relationships"]), 1)
        self.assertTrue(any("Skipped 1" in w for w in result["warnings"]))
        self.assertTrue(any("renames are not resolved" in w for w in result["warnings"]))
        self.fetch([row("Renamed Bank", row_id=999)])
        self.assertEqual(len(self.entities()), 3)
        self.fetch([])
        self.assertEqual(len(self.entities()), 3)

    def test_query_and_country_validation(self):
        query = 'A & "Bank" + B/é?offset=900#x'
        for country in ("", "CA", " ca ", "CAN"):
            self.fetch([], query=query, country=country)
            params = self.params()
            self.assertEqual(params["q"], [query])
            self.assertEqual(params["resource_id"], [OSFI_RESOURCE_ID])
            self.assertEqual(params["offset"], ["0"])
            self.assertEqual(params["limit"], ["25"])
            self.assertEqual(params["sort"], ["_id asc"])
            self.assertEqual(params["plain"], ["true"])
            self.assertNotIn("country", params)
            self.assertEqual(http.validate_url(self.client.get_json.call_args.args[0]), "open.canada.ca")
        self.factory.reset_mock()
        for kwargs in [dict(country=c) for c in ("US", "GB", "CA;US", "Canada")] + [
            dict(limit=v) for v in (0, -1, 1001, True, "25", 1.5)
        ] + [dict(query="a\nb"), dict(query="x" * 201)]:
            with self.subTest(kwargs=kwargs), self.assertRaises(SourceError):
                fetch_source(self.conn, "osfi", **kwargs)
        self.factory.assert_not_called()
        for url in (OSFI_DATASET_URL, "https://open.canada.ca/data/api/3/action/datastore_search_sql",
                    "https://open.canada.ca/data/api/3/action/package_search"):
            with self.assertRaises(SourceError):
                http.validate_url(url)
        self.assertEqual(SOURCES["OSFI"], "https://open.canada.ca/data/api/3/action/datastore_search")

    def test_short_offset_pages_and_truncation(self):
        self.client.get_json.side_effect = [page([row(str(n))], 3) for n in range(3)]
        result = fetch_source(self.conn, "osfi", limit=3)
        self.assertEqual((result["fetched"], result["stored"], result["pages"]), (3, 3, 3))
        self.assertFalse(result["truncated"])
        self.assertEqual([self.params(n)["offset"] for n in range(3)], [["0"], ["1"], ["2"]])
        self.client.get_json.side_effect = [page([row("4"), row("5")], 5), page([row("6"), row("7")], 5)]
        with patch.object(sources, "FETCH_PAGE_SIZE", 2):
            result = fetch_source(self.conn, "osfi", limit=3)
        self.assertEqual((result["fetched"], result["stored"], result["pages"]), (3, 3, 2))
        self.assertTrue(result["truncated"])
        self.assertEqual(self.params()["offset"], ["2"])

    def test_estimated_total_does_not_stop_early(self):
        self.client.get_json.side_effect = [page([row(str(n))], 1, True) for n in range(3)]
        result = fetch_source(self.conn, "osfi", limit=3)
        self.assertEqual((result["pages"], result["stored"]), (3, 3))
        self.assertTrue(result["truncated"])
        self.assertTrue(any("estimated" in w for w in result["warnings"]))

    def test_invalid_required_fields_optional_row_id_and_unknown_categories(self):
        bad = [None, {}, row(row_id=0), row(row_id=-1), row(row_id=True), row(row_id="bad")]
        for field in ("Company Name", "FI Type Name", "FI Group Name", "FI Industry Name"):
            for value in (None, " ", 12, [], "bad\x00field"):
                bad.append(dict(ROW, **{field: value}))
        no_id = dict(ROW)
        del no_id["_id"]
        unknown = row("Unknown", "Unrecognized group", "Unrecognized industry", "Unknown type")
        result = self.fetch(bad + [no_id, unknown], limit=100)
        self.assertEqual(result["stored"], 2)
        self.assertTrue(any(f"Skipped {len(bad)}" in w for w in result["warnings"]))
        self.assertTrue(any("Unknown OSFI classification" in w for w in result["warnings"]))
        entities = self.entities()
        self.assertNotIn("osfi.row_id", entities[0]["attributes"])
        self.assertEqual(entities[1]["kind"], "financial_institution")
        self.assertEqual(entities[1]["relationships"], [])
        self.assertEqual(entities[1]["attributes"]["osfi.fi_industry"], "Unrecognized industry")

    def test_malformed_pages_and_late_failure_zero_writes(self):
        malformed = [None, [], {}, {"success": False}, {"success": 1, "result": page([])["result"]}]
        for field, value in (("resource_id", "wrong"), ("records", None), ("records", {}),
                             ("total", -1), ("total", True), ("total_was_estimated", None),
                             ("total_was_estimated", "false")):
            payload = page([])
            payload["result"][field] = value
            malformed.append(payload)
        for field in ("resource_id", "records", "total", "total_was_estimated"):
            payload = page([])
            del payload["result"][field]
            malformed.append(payload)
        malformed += [{"success": True, "result": None}, SourceError("offline")]
        upsert_entity(self.conn, "bank", "Caller", key="manual:caller")
        for payload in malformed:
            with self.subTest(payload=payload):
                self.client.get_json.side_effect = [page([ROW], 2), payload]
                with self.assertRaisesRegex(SourceError, "no ingestion writes applied"):
                    fetch_source(self.conn, "osfi", limit=2)
                self.assertTrue(self.conn.in_transaction)
                self.assertEqual(self.conn.execute("SELECT key FROM entities").fetchall()[0][0], "manual:caller")
                self.assertEqual(self.conn.execute("SELECT count(*) FROM entities").fetchone()[0], 1)
                self.assertEqual(self.conn.execute("SELECT count(*) FROM edges").fetchone()[0], 0)
                self.assertEqual(self.conn.execute("SELECT count(*) FROM attributes").fetchone()[0], 0)

    def test_empty_repeated_pages_and_page_budget(self):
        self.assertEqual(self.fetch([])["stored"], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM entities").fetchone()[0], 0)
        self.client.get_json.side_effect = [page([ROW], 4), page([], 4)]
        self.assertTrue(fetch_source(self.conn, "osfi")["truncated"])
        self.client.get_json.side_effect = [page([ROW], 4), page([ROW], 4)]
        result = fetch_source(self.conn, "osfi")
        self.assertTrue(any("repeated" in w for w in result["warnings"]))
        self.assertTrue(result["truncated"])
        self.client.get_json.side_effect = [page([ROW], 4)]
        with patch.object(sources, "FETCH_MAX_PAGES", 1):
            result = fetch_source(self.conn, "osfi")
        self.assertTrue(any("budget" in w for w in result["warnings"]))
        self.assertTrue(result["truncated"])

    def test_maximum_budget_and_page_timestamps(self):
        def response(url):
            params = parse_qs(urlsplit(url).query)
            start = int(params["offset"][0])
            self.assertEqual(params["limit"], ["100"])
            self.client.retrieved_at = f"2026-09-17T00:{start // 100:02}:00+00:00"
            return page([row(str(n), row_id=n + 1) for n in range(start, start + 100)], 2000)

        self.client.get_json.side_effect = response
        result = fetch_source(self.conn, "osfi", limit=1000)
        self.assertEqual((result["stored"], result["fetched"], result["pages"]), (1000, 1000, 10))
        self.assertTrue(result["truncated"])
        entities = self.entities()
        self.assertEqual(entities[0]["relationships"][0]["observed_at"], "2026-09-17T00:00:00+00:00")
        self.assertEqual(entities[-1]["relationships"][0]["observed_at"], "2026-09-17T00:09:00+00:00")

    def test_edge_failure_rolls_back_authority_and_institutions(self):
        upsert_entity(self.conn, "bank", "Caller", key="manual:caller")
        with patch.object(sources, "add_edge", side_effect=sqlite3.OperationalError("locked")):
            with self.assertRaisesRegex(SourceError, "rolled back"):
                self.fetch([ROW])
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM entities").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM attributes").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM edges").fetchone()[0], 0)

    def test_representative_override_never_creates_regulator_edge(self):
        result = self.fetch([row(group=REP, industry=REP)])
        self.assertEqual(result["stored"], 1)
        self.assertEqual(self.entities()[0]["kind"], "representative_office")
        self.assertEqual(self.entities()[0]["relationships"], [])

    def test_changed_total_and_cache_warnings(self):
        self.client.warnings = ["HTTP cache warning"]
        self.client.get_json.side_effect = [page([ROW], 2), page([row("Other")], 3), page([row("Third")], 3)]
        result = fetch_source(self.conn, "osfi")
        self.assertEqual(result["stored"], 3)
        self.assertIn("HTTP cache warning", result["warnings"])
        self.assertTrue(any("total changed" in w for w in result["warnings"]))


if __name__ == "__main__":
    unittest.main()
