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
from lele.fetchers import nport

NPORT_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<edgarSubmission xmlns="http://www.sec.gov/edgar/nport">
  <headerData><submissionType>NPORT-P</submissionType></headerData>
  <formData>
    <genInfo>
      <regName>Example Index Funds</regName>
      <seriesName>Example 500 ETF</seriesName>
      <seriesLei>549300EXAMPLE00000001</seriesLei>
      <repPdDate>2026-06-30</repPdDate>
    </genInfo>
    <fundInfo>
      <totAssets>1000000.00</totAssets>
      <totLiabs>5000.00</totLiabs>
      <netAssets>995000.00</netAssets>
    </fundInfo>
    <invstOrSecs>
      <invstOrSec>
        <name>Aflac Inc</name>
        <lei>549300N0B7DOGLXWPP39</lei>
        <title>Aflac Inc</title>
        <cusip>001055102</cusip>
        <identifiers><isin value="US0010551028"/></identifiers>
        <balance>5551377.00000000</balance>
        <units>NS</units>
        <curCd>USD</curCd>
        <valUSD>650898953.25000000</valUSD>
        <pctVal>0.083321585405</pctVal>
        <payoffProfile>Long</payoffProfile>
        <assetCat>EC</assetCat>
        <issuerCat>CORP</issuerCat>
        <invCountry>US</invCountry>
        <fairValLevel>1</fairValLevel>
      </invstOrSec>
      <invstOrSec>
        <name>Cash</name>
        <cusip>000000000</cusip>
        <balance>100.00</balance>
        <units>USD</units>
        <curCd>USD</curCd>
        <valUSD>100.00</valUSD>
      </invstOrSec>
      <invstOrSec>
        <title>Nameless</title>
        <cusip>999999999</cusip>
      </invstOrSec>
    </invstOrSecs>
  </formData>
</edgarSubmission>
"""
SUBMISSIONS = {
    "cik": "0000884394",
    "filings": {"recent": {
        "form": ["NPORT-P", "NPORT-P/A", "N-CEN", "NPORT-P"],
        "filingDate": ["2026-08-28", "2026-08-29", "2026-08-01", "2026-08-27"],
        "acceptanceDateTime": ["2026-08-28T20:00:00.000Z", "2026-08-29T20:00:00.000Z",
                               "2026-08-01T20:00:00.000Z", "2026-08-27T20:00:00.000Z"],
        "accessionNumber": ["0001410368-26-089410", "0001410368-26-089411",
                            "0001410368-26-000001", "bad-accession"],
        "primaryDocument": ["xslFormNPORT-P_X01/primary_doc.xml", "primary_doc.xml", "x.htm",
                            "primary_doc.xml"],
    }},
}


class FakeClient:
    def __init__(self, *args, **kwargs):
        self.retrieved_at = "2026-08-30T00:00:00+00:00"
        self.warnings = []
        self.downloads = []

    def get_json(self, url):
        if url != f"{SOURCES['SEC_SUBMISSIONS']}/CIK0000884394.json":
            raise AssertionError(f"unexpected URL {url}")
        return SUBMISSIONS

    def download(self, url, target, max_bytes):
        if not url.endswith("/primary_doc.xml"):
            raise AssertionError(f"unexpected download {url}")
        self.downloads.append((url, max_bytes))
        with open(target, "wb") as stream:
            stream.write(NPORT_XML)
        return len(NPORT_XML)


class NportTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db = self.root / "registry.db"
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(registry._SCHEMA)
        self.eid = registry.upsert_entity(self.conn, "company", "Example Index Funds",
                                          key="cik:0000884394")
        self.conn.commit()
        self.addCleanup(self.conn.close)

    def test_parse_nport_holdings(self):
        items, context, meta = nport.parse_nport(
            NPORT_XML, "0001410368-26-089410", "2026-08-28T20:00:00+00:00",
            f"{SOURCES['SEC_ARCHIVES']}/884394/000141036826089410/primary_doc.xml")
        self.assertEqual((len(items), meta), (2, {"skipped": 1, "truncated": False}))
        self.assertEqual((context["reg_name"], context["series_name"], context["period"]),
                         ("Example Index Funds", "Example 500 ETF", "2026-06-30"))
        self.assertEqual(context["net_assets"], "995000.00")
        self.assertEqual([item["amount"] for item in items],
                         ["650898953.25000000", "100.00"])
        self.assertEqual({item["kind"] for item in items}, {"holding"})
        self.assertEqual(items[0]["currency"], "USD")
        self.assertEqual(items[0]["occurred_at"], "2026-06-30")
        evidence = json.loads(items[0]["evidence"])
        self.assertEqual((evidence["cusip"], evidence["isin"], evidence["asset_cat"]),
                         ("001055102", "US0010551028", "EC"))
        self.assertEqual(evidence["fund"]["period"], "2026-06-30")

    def test_parse_nport_rejections(self):
        with self.assertRaises(ValueError):
            nport.parse_nport(b"<!DOCTYPE x [<!ENTITY e 'v'>]><edgarSubmission/>",
                              "0001410368-26-089410", "2026-08-28T20:00:00+00:00",
                              f"{SOURCES['SEC_ARCHIVES']}/x/primary_doc.xml")
        with self.assertRaises(ValueError):
            nport.parse_nport(b"<notNport/>", "0001410368-26-089410",
                              "2026-08-28T20:00:00+00:00",
                              f"{SOURCES['SEC_ARCHIVES']}/x/primary_doc.xml")

    def test_filings_filters_and_skips(self):
        filings, skipped = nport._filings(SUBMISSIONS, "0000884394", 10)
        self.assertEqual(skipped, 2)
        self.assertEqual([row["accession"] for row in filings], ["0001410368-26-089410"])
        self.assertEqual(filings[0]["document"], "primary_doc.xml")
        with self.assertRaises(ValueError):
            nport._filings(dict(SUBMISSIONS, cik="0000000001"), "0000884394", 10)

    def test_fetch_nport_stores_holdings(self):
        with patch.object(nport, "HTTPClient", FakeClient):
            report = nport.fetch_nport(self.conn, self.eid, limit=5)
        self.assertEqual((report["filings"], report["observations"]), (1, 2))
        self.assertEqual(report["by_kind"], ["holding"])
        rows = registry.list_observations(self.conn, self.eid)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["instrument_key"] == "" for row in rows))
        self.assertTrue(all(row["actor_key"] == "cik:0000884394" for row in rows))
        runs = registry.list_ingest_runs(self.conn)
        self.assertEqual((runs[0]["source"], runs[0]["stored"]), ("sec-nport", 2))

    def test_fetch_requires_cik(self):
        other = registry.upsert_entity(self.conn, "company", "No CIK", key="local:no-cik")
        with self.assertRaises(ValueError):
            nport.fetch_nport(self.conn, other, limit=1)
        with self.assertRaises(ValueError):
            nport.fetch_nport(self.conn, self.eid, limit=0)

    def test_cli_dispatch(self):
        with patch("lele.fetchers.nport.fetch_nport",
                   return_value={"source": "sec-nport", "observations": 2}) as fetch:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                status = main(["--db", str(self.db), "--json", "fetch-nport", str(self.eid)])
            self.assertEqual((status, err.getvalue()), (0, ""))
            self.assertEqual(json.loads(out.getvalue())["observations"], 2)
            self.assertEqual(fetch.call_args.args[1], self.eid)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--db", str(self.db), "fetch-nport"]), 2)


if __name__ == "__main__":
    unittest.main()
