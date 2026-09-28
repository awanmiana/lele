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
from lele.fetchers import treasury

RECORDS = [
    {"record_date": "2026-09-17", "open_today_bal": "991708",
     "open_month_bal": "1023554", "open_fiscal_year_bal": "890825",
     "table_nbr": "I", "table_nm": "Operating Cash Balance",
     "sub_table_name": "Cash Balance Details", "src_line_nbr": "1",
     "record_fiscal_year": "2026", "record_calendar_year": "2026",
     "record_calendar_quarter": "3", "record_calendar_month": "09",
     "record_calendar_day": "17"},
    {"record_date": "2026-09-16", "open_today_bal": "1012345",
     "open_month_bal": "1023554", "open_fiscal_year_bal": "890825",
     "table_nbr": "I", "table_nm": "Operating Cash Balance",
     "sub_table_name": "Cash Balance Details", "src_line_nbr": "1",
     "record_fiscal_year": "2026", "record_calendar_year": "2026",
     "record_calendar_quarter": "3", "record_calendar_month": "09",
     "record_calendar_day": "16"},
    {"record_date": "2026-09-15", "open_today_bal": "0",
     "open_month_bal": "1023554", "open_fiscal_year_bal": "890825",
     "table_nbr": "I", "table_nm": "Operating Cash Balance",
     "sub_table_name": "Cash Balance Details", "src_line_nbr": "1",
     "record_fiscal_year": "2026", "record_calendar_year": "2026",
     "record_calendar_quarter": "3", "record_calendar_month": "09",
     "record_calendar_day": "15"},
]

PAYLOAD = {"data": RECORDS}


class FakeClient:
    body = None

    def __init__(self, *args, **kwargs):
        self.retrieved_at = "2026-09-19T00:00:00+00:00"
        self.warnings = []

    def get_json(self, url):
        if not url.startswith(SOURCES["TREASURY_FISCAL_DATA"] + "/"):
            raise AssertionError(f"unexpected URL {url}")
        FakeClient.body = url
        return PAYLOAD


class TreasuryTests(unittest.TestCase):
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

    def test_observation_is_macro_release(self):
        observation = treasury._observation(RECORDS[0], "2026-09-19T00:00:00+00:00")
        self.assertEqual(observation["kind"], "macro_release")
        self.assertEqual(observation["actor"], "")
        self.assertEqual(observation["counterparty"], "")
        self.assertEqual(observation["instrument"], "")
        self.assertEqual((observation["amount"], observation["currency"]),
                         ("991708", "USD"))
        self.assertEqual(observation["occurred_at"], "2026-09-17")
        self.assertTrue(observation["source_url"].startswith(
            SOURCES["TREASURY_FISCAL_DATA"]))
        evidence = json.loads(observation["evidence"])
        self.assertEqual(evidence["record_date"], "2026-09-17")
        self.assertEqual(evidence["table_nm"], "Operating Cash Balance")
        self.assertIn("Daily Treasury Statement", evidence["amount_semantics"])

    def test_observation_skips_zero_balance(self):
        observation = treasury._observation(RECORDS[2], "2026-09-19T00:00:00+00:00")
        self.assertIsNone(observation)

    def test_observation_skips_bad_date(self):
        bad = dict(RECORDS[0], record_date="not-a-date")
        self.assertIsNone(treasury._observation(bad, "2026-09-19T00:00:00+00:00"))

    def test_fetch_treasury_stores_macro_releases(self):
        with patch.object(treasury, "HTTPClient", FakeClient):
            report = treasury.fetch_treasury(self.conn, limit=5,
                                              start="2026-09-01", end="2026-09-30")
        self.assertEqual(report["records"], 3)
        self.assertEqual(report["observations"], 2)
        self.assertEqual(report["skipped"], 1)
        self.assertEqual(report["by_kind"], ["macro_release"])
        rows = registry.list_observations(self.conn)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["kind"], "macro_release")
        runs = registry.list_ingest_runs(self.conn)
        self.assertEqual(runs[0]["source"], "treasury-fiscal-data")
        self.assertEqual(runs[0]["stored"], 2)
        body = FakeClient.body
        self.assertIn("filter%5Brecord_date%5D%5Bgte%5D=2026-09-01", body)
        self.assertIn("filter%5Brecord_date%5D%5Blte%5D=2026-09-30", body)

    def test_fetch_treasury_rejections(self):
        with self.assertRaises(ValueError):
            treasury.fetch_treasury(self.conn, limit=0)
        with self.assertRaises(ValueError):
            treasury.fetch_treasury(self.conn, limit=101)
        with self.assertRaises(ValueError):
            treasury.fetch_treasury(self.conn, limit=5, start="2026-09-01",
                                      end="2026-08-01")
        with self.assertRaises(ValueError):
            treasury.fetch_treasury(self.conn, limit=5, start="01-01-2026")

    def test_cli_dispatch(self):
        with patch("lele.fetchers.treasury.fetch_treasury",
                   return_value={"source": "treasury-fiscal-data",
                                 "observations": 1}) as fetch:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                status = main(["--db", str(self.db), "--json", "fetch-treasury",
                               "--limit", "10", "--start", "2026-09-01",
                               "--end", "2026-09-30"])
            self.assertEqual((status, err.getvalue()), (0, ""))
            self.assertEqual(json.loads(out.getvalue())["observations"], 1)
            self.assertEqual(fetch.call_args.args[1], 10)
            self.assertEqual(fetch.call_args.args[2], "2026-09-01")
            self.assertEqual(fetch.call_args.args[3], "2026-09-30")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with patch("lele.fetchers.treasury.fetch_treasury",
                       return_value={"source": "treasury-fiscal-data",
                                     "observations": 0}):
                status = main(["--db", str(self.db), "fetch-treasury",
                               "--start", "2026-09-01", "--end", "2026-09-30"])
        self.assertEqual(status, 0)


if __name__ == "__main__":
    unittest.main()
