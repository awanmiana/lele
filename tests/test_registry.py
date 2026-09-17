import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from finworld.core import importer, registry


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
        schema = registry._SCHEMA.replace(" NOT NULL REFERENCES entities(id) ON DELETE CASCADE", "")
        for field in ("source_url", "observed_at", "evidence"):
            schema = schema.replace(f"  {field} TEXT NOT NULL DEFAULT '',\n", "")
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
            self.assertEqual(conn.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()[0], "3")
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

    def test_orphan_migration_is_atomic(self):
        self.legacy_db(orphan=True)
        with self.assertRaises(sqlite3.IntegrityError):
            registry.init_db(self.db)
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT v FROM meta").fetchone()[0], "2")
        self.assertEqual(len(conn.execute("PRAGMA table_info(edges)").fetchall()), 3)
        self.assertEqual(conn.execute("SELECT dst_id FROM edges").fetchone()[0], 999)
        self.assertEqual(conn.execute("SELECT name FROM sqlite_master WHERE name LIKE '_registry_migrate_%'").fetchall(), [])

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
        with patch("finworld.core.constants.REGISTRY_SCHEMA_VERSION", 999):
            other = str(Path(self.temp.name) / "other.db")
            with registry.get_conn(other) as conn:
                self.assertEqual(conn.execute("SELECT v FROM meta").fetchone()[0], "3")


if __name__ == "__main__":
    unittest.main()
