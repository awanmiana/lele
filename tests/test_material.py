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
from lele.fetchers import material

SUBMISSIONS = {
    "cik": "0000320193",
    "filings": {"recent": {
        "form": ["8-K", "S-1", "424B5", "DEF 14A", "10-Q", "8-K"],
        "filingDate": ["2026-05-02", "2026-05-01", "2026-04-30", "2026-04-29", "2026-04-28",
                       "2026-04-27"],
        "reportDate": ["2026-05-01", "2026-04-30", "2026-04-30", "", "2026-04-28", ""],
        "acceptanceDateTime": ["2026-05-02T20:31:23.000Z", "2026-05-01T18:00:00.000Z",
                               "2026-04-30T12:00:00.000Z", "2026-04-29T12:00:00.000Z",
                               "2026-04-28T12:00:00.000Z", "2026-04-27T12:00:00.000Z"],
        "accessionNumber": ["0000320193-26-000001", "0000320193-26-000002",
                            "0000320193-26-000003", "0000320193-26-000004",
                            "0000320193-26-000005", "bad-accession"],
        "primaryDocument": ["d1d8k.htm", "d2s1.htm", "d3b5.htm", "d4def14a.htm", "d5q.htm",
                            "d6bad.htm"],
        "primaryDocDescription": ["Current report", "Registration statement", "Prospectus",
                                  "Proxy statement", "Quarterly report", "Bad row"],
        "items": ["2.02,9.01", "", "", "", "", ""],
    }},
}


class FakeClient:
    def __init__(self, *args, **kwargs):
        self.retrieved_at = "2026-05-03T00:00:00+00:00"
        self.warnings = []

    def get_json(self, url):
        if url != f"{SOURCES['SEC_SUBMISSIONS']}/CIK0000320193.json":
            raise AssertionError(f"unexpected URL {url}")
        return SUBMISSIONS


class MaterialTests(unittest.TestCase):
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

    def test_form_selection(self):
        self.assertTrue(material._is_material("8-K"))
        self.assertTrue(material._is_material("S-1/A"))
        self.assertTrue(material._is_material("424B2"))
        self.assertTrue(material._is_material("DEF 14A"))
        self.assertFalse(material._is_material("10-Q"))
        self.assertFalse(material._is_material("4"))

    def test_filings_filters_and_skips_malformed(self):
        filings, skipped = material._filings(SUBMISSIONS, "0000320193", 10)
        self.assertEqual(skipped, 1)
        self.assertEqual([row["form"] for row in filings], ["8-K", "S-1", "424B5", "DEF 14A"])
        first = filings[0]
        self.assertEqual((first["report"], first["items"], first["url"]),
                         ("2026-05-01", "2.02,9.01",
                          f"{SOURCES['SEC_ARCHIVES']}/320193/000032019326000001/d1d8k.htm"))
        with self.assertRaises(ValueError):
            material._filings(dict(SUBMISSIONS, cik="0000000001"), "0000320193", 10)

    def test_fetch_material_stores_filing_events(self):
        with patch.object(material, "HTTPClient", FakeClient):
            report = material.fetch_material(self.conn, self.eid, limit=10)
        self.assertEqual((report["filings"], report["observations"], report["skipped"]),
                         (4, 4, {"malformed": 1}))
        self.assertEqual(report["by_kind"], ["filing_event"])
        rows = registry.list_observations(self.conn, self.eid)
        self.assertEqual(len(rows), 4)
        first = next(row for row in rows if row["external_id"] == "0000320193-26-000001")
        self.assertEqual((first["kind"], first["instrument_key"], first["amount"]),
                         ("filing_event", "cik:0000320193", None))
        self.assertEqual(first["occurred_at"], "2026-05-01")
        self.assertEqual(first["observed_at"], "2026-05-02T20:31:23+00:00")
        self.assertIn("2.02,9.01", first["description"])
        runs = registry.list_ingest_runs(self.conn)
        self.assertEqual((runs[0]["source"], runs[0]["stored"]), ("sec-material", 4))

    def test_projected_filing_event_feeds_worldstate(self):
        with patch.object(material, "HTTPClient", FakeClient):
            material.fetch_material(self.conn, self.eid, limit=10)
        payload, meta = observations.evidence_payload(self.conn, self.eid)
        self.assertEqual(meta["by_kind"], {"filing_event": 4})
        self.assertTrue(all(item["measurement"] is None for item in payload["observations"]))
        projected = self.root / "events.json"
        projected.write_text(json.dumps(payload), encoding="utf-8")
        records, _ = events._evidence(str(projected), "cik:0000320193")
        self.assertEqual(len(records), 4)
        latest = max(record[1] for record in records)
        features = worldstate.build_features(records, latest)
        self.assertEqual(features["event_records"], 1)
        self.assertEqual(features["net_direction"], "flat")

    def test_fetch_requires_cik(self):
        other = registry.upsert_entity(self.conn, "company", "No CIK", key="local:no-cik")
        with self.assertRaises(ValueError):
            material.fetch_material(self.conn, other, limit=5)
        with self.assertRaises(ValueError):
            material.fetch_material(self.conn, self.eid, limit=0)

    def test_cli_dispatch(self):
        with patch("lele.fetchers.material.fetch_material",
                   return_value={"source": "sec-material", "observations": 4}) as fetch:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                status = main(["--db", str(self.db), "--json", "fetch-material", str(self.eid),
                               "--limit", "7"])
            self.assertEqual((status, err.getvalue()), (0, ""))
            self.assertEqual(json.loads(out.getvalue())["observations"], 4)
            self.assertEqual(fetch.call_args.args[2], 7)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--db", str(self.db), "fetch-material"]), 2)


if __name__ == "__main__":
    unittest.main()
