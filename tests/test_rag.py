import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, UTC
from pathlib import Path

from lele.core import registry
from lele.analysis import rag


class RagTests(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / "registry.db"
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        registry._initialize(self.conn)
        self.addCleanup(self.conn.close)

    def test_store_events_inserts(self):
        events = [{
            "event_type": "political_event",
            "external_id": "2026-09-17-001",
            "title": "Proposed Rule: Environmental Protection Standards",
            "actor_key": "political:REGULATION-PROPOSED:2026-09-17-001",
            "actor_name": "Environmental Protection Agency",
            "government_entity": "Environmental Protection Agency",
            "publication_date": "2026-09-17",
            "abstract": "This proposed rule establishes new environmental protection standards.",
            "html_url": "https://www.federalregister.gov/documents/2026/09/17/2026-09-17-001",
        }]
        result = rag.store_events(self.conn, events, source="political-event")
        self.assertEqual(result["stored"], 1)
        rows = self.conn.execute("SELECT * FROM event_store WHERE event_type='political_event'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["actor_name"], "Environmental Protection Agency")

    def test_store_events_skips_empty_title(self):
        events = [{
            "event_type": "political_event",
            "external_id": "2026-09-17-001",
            "title": "",
            "actor_key": "test",
            "government_entity": "EPA",
            "publication_date": "2026-09-17",
        }]
        result = rag.store_events(self.conn, events)
        self.assertEqual(result["skipped"], 1)

    def test_store_events_duplicate_ignored(self):
        events = [{
            "event_type": "political_event",
            "external_id": "2026-09-17-001",
            "title": "Test Rule",
            "actor_key": "test",
            "government_entity": "EPA",
            "publication_date": "2026-09-17",
        }]
        rag.store_events(self.conn, events)
        rag.store_events(self.conn, events)
        rows = self.conn.execute("SELECT COUNT(*) as c FROM event_store").fetchone()
        self.assertEqual(rows["c"], 1)

    def test_build_event_graph_creates_relationships(self):
        recent = datetime.now(UTC).date().isoformat()
        events = [
            {"event_type": "political_event", "external_id": "001",
             "title": "First Event", "actor_key": "actor_a",
             "government_entity": "Agency A", "publication_date": recent},
            {"event_type": "regulatory_action", "external_id": "002",
             "title": "Second Event", "actor_key": "actor_a",
             "government_entity": "Agency A", "publication_date": recent},
        ]
        rag.store_events(self.conn, events)
        count = rag.build_event_graph(self.conn, window_hours=48)
        self.assertGreater(count, 0)
        rels = self.conn.execute("SELECT * FROM event_relationships").fetchall()
        self.assertGreater(len(rels), 0)

    def test_detect_price_anomalies(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS observations(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL CHECK(length(trim(source)) > 0),
                external_id TEXT NOT NULL CHECK(length(trim(external_id)) > 0),
                kind TEXT NOT NULL CHECK(length(trim(kind)) > 0),
                description TEXT NOT NULL DEFAULT '',
                actor_key TEXT NOT NULL DEFAULT '',
                counterparty_key TEXT NOT NULL DEFAULT '',
                instrument_key TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                reason_basis TEXT NOT NULL DEFAULT 'unknown',
                amount TEXT,
                unit TEXT NOT NULL DEFAULT '',
                currency TEXT NOT NULL DEFAULT '',
                basis TEXT NOT NULL DEFAULT 'observed',
                occurred_at TEXT NOT NULL DEFAULT '',
                observed_at TEXT NOT NULL DEFAULT '',
                available_at TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL DEFAULT '',
                evidence TEXT NOT NULL DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(source, external_id));
            CREATE INDEX IF NOT EXISTS idx_observations_instrument
                ON observations(instrument_key, observed_at);
        """)
        self.conn.commit()
        for i in range(10):
            price = 100.0 + (i * 0.5)
            self.conn.execute(
                """INSERT INTO observations(source, external_id, kind, instrument_key,
                    observed_at, evidence) VALUES(?,?,?,?,?,?)""",
                ("price", f"obs-{i}", "price_series", "TEST-USD",
                 f"2026-09-{i+1:02d}T10:00:00+00:00",
                 json.dumps({"value": price})),
            )
        self.conn.commit()
        anomalies = rag.detect_price_anomalies(self.conn, "TEST-USD", threshold_sigma=0.5)
        self.assertGreater(len(anomalies), 0)

    def test_event_indicator_v1(self):
        rag.store_events(self.conn, [{
            "event_type": "political_event", "external_id": "001",
            "title": "Test Event", "actor_key": "actor_a",
            "government_entity": "EPA", "publication_date": "2026-09-19",
        }])
        result = rag.event_indicator_v1(self.conn)
        self.assertIn("score", result)
        self.assertIn("event_count", result)
        self.assertIn("flow_count", result)
        self.assertIn("anomaly_count", result)
        self.assertIn("breakdown", result)
        self.assertEqual(result["event_count"], 1)

    def test_attribute_money_flows(self):
        rag.store_events(self.conn, [{
            "event_type": "political_event", "external_id": "001",
            "title": "Test Event", "actor_key": "actor_a",
            "government_entity": "EPA", "publication_date": "2026-09-19",
        }])
        count = rag.attribute_money_flows(self.conn, event_ids=[1], window_hours=48)
        self.assertGreaterEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
