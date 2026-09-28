import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from lele.analysis import events, observations, worldstate
from lele.cli.main import main
from lele.core import registry
from lele.core.constants import SOURCES
from lele.fetchers import formd

D_XML = b"""<?xml version="1.0"?>
<edgarSubmission>
  <submissionType>D</submissionType>
  <primaryIssuer>
    <entityName>EXAMPLE ROCKETS CORP</entityName>
    <entityType>Corporation</entityType>
    <jurisdictionOfInc>DELAWARE</jurisdictionOfInc>
  </primaryIssuer>
  <relatedPersonsList>
    <relatedPersonInfo>
      <relatedPersonName><firstName>JANE</firstName><lastName>DOE</lastName></relatedPersonName>
      <relatedPersonRelationshipList>
        <relationship>Executive Officer</relationship>
        <relationship>Director</relationship>
      </relatedPersonRelationshipList>
    </relatedPersonInfo>
  </relatedPersonsList>
  <offeringData>
    <industryGroup><industryGroupType>Other Technology</industryGroupType></industryGroup>
    <issuerSize><revenueRange>Decline to Disclose</revenueRange></issuerSize>
    <federalExemptionsExclusions><item>06b</item></federalExemptionsExclusions>
    <typeOfFiling>
      <newOrAmendment><isAmendment>false</isAmendment></newOrAmendment>
      <dateOfFirstSale><value>2026-07-20</value></dateOfFirstSale>
    </typeOfFiling>
    <offeringSalesAmounts>
      <totalOfferingAmount>249999890</totalOfferingAmount>
      <totalAmountSold>123456789</totalAmountSold>
      <totalRemaining>0</totalRemaining>
    </offeringSalesAmounts>
    <investors><totalNumberAlreadyInvested>5</totalNumberAlreadyInvested></investors>
    <salesCommissionsFindersFees>
      <salesCommissions><dollarAmount>1000</dollarAmount></salesCommissions>
      <findersFees><dollarAmount>0</dollarAmount></findersFees>
    </salesCommissionsFindersFees>
    <useOfProceeds><grossProceedsUsed><dollarAmount>0</dollarAmount></grossProceedsUsed></useOfProceeds>
    <signatureBlock><signature><signatureDate>2026-08-05</signatureDate></signature></signatureBlock>
  </offeringData>
</edgarSubmission>
"""
SUBMISSIONS = {
    "cik": "0001181412",
    "filings": {"recent": {
        "form": ["D", "D/A", "10-K", "D"],
        "filingDate": ["2026-08-05", "2026-08-06", "2026-08-01", "2026-07-01"],
        "acceptanceDateTime": ["2026-08-05T20:00:00.000Z", "2026-08-06T20:00:00.000Z",
                               "2026-08-01T20:00:00.000Z", "2026-07-01T20:00:00.000Z"],
        "accessionNumber": ["0001181412-26-000003", "0001181412-26-000004",
                            "0001181412-26-000005", "bad-accession"],
        "primaryDocument": ["xslFormDX01/primary_doc.xml", "primary_doc.xml", "x.htm",
                            "primary_doc.xml"],
    }},
}


class FakeClient:
    def __init__(self, *args, **kwargs):
        self.retrieved_at = "2026-08-07T00:00:00+00:00"
        self.warnings = []
        self.downloaded = []

    def get_json(self, url):
        if url != f"{SOURCES['SEC_SUBMISSIONS']}/CIK0001181412.json":
            raise AssertionError(f"unexpected URL {url}")
        return SUBMISSIONS

    def download(self, url, target, max_bytes):
        if not url.endswith("/primary_doc.xml"):
            raise AssertionError(f"unexpected download {url}")
        self.downloaded.append((url, max_bytes))
        with open(target, "wb") as stream:
            stream.write(D_XML)
        return len(D_XML)


class FormDTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db = self.root / "registry.db"
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(registry._SCHEMA)
        self.eid = registry.upsert_entity(self.conn, "company", "Example Rockets",
                                          key="cik:0001181412")
        self.conn.commit()
        self.addCleanup(self.conn.close)

    def test_parse_formd_covers_offering(self):
        observation = formd.parse_formd(D_XML, "0001181412-26-000003",
                                        "2026-08-05T20:00:00+00:00",
                                        f"{SOURCES['SEC_ARCHIVES']}/1181412/000118141226000003/"
                                        "primary_doc.xml")
        self.assertEqual((observation["kind"], observation["amount"], observation["currency"]),
                         ("filing_event", "123456789", "USD"))
        self.assertEqual(observation["occurred_at"], "2026-07-20")
        self.assertEqual(observation["observed_at"], "2026-08-05T20:00:00+00:00")
        self.assertEqual(observation["unit"], "currency")
        evidence = json.loads(observation["evidence"])
        self.assertEqual((evidence["industry_group"], evidence["exemptions"]),
                         ("Other Technology", ["06b"]))
        self.assertEqual(evidence["total_offering_amount_usd"], "249999890")
        self.assertEqual(evidence["investors"], "5")
        self.assertEqual(evidence["related_persons"],
                         [{"name": "JANE DOE", "relationships": ["Executive Officer", "Director"]}])

    def test_parse_formd_rejections(self):
        with self.assertRaises(ValueError):
            formd.parse_formd(b"<!DOCTYPE x [<!ENTITY e 'v'>]><edgarSubmission/>",
                              "0001181412-26-000003", "2026-08-05T20:00:00+00:00",
                              f"{SOURCES['SEC_ARCHIVES']}/x/primary_doc.xml")
        with self.assertRaises(ValueError):
            formd.parse_formd(b"<notFormD/>", "0001181412-26-000003",
                              "2026-08-05T20:00:00+00:00",
                              f"{SOURCES['SEC_ARCHIVES']}/x/primary_doc.xml")
        no_offering = D_XML.replace(b"<offeringData>", b"<offeringDataX>").replace(
            b"</offeringData>", b"</offeringDataX>")
        with self.assertRaises(ValueError):
            formd.parse_formd(no_offering, "0001181412-26-000003",
                              "2026-08-05T20:00:00+00:00",
                              f"{SOURCES['SEC_ARCHIVES']}/x/primary_doc.xml")

    def test_filings_filters_and_skips_malformed(self):
        filings, skipped = formd._filings(SUBMISSIONS, "0001181412", 10)
        self.assertEqual(skipped, 1)
        self.assertEqual([row["accession"] for row in filings],
                         ["0001181412-26-000004", "0001181412-26-000003"])
        self.assertEqual(filings[0]["document"], "primary_doc.xml")
        self.assertEqual(filings[1]["url"],
                         f"{SOURCES['SEC_ARCHIVES']}/1181412/000118141226000003/primary_doc.xml")
        with self.assertRaises(ValueError):
            formd._filings(dict(SUBMISSIONS, cik="0000000001"), "0001181412", 10)

    def test_fetch_formd_stores_and_projects(self):
        with patch.object(formd, "HTTPClient", FakeClient):
            report = formd.fetch_formd(self.conn, self.eid, limit=10)
        self.assertEqual((report["filings"], report["observations"], report["skipped"]),
                         (2, 2, {"malformed": 1}))
        self.assertEqual(report["by_kind"], ["filing_event"])
        rows = registry.list_observations(self.conn, self.eid)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["instrument_key"] == "cik:0001181412" for row in rows))
        self.assertEqual({row["amount"] for row in rows}, {"123456789"})
        runs = registry.list_ingest_runs(self.conn)
        self.assertEqual((runs[0]["source"], runs[0]["stored"]), ("sec-formd", 2))
        payload, meta = observations.evidence_payload(self.conn, self.eid)
        self.assertEqual(meta["by_kind"], {"filing_event": 2})
        projected = self.root / "formd.json"
        projected.write_text(json.dumps(payload), encoding="utf-8")
        records, _ = events._evidence(str(projected), "cik:0001181412")
        latest = max(record[1] for record in records)
        features = worldstate.build_features(records, latest)
        self.assertGreaterEqual(features["event_records"], 1)

    def test_fetch_requires_cik(self):
        other = registry.upsert_entity(self.conn, "company", "No CIK", key="local:no-cik")
        with self.assertRaises(ValueError):
            formd.fetch_formd(self.conn, other, limit=5)
        with self.assertRaises(ValueError):
            formd.fetch_formd(self.conn, self.eid, limit=0)

    def test_cli_dispatch(self):
        with patch("lele.fetchers.formd.fetch_formd",
                   return_value={"source": "sec-formd", "observations": 2}) as fetch:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                status = main(["--db", str(self.db), "--json", "fetch-formd", str(self.eid),
                               "--limit", "3"])
            self.assertEqual((status, err.getvalue()), (0, ""))
            self.assertEqual(json.loads(out.getvalue())["observations"], 2)
            self.assertEqual(fetch.call_args.args[2], 3)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--db", str(self.db), "fetch-formd"]), 2)


if __name__ == "__main__":
    unittest.main()
