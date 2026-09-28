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
from lele.fetchers import thirteenf

ACCESSION = "0001193125-26-352200"
BASE = f"{SOURCES['SEC_ARCHIVES']}/1067983/000119312526352200"
INFO_URL = BASE + "/56757.xml"
INFO_XML = b"""<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable>
    <nameOfIssuer>ALLY FINL INC</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>02005N100</cusip>
    <value>577211815</value>
    <shrsOrPrnAmt><sshPrnamt>12561737</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
    <investmentDiscretion>DFND</investmentDiscretion>
    <otherManager>4</otherManager>
    <votingAuthority><Sole>12561737</Sole><Shared>0</Shared><None>0</None></votingAuthority>
  </infoTable>
  <infoTable>
    <nameOfIssuer>APPLE INC</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>037833100</cusip>
    <value>100</value>
    <shrsOrPrnAmt><sshPrnamt>50</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
  </infoTable>
  <infoTable>
    <nameOfIssuer>NO VALUE</nameOfIssuer>
    <value>0</value>
  </infoTable>
</informationTable>
"""
COVER_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<edgarSubmission xmlns="http://www.sec.gov/edgar/thirteenffiler">
  <formData><coverPage><reportCalendarOrQuarter>06-30-2026</reportCalendarOrQuarter>
  <isAmendment>false</isAmendment></coverPage></formData>
</edgarSubmission>
"""
INDEX_JSON = {"directory": {"item": [
    {"name": "56757.xml", "size": "44724"},
    {"name": "primary_doc.xml", "size": "5555"},
    {"name": "R1.htm", "size": "100"},
]}}
SUBMISSIONS = {
    "cik": "0001067983",
    "filings": {"recent": {
        "form": ["13F-HR", "13F-HR/A", "10-K"],
        "filingDate": ["2026-08-14", "2026-08-15", "2026-08-01"],
        "acceptanceDateTime": ["2026-08-14T16:00:00.000Z", "2026-08-15T16:00:00.000Z",
                               "2026-08-01T16:00:00.000Z"],
        "accessionNumber": [ACCESSION, "0001193125-26-999999", "0001193125-26-111111"],
    }},
}


class FakeClient:
    def __init__(self, *args, **kwargs):
        self.retrieved_at = "2026-08-16T00:00:00+00:00"
        self.warnings = []
        self.files = {INFO_URL: INFO_XML, BASE + "/primary_doc.xml": COVER_XML}
        self.downloads = []

    def get_json(self, url):
        if url == f"{SOURCES['SEC_SUBMISSIONS']}/CIK0001067983.json":
            return SUBMISSIONS
        if url == BASE + "/index.json":
            return INDEX_JSON
        raise AssertionError(f"unexpected URL {url}")

    def download(self, url, target, max_bytes):
        if url not in self.files:
            raise AssertionError(f"unexpected download {url}")
        self.downloads.append((url, max_bytes))
        with open(target, "wb") as stream:
            stream.write(self.files[url])
        return len(self.files[url])


class ThirteenFTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db = self.root / "registry.db"
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(registry._SCHEMA)
        self.eid = registry.upsert_entity(self.conn, "company", "Berkshire Hathaway",
                                          key="cik:0001067983")
        self.conn.commit()
        self.addCleanup(self.conn.close)

    def test_parse_thirteenf_holdings(self):
        items, skipped = thirteenf.parse_thirteenf(INFO_XML, ACCESSION,
                                                   "2026-08-14T16:00:00+00:00", "2026-06-30",
                                                   INFO_URL, "cik:0001067983")
        self.assertEqual((len(items), skipped), (2, {"no_value": 1, "invalid": 0}))
        self.assertEqual([item["amount"] for item in items], ["577211815", "100"])
        self.assertEqual({item["kind"] for item in items}, {"holding"})
        self.assertEqual(items[0]["actor"], "cik:0001067983")
        self.assertEqual(items[0]["instrument"], "")
        self.assertEqual(items[0]["currency"], "USD")
        self.assertEqual(items[0]["occurred_at"], "2026-06-30")
        evidence = json.loads(items[0]["evidence"])
        self.assertEqual((evidence["cusip"], evidence["shares"], evidence["voting_authority"]["sole"]),
                         ("02005N100", "12561737", "12561737"))

    def test_parse_cover_and_rejections(self):
        cover = thirteenf.parse_cover(COVER_XML)
        self.assertEqual((cover["period"], cover["amendment"]), ("2026-06-30", False))
        with self.assertRaises(ValueError):
            thirteenf.parse_cover(b"<!DOCTYPE x [<!ENTITY e 'v'>]><edgarSubmission/>")
        with self.assertRaises(ValueError):
            thirteenf.parse_cover(b"<notCover/>")
        with self.assertRaises(ValueError):
            thirteenf.parse_thirteenf(b"<!DOCTYPE x><informationTable/>", ACCESSION,
                                      "2026-08-14T16:00:00+00:00", "", INFO_URL, "cik:0001067983")
        with self.assertRaises(ValueError):
            thirteenf.parse_thirteenf(b"<notTable/>", ACCESSION, "2026-08-14T16:00:00+00:00", "",
                                      INFO_URL, "cik:0001067983")

    def test_filings_filters_original_13f(self):
        filings, amendments = thirteenf._filings(SUBMISSIONS, "0001067983", 10)
        self.assertEqual(len(filings), 1)
        self.assertEqual(amendments, 1)
        self.assertEqual(filings[0]["accession"], ACCESSION)
        with self.assertRaises(ValueError):
            thirteenf._filings(dict(SUBMISSIONS, cik="0000000001"), "0001067983", 10)

    def test_information_table_selection(self):
        class NoTable:
            def get_json(self, url):
                return {"directory": {"item": [{"name": "primary_doc.xml", "size": "10"}]}}

        client = FakeClient()
        self.assertEqual(thirteenf._information_table(client, BASE), "56757.xml")
        with self.assertRaises(ValueError):
            thirteenf._information_table(NoTable(), BASE)

    def test_fetch_thirteenf_stores_holdings(self):
        with patch.object(thirteenf, "HTTPClient", FakeClient):
            report = thirteenf.fetch_thirteenf(self.conn, self.eid, limit=5)
        self.assertEqual((report["filings"], report["observations"]), (1, 2))
        self.assertEqual(report["by_kind"], ["holding"])
        rows = registry.list_observations(self.conn, self.eid)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["kind"], "holding")
        self.assertEqual(rows[0]["instrument_key"], "")
        runs = registry.list_ingest_runs(self.conn)
        self.assertEqual((runs[0]["source"], runs[0]["stored"]), ("sec-13f", 2))

    def test_fetch_requires_manager_cik(self):
        other = registry.upsert_entity(self.conn, "company", "No CIK", key="local:no-cik")
        with self.assertRaises(ValueError):
            thirteenf.fetch_thirteenf(self.conn, other, limit=2)
        with self.assertRaises(ValueError):
            thirteenf.fetch_thirteenf(self.conn, self.eid, limit=0)

    def test_cli_dispatch(self):
        with patch("lele.fetchers.thirteenf.fetch_thirteenf",
                   return_value={"source": "sec-13f", "observations": 7}) as fetch:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                status = main(["--db", str(self.db), "--json", "fetch-13f", str(self.eid),
                               "--limit", "2"])
            self.assertEqual((status, err.getvalue()), (0, ""))
            self.assertEqual(json.loads(out.getvalue())["observations"], 7)
            self.assertEqual(fetch.call_args.args[2], 2)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--db", str(self.db), "fetch-13f"]), 2)


if __name__ == "__main__":
    unittest.main()
