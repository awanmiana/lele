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
from lele.fetchers import awards

PAYLOAD = {"results": [
    {"Award ID": "N0001917C0001", "Recipient Name": "LOCKHEED MARTIN CORPORATION",
     "Recipient UEI": "G4KDGE4JFFK7", "Award Amount": 35135514910.2,
     "Awarding Agency": "Department of Defense", "Start Date": "2017-11-17",
     "End Date": "2031-03-31", "generated_internal_id": "CONT_AWD_N0001917C0001_9700_-NONE-_-NONE-",
     "awarding_agency_id": 1173, "agency_slug": "department-of-defense"},
    {"Award ID": "OTHER", "Recipient Name": "OTHER CORP", "Recipient UEI": "ZZZZZZZZZZZZ",
     "Award Amount": 999.0, "Awarding Agency": "Department of Defense", "Start Date": "2020-01-01",
     "End Date": "", "generated_internal_id": "CONT_AWD_OTHER", "awarding_agency_id": 1173,
     "agency_slug": "department-of-defense"},
    {"Award ID": "ZERO", "Recipient Name": "LOCKHEED MARTIN CORPORATION",
     "Recipient UEI": "G4KDGE4JFFK7", "Award Amount": 0, "Awarding Agency": "Department of Defense",
     "Start Date": "2020-01-01", "End Date": "", "generated_internal_id": "CONT_AWD_ZERO",
     "awarding_agency_id": 1173, "agency_slug": "department-of-defense"},
], }


class FakeClient:
    body = None

    def __init__(self, *args, **kwargs):
        self.retrieved_at = "2026-09-19T00:00:00+00:00"
        self.warnings = []

    def post_json(self, url, body):
        if url != SOURCES["USASPENDING_AWARDS"] + "/":
            raise AssertionError(f"unexpected URL {url}")
        FakeClient.body = body
        return PAYLOAD


class AwardsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db = self.root / "registry.db"
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(registry._SCHEMA)
        self.eid = registry.upsert_entity(self.conn, "company", "Lockheed Martin",
                                          key="cik:0000936468")
        self.conn.commit()
        self.addCleanup(self.conn.close)

    def test_observation_is_routed_fund_flow(self):
        observation = awards._observation(PAYLOAD["results"][0], "cik:0000936468",
                                          {"G4KDGE4JFFK7"}, "LOCKHEED MARTIN",
                                          "2026-09-19T00:00:00+00:00")
        self.assertEqual(observation["kind"], "fund_flow")
        self.assertEqual(observation["actor"], "usaspending:agency:department-of-defense")
        self.assertEqual(observation["counterparty"], "cik:0000936468")
        self.assertEqual(observation["instrument"], "cik:0000936468")
        self.assertEqual((observation["amount"], observation["currency"]), ("35135514910.2", "USD"))
        self.assertEqual(observation["occurred_at"], "2017-11-17")
        self.assertTrue(observation["source_url"].startswith("https://www.usaspending.gov/award/"))
        evidence = json.loads(observation["evidence"])
        self.assertEqual((evidence["award_kind"], evidence["recipient_uei"]),
                         ("contract", "G4KDGE4JFFK7"))
        self.assertEqual(evidence["matched_ueis"], ["G4KDGE4JFFK7"])

    def test_fetch_awards_matches_only_explicit_uei(self):
        with patch.object(awards, "HTTPClient", FakeClient):
            report = awards.fetch_awards(self.conn, self.eid, ["G4KDGE4JFFK7"], limit=5,
                                         start="2025-01-01", end="2025-12-31",
                                         search="LOCKHEED MARTIN")
        self.assertEqual(report["candidates"], 6)
        self.assertEqual(report["by_award_kind"], {"contract": 3, "grant": 3})
        self.assertEqual(report["observations"], 1)
        self.assertEqual(report["skipped"], {"no_positive_amount": 2, "uei_not_matched": 2})
        self.assertEqual(report["by_kind"], ["fund_flow"])
        body = FakeClient.body
        self.assertEqual(body["filters"]["recipient_search_text"], ["LOCKHEED MARTIN"])
        self.assertEqual(body["filters"]["time_period"],
                         [{"start_date": "2025-01-01", "end_date": "2025-12-31"}])
        rows = registry.list_observations(self.conn, self.eid)
        self.assertEqual(len(rows), 1)
        runs = registry.list_ingest_runs(self.conn)
        self.assertEqual((runs[0]["source"], runs[0]["stored"]), ("usaspending", 1))

    def test_fetch_awards_rejections(self):
        with self.assertRaises(ValueError):
            awards.fetch_awards(self.conn, self.eid, ["short"], limit=2)
        with self.assertRaises(ValueError):
            awards.fetch_awards(self.conn, self.eid, [], limit=2)
        with self.assertRaises(ValueError):
            awards.fetch_awards(self.conn, self.eid, ["G4KDGE4JFFK7"], limit=2,
                                start="2026-02-01", end="2026-01-01")
        with self.assertRaises(ValueError):
            awards.fetch_awards(self.conn, self.eid, ["G4KDGE4JFFK7"], limit=2, start="01-01-2026")

    def test_cli_dispatch(self):
        with patch("lele.fetchers.awards.fetch_awards",
                   return_value={"source": "usaspending", "observations": 1}) as fetch:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                status = main(["--db", str(self.db), "--json", "fetch-awards", str(self.eid),
                               "--recipient-uei", "G4KDGE4JFFK7", "--limit", "3"])
            self.assertEqual((status, err.getvalue()), (0, ""))
            self.assertEqual(json.loads(out.getvalue())["observations"], 1)
            self.assertEqual(fetch.call_args.args[2], ["G4KDGE4JFFK7"])
            self.assertEqual(fetch.call_args.args[3], 3)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--db", str(self.db), "fetch-awards", str(self.eid)]), 2)


if __name__ == "__main__":
    unittest.main()
