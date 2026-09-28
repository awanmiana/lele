import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from lele.cli.main import main
from lele.core import registry
from lele.core.constants import SOURCES
from lele.fetchers import lobbying

RECORD = {
    "filing_uuid": "u1", "filing_type": "Q1", "filing_type_display": "Quarterly Activity Report",
    "filing_year": 2024, "filing_period": "first_quarter",
    "filing_period_display": "First Quarter", "dt_posted": "2024-04-20T10:00:00-04:00",
    "income": "10000.00", "expenses": "0.00",
    "registrant": {"id": 1, "name": "LOBBY FIRM"}, "client": {"id": 2, "name": "CLIENT CO"},
    "filing_document_url": "https://lda.senate.gov/filings/u1.pdf",
    "url": "https://lda.gov/api/v1/filings/u1/",
    "lobbying_activities": [{"general_issue_code": "TAX"}],
}
NO_INCOME = dict(RECORD, filing_uuid="u2", income=None)
PER_QUARTER = {"Q1": [RECORD, NO_INCOME], "Q2": [], "Q3": [RECORD], "Q4": []}


class FakeClient:
    requests = []

    def __init__(self, *args, **kwargs):
        self.retrieved_at = "2026-09-19T00:00:00+00:00"
        self.warnings = []

    def get_json(self, url):
        if not url.startswith(SOURCES["SENATE_LDA_FILINGS"] + "/"):
            raise AssertionError(f"unexpected URL {url}")
        FakeClient.requests.append(url)
        for quarter, results in PER_QUARTER.items():
            if f"filing_type={quarter}" in url:
                return {"count": len(results), "results": results}
        raise AssertionError(f"unexpected URL {url}")


class LobbyingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db = self.root / "registry.db"
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(registry._SCHEMA)
        self.eid = registry.upsert_entity(self.conn, "company", "Client Co",
                                          key="cik:0000000001")
        self.conn.commit()
        self.addCleanup(self.conn.close)

    def test_client_binding_produces_routed_flow(self):
        with patch.object(lobbying, "HTTPClient", FakeClient):
            report = lobbying.fetch_lobbying(self.conn, self.eid, client_id=2, year=2024)
        self.assertEqual((report["filings"], report["observations"], report["skipped"]), (3, 1, 2))
        self.assertEqual(report["bound_party"], "client")
        rows = registry.list_observations(self.conn, self.eid)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["kind"], "fund_flow")
        self.assertEqual(row["actor_key"], "cik:0000000001")
        self.assertEqual(row["counterparty_key"], "lda:registrant:1")
        self.assertEqual((row["amount"], row["currency"]), ("10000.00", "USD"))
        self.assertEqual(row["occurred_at"], "2024-01-01")
        self.assertEqual(row["observed_at"], "2024-04-20T14:00:00+00:00")
        self.assertEqual(row["source_url"], "https://lda.senate.gov/filings/u1.pdf")
        evidence = json.loads(row["evidence"])
        self.assertEqual((evidence["registrant"]["name"], evidence["issue_codes"]),
                         ("LOBBY FIRM", ["TAX"]))

    def test_registrant_binding_reverses_direction(self):
        registrant = registry.upsert_entity(self.conn, "company", "Lobby Firm",
                                            key="cik:0000000002")
        with patch.object(lobbying, "HTTPClient", FakeClient):
            report = lobbying.fetch_lobbying(self.conn, registrant, registrant_id=1, year=2024)
        self.assertEqual(report["bound_party"], "registrant")
        row = registry.list_observations(self.conn, registrant)[0]
        self.assertEqual(row["actor_key"], "lda:client:2")
        self.assertEqual(row["counterparty_key"], "cik:0000000002")
        body = FakeClient.requests[-1]
        self.assertIn("registrant_id=1", body)

    def test_rejections(self):
        with self.assertRaises(ValueError):
            lobbying.fetch_lobbying(self.conn, self.eid, client_id=2, registrant_id=1)
        with self.assertRaises(ValueError):
            lobbying.fetch_lobbying(self.conn, self.eid)
        with self.assertRaises(ValueError):
            lobbying.fetch_lobbying(self.conn, self.eid, client_id=0)
        with self.assertRaises(ValueError):
            lobbying.fetch_lobbying(self.conn, self.eid, client_id=2, year=1990)
        with self.assertRaises(ValueError):
            lobbying.fetch_lobbying(self.conn, 999999, client_id=2, year=2024)

    def test_cli_dispatch(self):
        with patch("lele.fetchers.lobbying.fetch_lobbying",
                   return_value={"source": "senate-lda", "observations": 1}) as fetch:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                status = main(["--db", str(self.db), "--json", "fetch-lobbying", str(self.eid),
                               "--client-id", "2", "--filing-year", "2024", "--limit", "5"])
            self.assertEqual((status, err.getvalue()), (0, ""))
            self.assertEqual(json.loads(out.getvalue())["observations"], 1)
            self.assertEqual(fetch.call_args.args[2], 2)
            self.assertEqual(fetch.call_args.args[5], 2024)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--db", str(self.db), "fetch-lobbying", str(self.eid)]), 2)


if __name__ == "__main__":
    unittest.main()
