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
from lele.fetchers import political

RECORDS = [
    {"document_number": "2026-09-17-001", "citation": "91 FR 52345",
     "title": "Proposed Rule: Environmental Protection Standards",
     "type": ["REGULATION-PROPOSED"], "publication_date": "2026-09-17",
     "effective_on": "2026-12-01",
     "government_entity": "Environmental Protection Agency",
     "agencies": ["Environmental Protection Agency"],
     "abstract": "This proposed rule establishes new environmental protection standards.",
     "html_url": "https://www.federalregister.gov/documents/2026/09/17/2026-09-17-001"},
    {"document_number": "2026-09-16-002", "citation": "91 FR 52400",
     "title": "Final Rule: Financial Reporting Requirements",
     "type": ["REGULATION-FINAL"], "publication_date": "2026-09-16",
     "effective_on": "2026-10-01",
     "government_entity": "Securities and Exchange Commission",
     "agencies": ["Securities and Exchange Commission"],
     "abstract": "This final rule updates financial reporting requirements.",
     "html_url": "https://www.federalregister.gov/documents/2026/09/16/2026-09-16-002"},
    {"document_number": "", "citation": "",
     "title": "", "type": ["PRESIDENTIAL-ORDER"],
     "publication_date": "2026-09-15",
     "effective_on": "",
     "government_entity": "Executive Office of the President",
     "agencies": ["Executive Office of the President"],
     "abstract": "",
     "html_url": ""},
]

PAYLOAD = {"count": len(RECORDS), "results": RECORDS, "current_page": 1,
           "total_pages": 1, "per_page": 25, "order": "newest"}


class FakeClient:
    body = None

    def __init__(self, *args, **kwargs):
        self.retrieved_at = "2026-09-19T00:00:00+00:00"
        self.warnings = []

    def get_json(self, url):
        if not url.startswith(SOURCES["FEDERAL_REGISTER"]):
            raise AssertionError(f"unexpected URL {url}")
        FakeClient.body = url
        return PAYLOAD


class PoliticalTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db = self.root / "registry.db"
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(registry._SCHEMA)
        self.conn.commit()
        self.addCleanup(self.conn.close)

    def test_observation_is_political_event(self):
        observation = political._observation(RECORDS[0], "2026-09-19T00:00:00+00:00")
        self.assertEqual(observation["kind"], "political_event")
        self.assertEqual(observation["actor"], "political:REGULATION-PROPOSED:2026-09-17-001")
        self.assertEqual(observation["reason_basis"], "unknown")
        self.assertEqual(observation["occurred_at"], "2026-09-17")
        self.assertEqual(observation["counterparty"], "")
        self.assertTrue(observation["source_url"].startswith("https://www.federalregister.gov"))
        evidence = json.loads(observation["evidence"])
        self.assertEqual(evidence["document_number"], "2026-09-17-001")
        self.assertEqual(evidence["government_entity"], "Environmental Protection Agency")
        self.assertIn("environmental", evidence["abstract"].lower())

    def test_observation_skips_empty_title(self):
        empty = dict(RECORDS[2], title="")
        self.assertIsNone(political._observation(empty, "2026-09-19T00:00:00+00:00"))

    def test_observation_skips_bad_date(self):
        bad = dict(RECORDS[0], publication_date="not-a-date")
        self.assertIsNone(political._observation(bad, "2026-09-19T00:00:00+00:00"))

    def test_observation_kind_political(self):
        obs = political._observation(RECORDS[0], "2026-09-19T00:00:00+00:00")
        self.assertEqual(obs["kind"], "political_event")

    def test_fetch_political_stores_observations(self):
        with patch.object(political, "HTTPClient", FakeClient):
            report = political.fetch_political(self.conn, limit=10,
                                                   start="2026-09-01", end="2026-09-30")
        self.assertEqual(report["records"], 3)
        self.assertEqual(report["observations"], 2)
        self.assertEqual(report["skipped"], 1)
        self.assertIn("political_event", report["by_kind"])
        rows = registry.list_observations(self.conn)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["kind"], "political_event")
        runs = registry.list_ingest_runs(self.conn)
        self.assertEqual(runs[0]["source"], "political-event")
        self.assertEqual(runs[0]["stored"], 2)
        body = FakeClient.body
        self.assertIn("conditions%5Bpublication_date%5D%5Bgte%5D=2026-09-01", body)

    def test_fetch_political_rejections(self):
        with self.assertRaises(ValueError):
            political.fetch_political(self.conn, limit=0)
        with self.assertRaises(ValueError):
            political.fetch_political(self.conn, limit=101)
        with self.assertRaises(ValueError):
            political.fetch_political(self.conn, limit=5, start="2026-09-01",
                                          end="2026-08-01")
        with self.assertRaises(ValueError):
            political.fetch_political(self.conn, limit=5, start="01-01-2026")

    def test_cli_dispatch(self):
        with patch("lele.fetchers.political.fetch_political",
                   return_value={"source": "political-event",
                                 "observations": 1}) as fetch:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                status = main(["--db", str(self.db), "--json", "fetch-political",
                                "--limit", "10", "--start", "2026-09-01",
                                "--end", "2026-09-30"])
            self.assertEqual((status, err.getvalue()), (0, ""))
            self.assertEqual(json.loads(out.getvalue())["observations"], 1)
            self.assertEqual(fetch.call_args.args[1], 10)
            self.assertEqual(fetch.call_args.args[2], "2026-09-01")
            self.assertEqual(fetch.call_args.args[3], "2026-09-30")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with patch("lele.fetchers.political.fetch_political",
                       return_value={"source": "political-event",
                                     "observations": 0}):
                status = main(["--db", str(self.db), "fetch-political",
                                "--start", "2026-09-01", "--end", "2026-09-30"])
        self.assertEqual(status, 0)


if __name__ == "__main__":
    unittest.main()
