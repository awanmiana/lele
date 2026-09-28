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
from lele.fetchers import form4

ACCESSION = "0000320193-24-000001"
XML_URL = (f"{SOURCES['SEC_ARCHIVES']}/320193/000032019324000001/form4.xml")
XML = b"""<?xml version="1.0"?>
<ownershipDocument>
  <documentType>4</documentType>
  <periodOfReport>2024-05-01</periodOfReport>
  <issuer>
    <issuerCik>0000320193</issuerCik>
    <issuerName>APPLE INC</issuerName>
    <issuerTradingSymbol>AAPL</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0001234567</rptOwnerCik>
      <rptOwnerName>DOE JANE</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>1</isDirector><isOfficer>0</isOfficer><isTenPercentOwner>0</isTenPercentOwner>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2024-05-01</value></transactionDate>
      <transactionCoding>
        <transactionFormType>4</transactionFormType><transactionCode>S</transactionCode>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>1000</value></transactionShares>
        <transactionPricePerShare><value>180.5</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>5000</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
      <ownershipNature>
        <directOrIndirectOwnership><value>D</value></directOrIndirectOwnership>
      </ownershipNature>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2024-05-01</value></transactionDate>
      <transactionCoding>
        <transactionFormType>4</transactionFormType><transactionCode>A</transactionCode>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>200</value></transactionShares>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""
SUBMISSIONS = {
    "cik": "0000320193",
    "filings": {"recent": {
        "form": ["4", "10-K"],
        "filingDate": ["2024-05-02", "2024-05-01"],
        "acceptanceDateTime": ["2024-05-02T20:31:23.000Z", "2024-05-01T18:00:00.000Z"],
        "accessionNumber": [ACCESSION, "0000320193-24-000002"],
        "primaryDocument": ["form4.xml", "aapl-10k.htm"],
    }},
}


class FakeClient:
    xml = XML

    def __init__(self, *args, **kwargs):
        self.retrieved_at = "2024-05-03T00:00:00+00:00"
        self.warnings = []
        self.downloaded = []

    def get_json(self, url):
        if url != f"{SOURCES['SEC_SUBMISSIONS']}/CIK0000320193.json":
            raise AssertionError(f"unexpected URL {url}")
        return SUBMISSIONS

    def download(self, url, target, max_bytes):
        if not url.startswith(f"{SOURCES['SEC_ARCHIVES']}/320193/"):
            raise AssertionError(f"unexpected URL {url}")
        self.downloaded.append((url, max_bytes))
        with open(target, "wb") as stream:
            stream.write(self.xml)
        return len(self.xml)


class Form4Tests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db = self.root / "registry.db"
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(registry._SCHEMA)
        self.eid = registry.upsert_entity(self.conn, "company", "Apple Inc.",
                                          key="cik:0000320193")
        self.conn.commit()
        self.addCleanup(self.conn.close)

    def test_parse_form4_transactions(self):
        items, skipped = form4.parse_form4(XML, ACCESSION, "2024-05-02T20:31:23+00:00", XML_URL)
        self.assertEqual(skipped, {"no_shares": 0, "no_direction": 0, "invalid": 0})
        self.assertEqual([item["amount"] for item in items], ["-1000", "200"])
        self.assertEqual([item["actor"] for item in items],
                         ["sec-owner:0001234567", "sec-owner:0001234567"])
        self.assertEqual(items[0]["unit"], "shares")
        self.assertEqual(items[0]["occurred_at"], "2024-05-01")
        self.assertEqual(items[0]["observed_at"], "2024-05-02T20:31:23+00:00")
        evidence = json.loads(items[0]["evidence"])
        self.assertEqual((evidence["transaction_code"], evidence["price_per_share"]),
                         ("S", "180.5"))
        self.assertEqual(evidence["is_director"], "1")

    def test_parse_rejects_unsafe_documents(self):
        with self.assertRaises(ValueError):
            form4.parse_form4(b"<!DOCTYPE foo [<!ENTITY x 'y'>]><ownershipDocument/>",
                              ACCESSION, "2024-05-02T20:31:23+00:00", XML_URL)
        with self.assertRaises(ValueError):
            form4.parse_form4(b"<notForm4/>", ACCESSION, "2024-05-02T20:31:23+00:00", XML_URL)
        wrong = XML.replace(b"<documentType>4</documentType>", b"<documentType>3</documentType>")
        with self.assertRaises(ValueError):
            form4.parse_form4(wrong, ACCESSION, "2024-05-02T20:31:23+00:00", XML_URL)
        with self.assertRaises(ValueError):
            form4.parse_form4(XML, ACCESSION, "2024-05-02T20:31:23+00:00", "http://x.example/f")

    def test_filings_filters_original_form4(self):
        filings = form4._filings(SUBMISSIONS, "0000320193", 10)
        self.assertEqual(len(filings), 1)
        self.assertEqual(filings[0]["accession"], ACCESSION)
        self.assertEqual(filings[0]["accepted"], "2024-05-02T20:31:23+00:00")
        with self.assertRaises(ValueError):
            form4._filings(dict(SUBMISSIONS, cik="0000000001"), "0000320193", 10)

    def test_fetch_form4_stores_observations(self):
        with patch.object(form4, "HTTPClient", FakeClient):
            report = form4.fetch_form4(self.conn, self.eid, limit=10)
        self.assertEqual((report["filings"], report["observations"]), (1, 2))
        self.assertEqual(report["by_kind"], ["insider_trade"])
        rows = registry.list_observations(self.conn)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["instrument_key"], "cik:0000320193")
        self.assertEqual(rows[0]["source_url"], XML_URL)
        self.assertEqual(rows[0]["actor_key"], "sec-owner:0001234567")
        runs = registry.list_ingest_runs(self.conn)
        self.assertEqual(runs[0]["source"], "sec-form4")
        self.assertEqual(runs[0]["stored"], 2)

    def test_fetch_form4_requires_cik(self):
        other = registry.upsert_entity(self.conn, "company", "No CIK", key="local:no-cik")
        with self.assertRaises(ValueError):
            form4.fetch_form4(self.conn, other, limit=5)
        with self.assertRaises(ValueError):
            form4.fetch_form4(self.conn, self.eid, limit=0)

    def test_cli_dispatch(self):
        with patch("lele.fetchers.form4.fetch_form4",
                   return_value={"source": "sec-form4", "observations": 3}) as fetch:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                status = main(["--db", str(self.db), "--json", "fetch-form4", str(self.eid),
                               "--limit", "5"])
            self.assertEqual((status, err.getvalue()), (0, ""))
            self.assertEqual(json.loads(out.getvalue())["observations"], 3)
            self.assertEqual(fetch.call_args.args[1], self.eid)
            self.assertEqual(fetch.call_args.args[2], 5)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--db", str(self.db), "fetch-form4"]), 2)


if __name__ == "__main__":
    unittest.main()
