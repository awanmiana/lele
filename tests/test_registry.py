import copy
import json
import platform
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lele.analysis import sanctions
from lele.core import importer, registry
from lele.core.db import RegistryMigrationError
from lele.core import constants


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = str(Path(self.temp.name) / "registry.db")
        self.path = Path(self.temp.name) / "import.json"

    def payload(self):
        return {
            "entities": [
                {"key": "report:bank-1", "kind": "bank", "name": "Same Name",
                 "country": "US", "notes": "documented",
                 "attributes": {"source_url": "local:annual-report.pdf#page=2", "rating": "A"},
                 "metrics": [{"k": "assets", "v": 123.5, "period": "2025", "source": "annual report"},
                             {"k": "rating", "v": "AAA"}],
                 "filings": [{"form": "annual", "date": "2025-12-31", "title": "Report",
                              "url": "file:///reports/annual.pdf", "source": "issuer"}]},
                {"key": "report:regulator-1", "kind": "regulator", "name": "Same Name",
                 "attributes": {"source_url": "https://www.sec.gov/"}},
            ],
            "relationships": [{"src": "report:bank-1", "rel": "regulated_by", "dst": "report:regulator-1",
                               "source_url": "local:register.csv", "observed_at": "2026-01-01",
                               "evidence": "Listed in the official register"}],
        }

    def import_payload(self, conn, payload=None):
        self.path.write_text(json.dumps(self.payload() if payload is None else payload), encoding="utf-8")
        return importer.import_json(conn, str(self.path))

    def test_persistence_idempotence_and_evidence(self):
        with registry.get_conn(self.db) as conn:
            result = self.import_payload(conn)
            self.assertEqual(result, {"entities": 2, "relationships": 1, "attributes": 3, "metrics": 2, "filings": 1})
            first = registry.find_entities(conn, kind="bank")[0]["id"]
            self.assertEqual(self.import_payload(conn), result)
            registry.add_signal(conn, first, "filing", 1, "neutral", source="local:report")
        with registry.get_conn(self.db) as conn:
            data = registry.entity_payload(conn, first)
            self.assertEqual(registry.stats(conn)["entities"], 2)
            self.assertEqual(registry.stats(conn)["edges"], 1)
            self.assertEqual(registry.stats(conn)["metrics"], 2)
            self.assertEqual(registry.stats(conn)["filings"], 1)
            self.assertEqual(data["attributes"]["source_url"], "local:annual-report.pdf#page=2")
            self.assertEqual(data["metrics"][0]["v"], "123.5")
            self.assertEqual(len(data["signals"]), 1)
            edge = data["relationships"][0]
            self.assertEqual(edge["evidence"], "Listed in the official register")
            self.assertEqual(edge["source_url"], "local:register.csv")
            self.assertEqual(edge["observed_at"], "2026-01-01")
            incoming = registry.entity_payload(conn, edge["other_id"])["relationships"][0]
            self.assertEqual(incoming["dir"], "in")
            self.assertEqual(incoming["evidence"], edge["evidence"])
            registry.add_edge(conn, first, edge["rel"], edge["other_id"])
            self.assertEqual(registry.entity_payload(conn, first)["relationships"][0], edge)
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(registry.entity_payload(conn, 99999), {})

    def test_connections_close_commit_and_rollback(self):
        with registry.get_conn(self.db) as conn:
            registry.upsert_entity(conn, "bank", "Saved")
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
        with self.assertRaisesRegex(RuntimeError, "abort"):
            with registry.get_conn(self.db) as failed:
                registry.upsert_entity(failed, "bank", "Discarded")
                raise RuntimeError("abort")
        with self.assertRaises(sqlite3.ProgrammingError):
            failed.execute("SELECT 1")
        with registry.get_conn(self.db) as conn:
            self.assertEqual([r["name"] for r in registry.find_entities(conn)], ["Saved"])
        with registry.get_conn(":memory:") as conn:
            self.assertEqual(registry.stats(conn)["entities"], 0)

    def test_identity_and_parameterized_sql(self):
        with registry.get_conn(self.db) as conn:
            name = "Bank'); DROP TABLE entities; --"
            eid = registry.upsert_entity(conn, "bank", name, key="source:1")
            other = registry.upsert_entity(conn, "bank", name, key="source:2")
            self.assertNotEqual(eid, other)
            self.assertEqual(registry.upsert_entity(conn, "bank", "Renamed", key="source:1"), eid)
            default = registry.upsert_entity(conn, "bank", "  Default Name ")
            self.assertEqual(default, registry.upsert_entity(conn, "bank", "default   name"))
            self.assertEqual(len(registry.find_entities(conn, q=name)), 1)
            self.assertEqual(registry.find_entities(conn, kind="bank' OR 1=1 --"), [])
            self.assertEqual(registry.find_entities(conn, with_attribute="' OR 1=1 --"), [])
            with self.assertRaises(ValueError):
                registry.upsert_entity(conn, "regulator", "Wrong", key="source:1")
            for fields in ({"kind": "", "name": "x"}, {"kind": "bank", "name": " "},
                           {"kind": "bank", "name": "x", "key": ""}):
                with self.subTest(fields=fields), self.assertRaises(ValueError):
                    registry.upsert_entity(conn, **fields)

    def test_foreign_keys_and_cascade(self):
        with registry.get_conn(self.db) as conn:
            self.import_payload(conn)
            eid = registry.find_entities(conn, kind="bank")[0]["id"]
            for sql in (
                "INSERT INTO attributes VALUES(999, 'k', 'v')",
                "INSERT INTO metrics VALUES(999, 'k', '1', '', '')",
                "INSERT INTO signals(entity_id) VALUES(999)",
                "INSERT INTO filings(entity_id) VALUES(999)",
                "INSERT INTO edges(src_id, rel, dst_id) VALUES(999, 'owned_by', 1)",
            ):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(sql)
            conn.execute("DELETE FROM entities WHERE id=?", (eid,))
            self.assertEqual(registry.stats(conn)["edges"], 0)
            self.assertEqual(registry.stats(conn)["metrics"], 0)
            self.assertEqual(registry.stats(conn)["filings"], 0)

    def test_validation_precedes_writes(self):
        mutations = [
            lambda p: p["entities"][1].update(name=" "),
            lambda p: p["entities"][1].update(key=""),
            lambda p: p["entities"][1].update(kind=None),
            lambda p: p["entities"][1].update(key=p["entities"][0]["key"]),
            lambda p: p["entities"][1].update(attributes={}),
            lambda p: p["entities"][1].update(extra="unknown"),
            lambda p: p["entities"][0]["metrics"][0].update(v=True),
            lambda p: p["entities"][0]["metrics"][0].update(v=None),
            lambda p: p["entities"][0]["metrics"][0].update(v={}),
            lambda p: p["entities"][0]["metrics"][0].update(v=float("nan")),
            lambda p: p["entities"][0]["metrics"][0].update(v=float("inf")),
            lambda p: p["entities"][0]["metrics"][0].update(v="NaN"),
            lambda p: p["entities"][0]["filings"][0].update(title="", url=""),
            lambda p: p["relationships"][0].update(dst="missing"),
            lambda p: p["relationships"][0].update(dst="report:bank-1"),
            lambda p: p["relationships"][0].update(rel="allegedly_owned_by"),
            lambda p: p["relationships"][0].update(evidence=""),
            lambda p: p["relationships"][0].update(source_url=""),
            lambda p: p["relationships"].append(copy.deepcopy(p["relationships"][0])),
            lambda p: p["entities"][0].update(name="x" * 1025),
            lambda p: p["entities"][0].update(name="\ud800"),
        ]
        with registry.get_conn(self.db) as conn:
            eid = registry.upsert_entity(conn, "bank", "Caller work", key="caller:1")
            for mutate in mutations:
                payload = self.payload()
                mutate(payload)
                before = conn.total_changes
                with self.subTest(payload=payload), self.assertRaises(ValueError):
                    self.import_payload(conn, payload)
                self.assertEqual(conn.total_changes, before)
                self.assertEqual(registry.stats(conn)["entities"], 1)
                self.assertEqual(registry.get_entity(conn, eid)["name"], "Caller work")

    def test_import_failure_rolls_back_only_savepoint(self):
        with registry.get_conn(self.db) as conn:
            registry.upsert_entity(conn, "bank", "Caller work", key="caller:1")
            with patch.object(importer, "add_edge", side_effect=sqlite3.IntegrityError("forced")):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.import_payload(conn)
            self.assertTrue(conn.in_transaction)
            self.assertEqual(registry.stats(conn)["entities"], 1)
            self.assertEqual(registry.stats(conn)["metrics"], 0)
            self.import_payload(conn)
            conn.rollback()
        with registry.get_conn(self.db) as conn:
            self.assertEqual(registry.stats(conn)["entities"], 0)

    def test_import_never_commits_caller_transaction(self):
        registry.init_db(self.db)
        conn = registry._connect(self.db)
        self.addCleanup(conn.close)
        self.import_payload(conn)
        observer = registry._connect(self.db)
        self.addCleanup(observer.close)
        self.assertEqual(registry.stats(observer)["entities"], 0)
        conn.commit()
        self.assertEqual(registry.stats(observer)["entities"], 2)
        conn.execute("BEGIN")
        self.import_payload(conn)
        conn.rollback()
        self.assertEqual(registry.stats(conn)["entities"], 2)

    def test_failed_standalone_import_restores_transaction_state(self):
        with registry.get_conn(self.db) as conn:
            with patch.object(importer, "add_metric", side_effect=RuntimeError("forced")):
                with self.assertRaises(RuntimeError):
                    self.import_payload(conn)
            self.assertFalse(conn.in_transaction)
            self.assertEqual(registry.stats(conn)["entities"], 0)

    def test_existing_kind_conflict_is_validated_before_writing(self):
        with registry.get_conn(self.db) as conn:
            registry.upsert_entity(conn, "bank", "Other", key="report:regulator-1")
            before = conn.total_changes
            with self.assertRaises(ValueError):
                self.import_payload(conn)
            self.assertEqual(conn.total_changes, before)

    def test_provenance_conventions(self):
        for source in ("local:reports/annual.pdf#page=3", "file:///reports/annual.pdf", "https://www.sec.gov/report"):
            self.assertEqual(importer._provenance(source, "source"), source)
        for source in ("", "local:", "http://www.sec.gov/", "https://localhost/a", "https://127.0.0.1/a",
                       "https://10.0.0.1/a", "https://[::1]/a", "https://user:pass@www.sec.gov/a",
                       "https://host.internal/a", "file://remote/report", "annual.pdf", "https://bad host/a"):
            with self.subTest(source=source), self.assertRaises(ValueError):
                importer._provenance(source, "source")
        self.assertIs(importer.RELATIONSHIPS, registry.RELATIONSHIPS)
        self.assertTrue({"supervises", "shareholder_of", "regulated_by"} <= importer.RELATIONSHIPS)

    def test_json_and_resource_limits(self):
        with registry.get_conn(self.db) as conn:
            for raw in (b'{"entities": [], "entities": []}', b'{', b'[]', b'null', b'\xff',
                        b'{"entities": "bad"}', b'[' * 2000 + b']' * 2000):
                self.path.write_bytes(raw)
                with self.subTest(raw=raw[:40]), self.assertRaises(ValueError):
                    importer.import_json(conn, str(self.path))
            for constant in ("MAX_ENTITIES", "MAX_RELATIONSHIPS", "MAX_RECORDS", "MAX_FILE_BYTES"):
                with patch.object(importer, constant, 0), self.assertRaises(ValueError):
                    self.import_payload(conn)
            self.path.write_bytes(b" " * (importer.MAX_FILE_BYTES + 1))
            with self.assertRaises(ValueError):
                importer.import_json(conn, str(self.path))
            self.assertEqual(registry.stats(conn)["entities"], 0)

    def legacy_db(self, version="2", orphan=False):
        conn = sqlite3.connect(self.db)
        # Hardcoded v2 schema (before lifecycle columns, observations, and new RAG tables)
        schema = """
CREATE TABLE IF NOT EXISTS meta(
  k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS entities(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT NOT NULL UNIQUE CHECK(length(trim(key)) > 0),
  kind TEXT NOT NULL CHECK(length(trim(kind)) > 0),
  name TEXT NOT NULL CHECK(length(trim(name)) > 0),
  country TEXT,
  website TEXT,
  lei TEXT,
  notes TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS attributes(
  entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  k TEXT,
  v TEXT,
  UNIQUE(entity_id, k));
CREATE TABLE IF NOT EXISTS edges(
  src_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  rel TEXT NOT NULL CHECK(length(trim(rel)) > 0),
  dst_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  UNIQUE(src_id, rel, dst_id));
CREATE TABLE IF NOT EXISTS metrics(
  entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  k TEXT,
  v TEXT,
  period TEXT,
  source TEXT,
  UNIQUE(entity_id, k, period, source));
CREATE TABLE IF NOT EXISTS signals(
  entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  ts TEXT DEFAULT CURRENT_TIMESTAMP,
  kind TEXT,
  score REAL,
  direction TEXT,
  rationale TEXT,
  source TEXT,
  ref TEXT);
CREATE INDEX IF NOT EXISTS idx_signals_ent ON signals(entity_id);
CREATE TABLE IF NOT EXISTS filings(
  entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  form TEXT,
  date TEXT,
  title TEXT,
  url TEXT,
  source TEXT,
  UNIQUE(entity_id, form, date, title, url));
CREATE TABLE IF NOT EXISTS ingest_runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  query TEXT NOT NULL DEFAULT '',
  country TEXT NOT NULL DEFAULT '',
  indicator TEXT NOT NULL DEFAULT '',
  category TEXT NOT NULL DEFAULT '',
  started_at TEXT NOT NULL DEFAULT '',
  finished_at TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'completed',
  fetched INTEGER NOT NULL DEFAULT 0,
  stored INTEGER NOT NULL DEFAULT 0,
  skipped INTEGER NOT NULL DEFAULT 0,
  missing INTEGER NOT NULL DEFAULT 0,
  pages INTEGER NOT NULL DEFAULT 0,
  total INTEGER,
  truncated INTEGER NOT NULL DEFAULT 0,
  request_sha256 TEXT NOT NULL DEFAULT '',
  records_sha256 TEXT NOT NULL DEFAULT '',
  warnings TEXT NOT NULL DEFAULT '[]',
  coverage TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS entity_aliases(
  alias_id INTEGER PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,
  canonical_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  reason TEXT NOT NULL DEFAULT '',
  source_url TEXT NOT NULL DEFAULT '',
  CHECK(alias_id != canonical_id));
CREATE INDEX IF NOT EXISTS idx_aliases_canonical ON entity_aliases(canonical_id);
CREATE TABLE IF NOT EXISTS sanctions_listings(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  listing_key TEXT NOT NULL,
  name TEXT NOT NULL,
  entity_type TEXT NOT NULL DEFAULT '',
  program TEXT NOT NULL DEFAULT '',
  country TEXT NOT NULL DEFAULT '',
  basis TEXT NOT NULL DEFAULT 'unknown',
  status TEXT NOT NULL DEFAULT 'active',
  published_at TEXT NOT NULL DEFAULT '',
  effective_at TEXT NOT NULL DEFAULT '',
  source_url TEXT NOT NULL DEFAULT '',
  retrieved_at TEXT NOT NULL DEFAULT '',
  evidence TEXT NOT NULL DEFAULT '',
  UNIQUE(source, listing_key));
CREATE TABLE IF NOT EXISTS sanctions_links(
  listing_id INTEGER NOT NULL REFERENCES sanctions_listings(id) ON DELETE CASCADE,
  entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  reason TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(listing_id, entity_id));
CREATE TABLE IF NOT EXISTS money_flows(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  src_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  dst_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  flow_type TEXT NOT NULL,
  amount TEXT NOT NULL,
  currency TEXT NOT NULL,
  occurred_at TEXT NOT NULL DEFAULT '',
  source_url TEXT NOT NULL DEFAULT '',
  evidence TEXT NOT NULL DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  CHECK(src_id != dst_id));
CREATE INDEX IF NOT EXISTS idx_flows_src ON money_flows(src_id);
CREATE INDEX IF NOT EXISTS idx_flows_dst ON money_flows(dst_id);
CREATE TABLE IF NOT EXISTS entity_links(
  entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  link_type TEXT NOT NULL,
  url TEXT NOT NULL,
  label TEXT NOT NULL DEFAULT '',
  source_url TEXT NOT NULL DEFAULT '',
  observed_at TEXT NOT NULL DEFAULT '',
  evidence TEXT NOT NULL DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(entity_id, link_type, url));
CREATE INDEX IF NOT EXISTS idx_links_entity ON entity_links(entity_id);
CREATE TABLE IF NOT EXISTS event_store(
   id INTEGER PRIMARY KEY AUTOINCREMENT,
   source TEXT NOT NULL,
   event_type TEXT NOT NULL,
   event_subtype TEXT NOT NULL DEFAULT '',
   external_id TEXT NOT NULL DEFAULT '',
   title TEXT NOT NULL DEFAULT '',
   description TEXT NOT NULL DEFAULT '',
   actor_key TEXT NOT NULL DEFAULT '',
   actor_name TEXT NOT NULL DEFAULT '',
   counterparty_key TEXT NOT NULL DEFAULT '',
   counterparty_name TEXT NOT NULL DEFAULT '',
   instrument_key TEXT NOT NULL DEFAULT '',
   occurred_at TEXT NOT NULL DEFAULT '',
   observed_at TEXT NOT NULL DEFAULT '',
   severity TEXT NOT NULL DEFAULT 'unknown',
   confidence REAL NOT NULL DEFAULT 0.0,
   source_url TEXT NOT NULL DEFAULT '',
   evidence TEXT NOT NULL DEFAULT '',
   metadata TEXT NOT NULL DEFAULT '{}',
   created_at TEXT DEFAULT CURRENT_TIMESTAMP,
   UNIQUE(source, event_type, external_id));
CREATE INDEX IF NOT EXISTS idx_event_store_type ON event_store(event_type, occurred_at);
CREATE INDEX IF NOT EXISTS idx_event_store_actor ON event_store(actor_key, occurred_at);
CREATE INDEX IF NOT EXISTS idx_event_store_instrument ON event_store(instrument_key, occurred_at);
CREATE TABLE IF NOT EXISTS event_relationships(
   id INTEGER PRIMARY KEY AUTOINCREMENT,
   source_event_id INTEGER NOT NULL REFERENCES event_store(id) ON DELETE CASCADE,
   target_event_id INTEGER NOT NULL REFERENCES event_store(id) ON DELETE CASCADE,
   relationship TEXT NOT NULL CHECK(length(trim(relationship)) > 0),
   strength REAL NOT NULL DEFAULT 0.0,
   direction TEXT NOT NULL DEFAULT 'causal',
   source_url TEXT NOT NULL DEFAULT '',
   evidence TEXT NOT NULL DEFAULT '',
   created_at TEXT DEFAULT CURRENT_TIMESTAMP,
   UNIQUE(source_event_id, target_event_id, relationship));
CREATE INDEX IF NOT EXISTS idx_event_rel_source ON event_relationships(source_event_id);
CREATE INDEX IF NOT EXISTS idx_event_rel_target ON event_relationships(target_event_id);
CREATE TABLE IF NOT EXISTS price_anomalies(
   id INTEGER PRIMARY KEY AUTOINCREMENT,
   instrument_key TEXT NOT NULL DEFAULT '',
   anomaly_type TEXT NOT NULL,
   observed_at TEXT NOT NULL DEFAULT '',
   occurred_at TEXT NOT NULL DEFAULT '',
   severity TEXT NOT NULL DEFAULT 'unknown',
   score REAL NOT NULL DEFAULT 0.0,
   description TEXT NOT NULL DEFAULT '',
   evidence TEXT NOT NULL DEFAULT '',
   source_url TEXT NOT NULL DEFAULT '',
   metadata TEXT NOT NULL DEFAULT '{}',
   created_at TEXT DEFAULT CURRENT_TIMESTAMP,
   UNIQUE(instrument_key, anomaly_type, observed_at));
CREATE INDEX IF NOT EXISTS idx_price_anomaly_instrument ON price_anomalies(instrument_key, observed_at);
CREATE TABLE IF NOT EXISTS money_flow_attribution(
   id INTEGER PRIMARY KEY AUTOINCREMENT,
   event_id INTEGER NOT NULL REFERENCES event_store(id) ON DELETE CASCADE,
   flow_id INTEGER NOT NULL,
   src_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
   dst_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
   attribution_type TEXT NOT NULL DEFAULT 'direct',
   attribution_score REAL NOT NULL DEFAULT 0.0,
   rationale TEXT NOT NULL DEFAULT '',
   created_at TEXT DEFAULT CURRENT_TIMESTAMP,
   UNIQUE(event_id, flow_id, attribution_type));
CREATE INDEX IF NOT EXISTS idx_money_attrib_event ON money_flow_attribution(event_id);
CREATE INDEX IF NOT EXISTS idx_money_attrib_flow ON money_flow_attribution(flow_id);
"""
        conn.executescript(schema)
        if version is not None:
            conn.execute("INSERT INTO meta VALUES('schema_version', ?)", (version,))
        conn.execute("INSERT INTO entities(id, key, kind, name) VALUES(1, 'legacy:1', 'bank', 'Legacy')")
        conn.execute("INSERT INTO entities(id, key, kind, name) VALUES(2, 'legacy:2', 'regulator', 'Regulator')")
        conn.execute("INSERT INTO edges VALUES(1, 'regulated_by', ?)", (999 if orphan else 2,))
        conn.execute("INSERT INTO metrics VALUES(1, 'assets', '100', '', '')")
        conn.execute("INSERT INTO signals(entity_id, kind, score) VALUES(1, 'test', 1)")
        conn.execute("INSERT INTO attributes VALUES(1, 'source_url', 'local:legacy')")
        conn.commit()
        conn.close()

    def test_legacy_migration_retains_data_and_adds_integrity(self):
        self.legacy_db()
        registry.init_db(self.db)
        registry.init_db(self.db)
        with registry.get_conn(self.db) as conn:
            self.assertEqual(conn.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()[0],
                             str(registry._SCHEMA_VERSION))
            self.assertEqual(registry.entity_payload(conn, 1)["relationships"][0]["evidence"], "")
            self.assertEqual(registry.entity_payload(conn, 1)["metrics"][0]["v"], "100")
            for table in ("attributes", "edges", "metrics", "signals", "filings"):
                self.assertTrue(conn.execute(f"PRAGMA foreign_key_list({table})").fetchall())
            with self.assertRaises(sqlite3.IntegrityError):
                registry.add_edge(conn, 1, "owned_by", 999)
            registry.add_edge(conn, 1, "regulated_by", 2, "local:register", "2026", "Published register")
            self.assertEqual(registry.entity_payload(conn, 1)["relationships"][0]["evidence"], "Published register")

    def test_unversioned_migration(self):
        self.legacy_db(version=None)
        with registry.get_conn(self.db) as conn:
            self.assertEqual(registry.stats(conn)["entities"], 2)

    def test_orphan_migration_is_atomic_and_explains_itself(self):
        self.legacy_db(orphan=True)
        with self.assertRaises(RegistryMigrationError) as caught:
            registry.init_db(self.db)
        message = str(caught.exception)
        self.assertIn("edges", message)
        self.assertIn("foreign_key_check", message,
                      "the operator has to be told how to find the offending rows")
        self.assertIn("1 row", message, "the count of broken rows is part of the report")
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT v FROM meta").fetchone()[0], "2")
        self.assertEqual(len(conn.execute("PRAGMA table_info(edges)").fetchall()), 3)
        self.assertEqual(conn.execute("SELECT dst_id FROM edges").fetchone()[0], 999)
        self.assertEqual(conn.execute("SELECT name FROM sqlite_master WHERE name LIKE '_registry_migrate_%'").fetchall(), [])

    def test_schema_version_constants_agree(self):
        self.assertEqual(registry._SCHEMA_VERSION, constants.REGISTRY_SCHEMA_VERSION)

    def test_entity_resolution_candidates_and_reversible_merge(self):
        with registry.get_conn(self.db) as conn:
            first = registry.upsert_entity(conn, "bank", "Acme Bank", key="local:a", country="US",
                                           lei="123456789012345678")
            second = registry.upsert_entity(conn, "bank", "Acme  Bank", key="local:b", country="US")
            third = registry.upsert_entity(conn, "bank", "Acme Bank", key="local:c", country="GB")
            fourth = registry.upsert_entity(conn, "bank", "Other", key="local:d",
                                            lei="123456789012345678")
            candidates = registry.matching_candidates(conn, limit=20)
            by_pair = {(x["left_id"], x["right_id"]): x for x in candidates}
            self.assertEqual(by_pair[(first, fourth)]["reason"], "same_lei")
            self.assertEqual(by_pair[(first, second)]["reason"], "same_name_and_country")
            self.assertEqual(by_pair[(first, third)]["reason"], "same_name")
            self.assertTrue(all(x["review"] == "unreviewed_candidate" for x in candidates))
            self.assertEqual(registry.merge_entity(conn, second, first, "same bank")["canonical_id"],
                             first)
            self.assertEqual(registry.resolve_entity_id(conn, second), first)
            after = registry.matching_candidates(conn, limit=20)
            self.assertTrue(all(second not in (x["left_id"], x["right_id"]) for x in after))
            self.assertEqual([(x["alias_id"], x["canonical_id"]) for x in registry.list_aliases(conn)],
                             [(second, first)])
            with self.assertRaises(ValueError):
                registry.merge_entity(conn, second, third)
            with self.assertRaises(ValueError):
                registry.merge_entity(conn, first, first)
            with self.assertRaises(ValueError):
                registry.merge_entity(conn, third, second)
            with self.assertRaises(ValueError):
                registry.merge_entity(conn, first, third)
            self.assertEqual(registry.unmerge_entity(conn, second), 1)
            self.assertEqual(registry.unmerge_entity(conn, second), 0)
            self.assertEqual(registry.resolve_entity_id(conn, second), second)
            for kwargs in ({"limit": 0}, {"eid": 0}):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    registry.matching_candidates(conn, **kwargs)

    def test_un_consolidated_parser_and_fetch_offline(self):
        xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<CONSOLIDATED_LIST>\n'
               '  <INDIVIDUALS>\n'
               '    <INDIVIDUAL><DATAID>1</DATAID><FIRST_NAME>ERIC</FIRST_NAME>'
               '<SECOND_NAME>BADEGE</SECOND_NAME><UN_LIST_TYPE>DRC</UN_LIST_TYPE>'
               '<REFERENCE_NUMBER>CDi.001</REFERENCE_NUMBER><LISTED_ON>2012-12-31</LISTED_ON>'
               '<NATIONALITY><VALUE>CONGO, DEM. REP. OF</VALUE></NATIONALITY>'
               '<COMMENTS1>Sample comment</COMMENTS1></INDIVIDUAL>\n'
               '    <INDIVIDUAL><FIRST_NAME>NO KEY</FIRST_NAME></INDIVIDUAL>\n'
               '  </INDIVIDUALS>\n'
               '  <ENTITIES>\n'
               '    <ENTITY><DATAID>2</DATAID><FIRST_NAME>ADF</FIRST_NAME>'
               '<UN_LIST_TYPE>DRC</UN_LIST_TYPE><REFERENCE_NUMBER>CDe.001</REFERENCE_NUMBER>'
               '<LISTED_ON>2014-06-30</LISTED_ON>'
               '<ENTITY_ADDRESS><COUNTRY>CONGO, DEM. REP. OF</COUNTRY></ENTITY_ADDRESS>'
               '</ENTITY>\n'
               '  </ENTITIES>\n</CONSOLIDATED_LIST>')
        listings, skipped = sanctions.parse_un_consolidated(xml)
        self.assertEqual([(item["key"], item["name"], item["type"]) for item in listings],
                         [("CDi.001", "ERIC BADEGE", "Individual"), ("CDe.001", "ADF", "Entity")])
        self.assertEqual(skipped, 1)
        payload = xml.encode("utf-8")

        class Fake:
            def __init__(self, *args, **kwargs):
                self.retrieved_at = "2026-01-01T00:00:00+00:00"

            def download(self, url, path, max_bytes):
                with open(path, "wb") as stream:
                    stream.write(payload)
                return len(payload)

        class Dtd:
            def __init__(self, *args, **kwargs):
                self.retrieved_at = "2026-01-01T00:00:00+00:00"

            def download(self, url, path, max_bytes):
                with open(path, "wb") as stream:
                    stream.write(b'<!DOCTYPE x><CONSOLIDATED_LIST/>')
                return 32

        with registry.get_conn(self.db) as conn:
            with patch.object(sanctions, "HTTPClient", Fake):
                report = sanctions.fetch_un(conn, "un-consolidated")
            self.assertEqual((report["parsed"], report["imported"], report["delisted"],
                              report["skipped"]), (2, 2, 0, 1))
            stored = {item["listing_key"]: item
                      for item in registry.list_sanctions_listings(conn, limit=10)}
            self.assertEqual({item["source"] for item in stored.values()}, {"un-consolidated"})
            self.assertEqual(stored["CDi.001"]["published_at"], "2012-12-31")
            self.assertEqual(stored["CDi.001"]["basis"], "designation")
            self.assertEqual(stored["CDe.001"]["country"], "CONGO, DEM. REP. OF")
            with patch.object(sanctions, "HTTPClient", Dtd), self.assertRaises(ValueError):
                sanctions.fetch_un(conn, "un-consolidated")
            with self.assertRaises(ValueError):
                sanctions.fetch_sanctions(conn, "unsupported-source")

    def test_uk_ofsi_parser_and_fetch_offline(self):
        header = ["Name 6", "Name 1", "Name 2", "Name 3", "Name 4", "Name 5", "Title",
                  "Name Non-Latin Script", "Non-Latin Script Type", "Non-Latin Script Language",
                  "DOB", "Town of Birth", "Country of Birth", "Nationality", "Passport Number",
                  "Passport Details", "National Identification Number",
                  "National Identification Details", "Position", "Address 1", "Address 2",
                  "Address 3", "Address 4", "Address 5", "Address 6", "Post/Zip Code", "Country",
                  "Other Information", "Group Type", "Alias Type", "Alias Quality", "Regime",
                  "Listed On", "UK Sanctions List Date Designated", "Last Updated", "Group ID"]

        def uk_row(**values):
            row = dict.fromkeys(header, "")
            row.update(values)
            return ",".join('"' + str(row[name]).replace('"', '""') + '"' for name in header)

        text = ("Last Updated,01/01/2026\r\n" + ",".join(header) + "\r\n"
                + uk_row(**{"Name 6": "ABAHUSSAIN", "Name 1": "Mansour", "Group Type": "Individual",
                            "Alias Type": "Primary name", "Regime": "Global Human Rights",
                            "Listed On": "09/12/2022", "Group ID": "15672", "Country": "Pakistan",
                            "Other Information": "note"}) + "\r\n"
                + uk_row(**{"Name 6": "ABAHUSSAIN", "Name 1": "Mansour ALIAS",
                            "Group Type": "Individual", "Alias Type": "AKA",
                            "Regime": "Global Human Rights", "Listed On": "09/12/2022",
                            "Group ID": "15672"}) + "\r\n"
                + uk_row(**{"Name 6": "ACME SHIPPING", "Group Type": "Entity",
                            "Alias Type": "Primary name", "Regime": "Iran",
                            "Listed On": "01/02/2024", "Group ID": "14042",
                            "Country": "Iran"}) + "\r\n"
                + uk_row(**{"Group Type": "Individual", "Alias Type": "Primary name",
                            "Group ID": "99999"}))
        listings, skipped = sanctions.parse_uk_ofsi(text)
        self.assertEqual([(item["key"], item["name"], item["type"]) for item in listings],
                         [("14042", "ACME SHIPPING", "Entity"),
                          ("15672", "Mansour ABAHUSSAIN", "Individual")])
        self.assertEqual(skipped, 1)
        by_key = {item["key"]: item for item in listings}
        self.assertEqual(by_key["15672"]["listed_on"], "2022-12-09")
        self.assertEqual(by_key["15672"]["country"], "Pakistan")
        payload = text.encode("utf-8")

        class Fake:
            def __init__(self, *args, **kwargs):
                self.retrieved_at = "2026-01-01T00:00:00+00:00"

            def download(self, url, path, max_bytes):
                with open(path, "wb") as stream:
                    stream.write(payload)
                return len(payload)

        with registry.get_conn(self.db) as conn:
            with patch.object(sanctions, "HTTPClient", Fake):
                report = sanctions.fetch_uk(conn, "uk-ofsi")
            self.assertEqual((report["parsed"], report["imported"], report["skipped"]), (2, 2, 1))
            stored = {item["listing_key"]: item
                      for item in registry.list_sanctions_listings(conn, limit=10)}
            self.assertEqual(stored["15672"]["published_at"], "2022-12-09")
            self.assertEqual(stored["15672"]["basis"], "designation")
            self.assertEqual(set(stored), {"14042", "15672"})

    def test_eu_consolidated_parser_and_fetch_offline(self):
        xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
               '<export xmlns="http://eu.europa.ec/fpi/fsd/export">\n'
               '  <sanctionEntity euReferenceNumber="EU.27.28" logicalId="13">\n'
               '    <regulation entryIntoForceDate="2003-07-07" publicationDate="2003-07-08"'
               ' numberTitle="1210/2003" programme="IRQ"/>\n'
               '    <subjectType code="person" classificationCode="P"/>\n'
               '    <nameAlias firstName="Saddam" lastName="Hussein" wholeName="Saddam Hussein"'
               ' strong="true" nameLanguage="en"/>\n'
               '    <nameAlias wholeName="Abu Ali" strong="true" nameLanguage="FR"/>\n'
               '    <citizenship countryIso2Code="IQ"/>\n'
               '  </sanctionEntity>\n'
               '  <sanctionEntity euReferenceNumber="EU.39.56" logicalId="20">\n'
               '    <regulation entryIntoForceDate="2003-07-07" programme="IRQ"'
               ' numberTitle="1210/2003"/>\n'
               '    <subjectType code="enterprise" classificationCode="E"/>\n'
               '    <nameAlias wholeName="ACME ENTERPRISE" strong="true" nameLanguage="en"/>\n'
               '    <address countryIso2Code="IR"/>\n'
               '  </sanctionEntity>\n'
               '  <sanctionEntity logicalId="99"><subjectType code="person"/></sanctionEntity>\n'
               '</export>')
        listings, skipped = sanctions.parse_eu_consolidated(xml)
        by_key = {item["key"]: item for item in listings}
        self.assertEqual(set(by_key), {"EU.27.28", "EU.39.56"})
        self.assertEqual((by_key["EU.27.28"]["name"], by_key["EU.27.28"]["type"]),
                         ("Saddam Hussein", "Individual"))
        self.assertEqual((by_key["EU.27.28"]["program"], by_key["EU.27.28"]["country"],
                          by_key["EU.27.28"]["listed_on"]), ("IRQ", "IQ", "2003-07-07"))
        self.assertEqual((by_key["EU.39.56"]["type"], by_key["EU.39.56"]["country"]),
                         ("Entity", "IR"))
        self.assertEqual(skipped, 1)
        payload = xml.encode("utf-8")

        class Fake:
            def __init__(self, *args, **kwargs):
                self.retrieved_at = "2026-01-01T00:00:00+00:00"

            def download(self, url, path, max_bytes):
                with open(path, "wb") as stream:
                    stream.write(payload)
                return len(payload)

        class Dtd:
            def __init__(self, *args, **kwargs):
                self.retrieved_at = "2026-01-01T00:00:00+00:00"

            def download(self, url, path, max_bytes):
                with open(path, "wb") as stream:
                    stream.write(b'<!DOCTYPE x><export/>')
                return 20

        with registry.get_conn(self.db) as conn:
            with patch.object(sanctions, "HTTPClient", Fake):
                report = sanctions.fetch_eu(conn, "eu-consolidated")
            self.assertEqual((report["parsed"], report["imported"], report["skipped"]), (2, 2, 1))
            stored = {item["listing_key"]: item
                      for item in registry.list_sanctions_listings(conn, limit=10)}
            self.assertEqual(set(stored), {"EU.27.28", "EU.39.56"})
            self.assertEqual(stored["EU.27.28"]["published_at"], "2003-07-07")
            with patch.object(sanctions, "HTTPClient", Dtd), self.assertRaises(ValueError):
                sanctions.fetch_eu(conn, "eu-consolidated")

    def test_sanctions_delisting_preserves_rows(self):
        with registry.get_conn(self.db) as conn:
            keep = registry.add_sanctions_listing(conn, "ofac-sdn", "1", "Keep",
                                                  basis="designation")
            gone = registry.add_sanctions_listing(conn, "ofac-sdn", "2", "Gone",
                                                  basis="designation")
            registry.add_sanctions_listing(conn, "eu-consolidated", "2", "Gone",
                                           basis="designation")
            self.assertEqual(registry.mark_delisted(conn, "ofac-sdn", ["1"]), 1)
            statuses = {item["id"]: item["status"]
                        for item in registry.list_sanctions_listings(conn, limit=10)}
            self.assertEqual(statuses[keep], "active")
            self.assertEqual(statuses[gone], "delisted")
            self.assertEqual(sum(1 for value in statuses.values() if value == "active"), 2)
            self.assertEqual(registry.mark_delisted(conn, "ofac-sdn", ["1"]), 0)
            registry.add_sanctions_listing(conn, "ofac-sdn", "2", "Gone", basis="designation",
                                           status="active")
            statuses = {item["id"]: item["status"]
                        for item in registry.list_sanctions_listings(conn, limit=10)}
            self.assertEqual(statuses[gone], "active")

    def test_ofac_sdn_parser_and_live_fetch_offline(self):
        text = ('36,"AEROCARIBBEAN AIRLINES",-0- ,"CUBA",-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,-0- \r\n'
                '2676,"ACME SHIPPING","Entity","IRAN",-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,-0- \r\n'
                'bad,row\r\n')
        listings, skipped = sanctions.parse_ofac_sdn(text)
        self.assertEqual([(item["key"], item["name"], item["type"]) for item in listings],
                         [("36", "AEROCARIBBEAN AIRLINES", ""), ("2676", "ACME SHIPPING", "Entity")])
        self.assertEqual(skipped, 1)
        payload = text.encode("utf-8")

        class Fake:
            current = payload

            def __init__(self, *args, **kwargs):
                self.retrieved_at = "2026-01-01T00:00:00+00:00"

            def download(self, url, path, max_bytes):
                with open(path, "wb") as stream:
                    stream.write(Fake.current)
                return len(Fake.current)

        with registry.get_conn(self.db) as conn:
            with patch.object(sanctions, "HTTPClient", Fake):
                report = sanctions.fetch_ofac(conn, "ofac-sdn")
                self.assertEqual((report["parsed"], report["imported"], report["delisted"],
                                  report["skipped"]), (2, 2, 0, 1))
                stored = registry.list_sanctions_listings(conn, limit=10)
                self.assertEqual(len(stored), 2)
                self.assertEqual(stored[0]["basis"], "designation")
                Fake.current = (b'36,"AEROCARIBBEAN AIRLINES",-0- ,"CUBA",-0- ,-0- ,-0- ,-0- ,-0- ,'
                                b'-0- ,-0- ,-0- \r\n')
                second = sanctions.fetch_ofac(conn, "ofac-sdn")
            self.assertEqual((second["parsed"], second["delisted"]), (1, 1))
            statuses = {item["listing_key"]: item["status"]
                        for item in registry.list_sanctions_listings(conn, limit=10)}
            self.assertEqual(statuses, {"36": "active", "2676": "delisted"})
            run = registry.list_ingest_runs(conn, 2)[0]
            self.assertEqual((run["source"], run["stored"], run["fetched"]), ("ofac-sdn", 1, 1))
            self.assertTrue(any("delisted" in warning for warning in run["warnings"]))
            with self.assertRaises(ValueError):
                sanctions.fetch_ofac(conn, "unknown")

    def test_sanctions_listings_candidates_and_links(self):
        with registry.get_conn(self.db) as conn:
            entity = registry.upsert_entity(conn, "legal_entity", "Acme Bank", key="lei:acme",
                                            country="US")
            listing = registry.add_sanctions_listing(
                conn, "ofac-sdn", "SDN-1", "Acme Bank", entity_type="Entity", program="SDGT",
                country="US", basis="designation", status="active", published_at="2026-01-01",
                effective_at="2026-01-02", source_url="https://example.gov/sdn",
                retrieved_at="2026-01-03T00:00:00Z", evidence="SDN list entry")
            self.assertEqual(registry.add_sanctions_listing(conn, "ofac-sdn", "SDN-1", "Acme Bank",
                                                            basis="designation"), listing)
            registry.add_sanctions_listing(conn, "ofac-sdn", "SDN-1", "Acme Bank",
                                           status="delisted", basis="designation")
            self.assertEqual(registry.list_sanctions_listings(conn, active_only=True), [])
            registry.add_sanctions_listing(conn, "ofac-sdn", "SDN-1", "Acme Bank", status="active",
                                           basis="designation")
            candidates = registry.sanctions_candidates(conn)
            self.assertEqual([(c["listing_id"], c["entity_id"], c["review"]) for c in candidates],
                             [(listing, entity, "unreviewed_candidate")])
            self.assertEqual(registry.sanctions_candidates(conn, entity), candidates)
            linked = registry.link_sanctions(conn, listing, entity, "reviewed by analyst")
            self.assertEqual(linked["edge"], "sanctioned_by")
            self.assertEqual(registry.sanctions_candidates(conn), [])
            links = registry.sanctions_links(conn)
            self.assertEqual((links[0]["listing_name"], links[0]["entity_name"], links[0]["basis"]),
                             ("Acme Bank", "Acme Bank", "designation"))
            edges = conn.execute("SELECT rel, status, seen_count, evidence FROM edges"
                                 " WHERE src_id=? AND rel='sanctioned_by'", (entity,)).fetchall()
            self.assertEqual(len(edges), 1)
            self.assertEqual((edges[0]["status"], edges[0]["seen_count"]), ("active", 1))
            self.assertIn("not guilt", edges[0]["evidence"])
            authority = registry.entity_payload(conn, linked["authority_id"])
            self.assertEqual(authority["kind"], "authority")
            second = registry.add_sanctions_listing(conn, "ofac-sdn", "SDN-3", "Acme Bank",
                                                    basis="designation")
            registry.link_sanctions(conn, second, entity, "second listing")
            self.assertEqual(registry.unlink_sanctions(conn, listing, entity), 1)
            edges = conn.execute("SELECT status FROM edges WHERE src_id=? AND rel='sanctioned_by'",
                                 (entity,)).fetchall()
            self.assertEqual([edge["status"] for edge in edges], ["active"])
            self.assertEqual(registry.unlink_sanctions(conn, second, entity), 1)
            edges = conn.execute("SELECT status FROM edges WHERE src_id=? AND rel='sanctioned_by'",
                                 (entity,)).fetchall()
            self.assertEqual([edge["status"] for edge in edges], ["retracted"])
            self.assertEqual(registry.unlink_sanctions(conn, listing, entity), 0)
            for kwargs in ({"basis": "hunch"}, {"status": "maybe"}):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    registry.add_sanctions_listing(conn, "ofac-sdn", "SDN-2", "X", **kwargs)
            with self.assertRaises(ValueError):
                registry.link_sanctions(conn, 999, entity)
            with self.assertRaises(ValueError):
                registry.sanctions_candidates(conn, 999)

    def test_fuzzy_name_candidates_require_review(self):
        with registry.get_conn(self.db) as conn:
            first = registry.upsert_entity(conn, "legal_entity", "Acme Bank", key="lei:a",
                                           country="US")
            second = registry.upsert_entity(conn, "legal_entity", "Acme Bank Holdings Ltd",
                                            key="lei:b", country="US")
            registry.upsert_entity(conn, "legal_entity", "Beta Corp", key="lei:c", country="US")
            self.assertEqual(registry.matching_candidates(conn, first), [])
            fuzzy = registry.matching_candidates(conn, first, fuzzy=True)
            self.assertEqual([(item["right_id"], item["reason"], item["review"]) for item in fuzzy],
                             [(second, "fuzzy_name", "unreviewed_candidate")])
            with self.assertRaises(ValueError):
                registry.matching_candidates(conn, fuzzy=True)
            registry.add_sanctions_listing(conn, "ofac-sdn", "L1", "ACME BANK HOLDINGS",
                                           basis="designation")
            self.assertEqual(registry.sanctions_candidates(conn, first), [])
            matches = registry.sanctions_candidates(conn, first, fuzzy=True)
            self.assertEqual([(item["listing_name"], item["reason"]) for item in matches],
                             [("ACME BANK HOLDINGS", "fuzzy_name")])
            with self.assertRaises(ValueError):
                registry.sanctions_candidates(conn, fuzzy=True)

    def test_entity_links_with_provenance(self):
        with registry.get_conn(self.db) as conn:
            eid = registry.upsert_entity(conn, "bank", "Linked", key="local:linked")
            record = registry.set_entity_link(conn, eid, "website", "https://linked.example",
                                              "Main site", "https://register.example/1",
                                              "2026-01-01T00:00:00Z", "published in register")
            self.assertEqual(record["review"], "verified_link_recorded")
            links = registry.list_entity_links(conn, eid)
            self.assertEqual([(x["link_type"], x["url"], x["source_url"]) for x in links],
                             [("website", "https://linked.example", "https://register.example/1")])
            registry.set_entity_link(conn, eid, "website", "https://linked.example", "Updated",
                                     "https://register.example/2")
            links = registry.list_entity_links(conn, eid)
            self.assertEqual((links[0]["label"], links[0]["source_url"]),
                             ("Updated", "https://register.example/2"))
            registry.set_entity_link(conn, eid, "social", "https://social.example/x",
                                     source_url="https://register.example/1")
            self.assertEqual(len(registry.list_entity_links(conn, eid)), 2)
            for change in ({"link_type": "guess"}, {"url": ""}, {"source_url": ""}):
                args = {"entity_id": eid, "link_type": "website", "url": "https://a.example",
                        "source_url": "https://r.example"}
                args.update(change)
                with self.subTest(change=change), self.assertRaises(ValueError):
                    registry.set_entity_link(conn, **args)
            with self.assertRaises(ValueError):
                registry.set_entity_link(conn, 999, "website", "https://a.example",
                                         source_url="https://r.example")
            self.assertEqual(registry.remove_entity_link(conn, eid, "https://linked.example"), 1)
            self.assertEqual(registry.remove_entity_link(conn, eid, "https://linked.example"), 0)
            with self.assertRaises(ValueError):
                registry.list_entity_links(conn, 999)

    def test_money_flows_documented_only(self):
        with registry.get_conn(self.db) as conn:
            payer = registry.upsert_entity(conn, "bank", "Payer", key="local:payer")
            payee = registry.upsert_entity(conn, "bank", "Payee", key="local:payee")
            self.assertEqual(registry.entity_id_by_key(conn, "local:payer"), payer)
            self.assertIsNone(registry.entity_id_by_key(conn, "local:absent"))
            registry.add_money_flow(conn, payer, payee, "loan", "1000", "USD", "2026-01-01",
                                    "https://example.gov/loan", "loan agreement")
            flows = registry.list_money_flows(conn)
            self.assertEqual(len(flows), 1)
            self.assertEqual((flows[0]["flow_type"], flows[0]["amount"], flows[0]["currency"],
                              flows[0]["src_name"], flows[0]["dst_name"]),
                             ("loan", "1000", "USD", "Payer", "Payee"))
            self.assertEqual(registry.list_money_flows(conn, payer), flows)
            self.assertEqual(registry.list_money_flows(conn, payee), flows)
            for change in ({"flow_type": "guess"}, {"amount": "0"}, {"amount": "-5"},
                           {"amount": "abc"}, {"currency": ""}):
                args = {"src_id": payer, "dst_id": payee, "flow_type": "loan", "amount": "1",
                        "currency": "USD"}
                args.update(change)
                with self.subTest(change=change), self.assertRaises(ValueError):
                    registry.add_money_flow(conn, **args)
            with self.assertRaises(ValueError):
                registry.add_money_flow(conn, payer, payer, "loan", "1", "USD")
            with self.assertRaises(ValueError):
                registry.add_money_flow(conn, payer, 999, "loan", "1", "USD")
            with self.assertRaises(ValueError):
                registry.list_money_flows(conn, 999)

    def test_money_flow_summary_per_currency(self):
        with registry.get_conn(self.db) as conn:
            alpha = registry.upsert_entity(conn, "bank", "Alpha", key="local:alpha")
            beta = registry.upsert_entity(conn, "bank", "Beta", key="local:beta")
            gamma = registry.upsert_entity(conn, "bank", "Gamma", key="local:gamma")
            registry.add_money_flow(conn, beta, alpha, "loan", "1000", "USD", "2026-01-01",
                                    "https://x/1", "e")
            registry.add_money_flow(conn, alpha, gamma, "grant", "400", "USD", "2026-01-02",
                                    "https://x/2", "e")
            registry.add_money_flow(conn, gamma, alpha, "fee", "100", "EUR", "2026-01-03",
                                    "https://x/3", "e")
            summary = registry.money_flow_summary(conn, alpha)
            self.assertEqual(summary["entity_id"], alpha)
            usd = next(item for item in summary["currencies"] if item["currency"] == "USD")
            self.assertEqual((usd["inflow"], usd["outflow"], usd["net"]),
                             ("1000", "400", "600"))
            self.assertEqual((usd["inflow_count"], usd["outflow_count"]), (1, 1))
            partners = {item["name"]: item for item in usd["counterparties"]}
            self.assertEqual((partners["Beta"]["inflow"], partners["Beta"]["outflow"]),
                             ("1000", "0"))
            self.assertEqual((partners["Gamma"]["inflow"], partners["Gamma"]["outflow"]),
                             ("0", "400"))
            eur = next(item for item in summary["currencies"] if item["currency"] == "EUR")
            self.assertEqual((eur["inflow"], eur["outflow"], eur["net"]), ("100", "0", "100"))
            self.assertFalse(summary["coverage"]["cross_currency_netting"])
            with self.assertRaises(ValueError):
                registry.money_flow_summary(conn, 999)
            with self.assertRaises(ValueError):
                registry.money_flow_summary(conn, alpha, limit=0)

    def test_kind_catalog(self):
        kinds = registry.list_kinds()
        self.assertEqual([item["kind"] for item in kinds], sorted(item["kind"] for item in kinds))
        by_kind = {item["kind"]: item for item in kinds}
        self.assertTrue({"bank", "fund", "legal_entity", "company", "instrument", "economy"}
                        <= set(by_kind))
        for item in kinds:
            self.assertTrue(item["definition"].strip())
            self.assertTrue(item["produced_by"].strip())
        self.assertIn("not proof", by_kind["fund"]["definition"])
        self.assertIn("not a legal entity", by_kind["instrument"]["definition"])

    def test_health_check_valid_absent_and_corrupt(self):
        with registry.get_conn(self.db) as conn:
            registry.upsert_entity(conn, "bank", "Healthy", key="local:h")
        report = registry.health_check(self.db)
        self.assertEqual(report["status"], "ok")
        self.assertTrue(report["python"]["supported"])
        self.assertTrue(report["sqlite"]["supported"])
        self.assertEqual(report["python"]["implementation"], platform.python_implementation())
        self.assertEqual(report["platform"]["system"], sys.platform)
        self.assertEqual(report["registry"]["integrity_check"], "ok")
        self.assertEqual(report["registry"]["foreign_key_violations"], 0)
        self.assertEqual(report["registry"]["schema_version"], registry._SCHEMA_VERSION)
        self.assertEqual(report["registry"]["entities"], 1)
        absent = registry.health_check(str(Path(self.temp.name) / "absent.db"))
        self.assertEqual((absent["status"], absent["registry"]["present"]), ("ok", False))
        self.assertFalse((Path(self.temp.name) / "absent.db").exists())
        raw = sqlite3.connect(self.db)
        raw.execute("UPDATE meta SET v='invalid' WHERE k='schema_version'")
        raw.commit()
        raw.close()
        self.assertEqual(registry.health_check(self.db)["status"], "issues")

    def test_backup_database_is_consistent_and_protected(self):
        with registry.get_conn(self.db) as conn:
            registry.upsert_entity(conn, "bank", "Backed Up", key="local:backup")
        target = str(Path(self.temp.name) / "backup.db")
        report = registry.backup_database(self.db, target)
        self.assertEqual((report["entities"], report["schema_version"]),
                         (1, str(registry._SCHEMA_VERSION)))
        self.assertGreater(report["bytes"], 0)
        self.assertEqual(len(report["sha256"]), 64)
        with registry.get_conn(target) as conn:
            self.assertEqual(conn.execute("SELECT name FROM entities").fetchone()[0], "Backed Up")
        with self.assertRaises(ValueError):
            registry.backup_database(self.db, target)
        self.assertEqual(registry.backup_database(self.db, target, force=True)["entities"], 1)
        with self.assertRaises(ValueError):
            registry.backup_database(self.db, self.db, force=True)
        with self.assertRaises(ValueError):
            registry.backup_database(self.db, str(Path(self.temp.name) / "missing" / "b.db"))
        with self.assertRaises(ValueError):
            registry.backup_database(str(Path(self.temp.name) / "absent.db"), target, force=True)
        with self.assertRaises(ValueError):
            registry.backup_database(self.db, target, force="yes")

    def test_find_entities_separates_country_dimensions(self):
        with registry.get_conn(self.db) as conn:
            alpha = registry.upsert_entity(conn, "legal_entity", "Alpha", key="lei:1", country="US")
            registry.set_attr(conn, alpha, "iso_jurisdiction", "US-DE")
            registry.set_attr(conn, alpha, "gleif.headquarters_country", "US")
            registry.set_attr(conn, alpha, "source", "gleif")
            beta = registry.upsert_entity(conn, "legal_entity", "Beta", key="lei:2", country="GB")
            registry.set_attr(conn, beta, "iso_jurisdiction", "GB-ENG")
            registry.set_attr(conn, beta, "gleif.headquarters_country", "IE")
            registry.set_attr(conn, beta, "source", "fdic")
            self.assertEqual([r["name"] for r in registry.find_entities(conn, legal_country="US")],
                             ["Alpha"])
            self.assertEqual([r["name"] for r in registry.find_entities(conn, jurisdiction="GB-ENG")],
                             ["Beta"])
            self.assertEqual(
                [r["name"] for r in registry.find_entities(conn, headquarters_country="IE")],
                ["Beta"])
            self.assertEqual(registry.find_entities(conn, legal_country="us"), [])
            self.assertEqual([r["name"] for r in registry.find_entities(conn, country="US")],
                             ["Alpha"])
            self.assertEqual([r["name"] for r in registry.find_entities(conn, source="fdic")],
                             ["Beta"])

    def test_entity_tree_traversal_and_status(self):
        with registry.get_conn(self.db) as conn:
            top = registry.upsert_entity(conn, "legal_entity", "Top", key="lei:top", country="US")
            mid = registry.upsert_entity(conn, "legal_entity", "Mid", key="lei:mid", country="US")
            leaf = registry.upsert_entity(conn, "legal_entity", "Leaf", key="lei:leaf", country="GB")
            other = registry.upsert_entity(conn, "legal_entity", "Other", key="lei:other", country="GB")
            registry.add_edge(conn, mid, "subsidiary_of", top, "local:r", "2026", "e")
            registry.add_edge(conn, leaf, "ultimate_subsidiary_of", top, "local:r", "2026", "e")
            registry.set_attr(conn, other, "gleif.direct_parent_exception", '{"reason":"no LEI"}')
            tree = registry.entity_tree(conn, top, depth=3, direction="both")
            self.assertEqual(tree["method"], "gleif_consolidation_tree_v1")
            by_id = {node["id"]: node for node in tree["nodes"]}
            self.assertEqual(set(by_id), {top, mid, leaf})
            self.assertEqual((by_id[top]["direction"], by_id[mid]["direction"]),
                             ("self", "descendant"))
            self.assertEqual(by_id[mid]["via_rel"], "subsidiary_of")
            self.assertEqual(by_id[leaf]["via_rel"], "ultimate_subsidiary_of")
            self.assertEqual(by_id[top]["parent_status"], "unknown")
            self.assertEqual((by_id[top]["level"], by_id[mid]["level"]), (0, 1))
            parents = registry.entity_tree(conn, leaf, depth=1, direction="parents")
            self.assertEqual([(node["id"], node["direction"]) for node in parents["nodes"]],
                             [(leaf, "self"), (top, "ancestor")])
            exception = registry.entity_tree(conn, other, direction="both")
            self.assertEqual(exception["nodes"][0]["parent_status"], "exception")
            self.assertIn("gleif.direct_parent_exception",
                          exception["nodes"][0]["parent_exceptions"])
            bounded = registry.entity_tree(conn, top, depth=3, max_nodes=1)
            self.assertEqual(len(bounded["nodes"]), 1)
            self.assertTrue(bounded["coverage"]["truncated"])
            for kwargs in ({"depth": 0}, {"depth": 21}, {"max_nodes": 0},
                           {"direction": "sideways"}):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    registry.entity_tree(conn, top, **kwargs)
            with self.assertRaises(ValueError):
                registry.entity_tree(conn, 999)

    def test_entity_tree_skips_cycles(self):
        with registry.get_conn(self.db) as conn:
            first = registry.upsert_entity(conn, "legal_entity", "First", key="lei:f")
            second = registry.upsert_entity(conn, "legal_entity", "Second", key="lei:s")
            registry.add_edge(conn, first, "subsidiary_of", second)
            registry.add_edge(conn, second, "subsidiary_of", first)
            tree = registry.entity_tree(conn, first, depth=5, direction="both")
            self.assertGreaterEqual(tree["coverage"]["cycle_edges_skipped"], 1)
            self.assertEqual(len(tree["nodes"]), 2)

    def test_ingest_run_records_and_bounds(self):
        with registry.get_conn(self.db) as conn:
            run_id = registry.record_ingest_run(
                conn, "gleif", "2026-09-18T00:00:00Z", "2026-09-18T00:00:05Z", query="acme",
                country="US", fetched=10, stored=8, skipped=1, missing=1, pages=2, total=10,
                truncated=False, request_sha256="a" * 64, records_sha256="b" * 64,
                warnings=["partial"], coverage="coverage text")
            runs = registry.list_ingest_runs(conn, 10)
        self.assertEqual(run_id, runs[0]["id"])
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertEqual((run["source"], run["query"], run["country"], run["fetched"],
                          run["stored"], run["skipped"], run["missing"], run["pages"],
                          run["total"], run["status"]), ("gleif", "acme", "US", 10, 8, 1, 1, 2, 10,
                                                         "completed"))
        self.assertEqual((run["request_sha256"], run["records_sha256"]), ("a" * 64, "b" * 64))
        self.assertEqual(run["warnings"], ["partial"])
        self.assertFalse(run["truncated"])
        with registry.get_conn(self.db) as conn, self.assertRaises(ValueError):
            registry.list_ingest_runs(conn, 0)

    def test_edge_lifecycle_and_retraction_preserve_history(self):
        with registry.get_conn(self.db) as conn:
            src = registry.upsert_entity(conn, "fund", "Fund A", key="lei:00000000000000000009")
            dst = registry.upsert_entity(conn, "legal_entity", "Manager B", key="lei:00000000000000000010")
            registry.add_edge(conn, src, "managed_by", dst, "local:first", "2026-01-01T00:00:00Z",
                              "first sighting")
            row = conn.execute(
                "SELECT first_seen_at, last_seen_at, seen_count, status, retracted_at FROM edges"
            ).fetchone()
            self.assertEqual(tuple(row), ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", 1,
                                          "active", ""))
            registry.add_edge(conn, src, "managed_by", dst, "local:second", "2026-02-01T00:00:00Z",
                              "second sighting")
            row = conn.execute(
                "SELECT first_seen_at, last_seen_at, seen_count, status, evidence FROM edges"
            ).fetchone()
            self.assertEqual(tuple(row), ("2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z", 2,
                                          "active", "second sighting"))
            self.assertEqual(registry.retract_edge(conn, src, "managed_by", dst,
                                                    "2026-03-01T00:00:00Z"), 1)
            self.assertEqual(registry.retract_edge(conn, src, "managed_by", dst,
                                                   "2026-03-02T00:00:00Z"), 0)
            row = conn.execute(
                "SELECT status, retracted_at, seen_count, evidence FROM edges"
            ).fetchone()
            self.assertEqual(tuple(row), ("retracted", "2026-03-01T00:00:00Z", 2, "second sighting"))
            registry.add_edge(conn, src, "managed_by", dst, "local:third", "2026-04-01T00:00:00Z", "")
            row = conn.execute(
                "SELECT first_seen_at, last_seen_at, seen_count, status, retracted_at FROM edges"
            ).fetchone()
            self.assertEqual(tuple(row), ("2026-01-01T00:00:00Z", "2026-04-01T00:00:00Z", 3,
                                          "active", ""))

    def test_future_and_malformed_versions_are_not_rewritten(self):
        registry.init_db(self.db)
        for version in ("999", "-1", "invalid", "2.5", None):
            conn = sqlite3.connect(self.db)
            conn.execute("UPDATE meta SET v=? WHERE k='schema_version'", (version,))
            conn.commit()
            conn.close()
            with self.subTest(version=version), self.assertRaises(ValueError):
                registry.init_db(self.db)
            conn = sqlite3.connect(self.db)
            self.assertEqual(conn.execute("SELECT v FROM meta").fetchone()[0], version)
            conn.close()
        with patch("lele.core.constants.REGISTRY_SCHEMA_VERSION", 999):
            other = str(Path(self.temp.name) / "other.db")
            with registry.get_conn(other) as conn:
                self.assertEqual(conn.execute("SELECT v FROM meta").fetchone()[0],
                                 str(registry._SCHEMA_VERSION))


if __name__ == "__main__":
    unittest.main()
