"""The registry must not be written by a reader, and must never be left broken.

Three guarantees, each of which used to fail.

* A read command wrote to the database. Every open replayed the whole schema in
  a `BEGIN IMMEDIATE` transaction, so `lele list` created a file, took a
  write lock and grew it by a page. A read-only connection cannot be made to
  write, which is what `mode=ro` is for.
* A migration that could not finish bricked the database. The rebuild that adds
  foreign keys fails on orphaned rows, and because the whole migration is one
  transaction, every later command then failed too — with an IntegrityError
  naming nothing useful.
* A connection dropped without being closed leaked a file handle, which in a
  cron-driven run is unbounded growth rather than a visible failure.

No network. Temporary databases only.
"""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lele.cli.main import (COMMANDS, READ_ONLY_COMMANDS, _read_only, build_parser,
                               main)
from lele.core import db, registry


def digest(path):
    import hashlib
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


class RegistryCase(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = str(self.root / "registry.db")
        db.init_db(self.path)
        # Every direct open is registered for closing, so a test that exercises
        # a failure path cannot leave a handle behind and make an unrelated
        # test look like it leaked one.
        opened = []
        original = db.connect

        def tracked(*args, **kwargs):
            conn = original(*args, **kwargs)
            opened.append(conn)
            return conn

        self.opened = opened
        patcher = patch.object(db, "connect", side_effect=tracked)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: [conn.close() for conn in opened
                                 if getattr(conn, "execute", None) is not None])

    def connect(self):
        """A tracked connection, closed by the cleanup even if a test fails."""
        return db.connect(self.path)


class ReadDoesNotWrite(RegistryCase):

    def test_opening_an_up_to_date_database_changes_nothing(self):
        before = (digest(self.path), os.path.getsize(self.path))
        for _ in range(5):
            with registry.get_conn(self.path) as conn:
                registry.counts(conn)
        after = (digest(self.path), os.path.getsize(self.path))
        self.assertEqual(before, after,
                         "an up-to-date database must be opened without a single write")

    def test_read_only_commands_leave_the_file_alone(self):
        with registry.get_conn(self.path) as conn:
            registry.upsert_entity(conn, "bank", "Example Bank", key="local:example")
        before = (digest(self.path), os.path.getsize(self.path))
        for argv in (["list", "--limit", "5"], ["stats"], ["countries"], ["runs"],
                     ["show", "1"], ["kinds"], ["sources"]):
            with self.subTest(command=argv[0]):
                self.assertEqual(main(["--db", self.path, "--json", *argv]), 0)
        after = (digest(self.path), os.path.getsize(self.path))
        self.assertEqual(before, after)

    def test_a_read_only_connection_refuses_a_write(self):
        with registry.get_read_conn(self.path) as conn:
            self.assertTrue(conn.lele_readonly)
            with self.assertRaises(sqlite3.OperationalError):
                registry.upsert_entity(conn, "bank", "Should Not Exist", key="local:nope")
        with registry.get_conn(self.path) as conn:
            self.assertIsNone(registry.entity_id_by_key(conn, "local:nope"))

    def test_a_read_only_command_on_a_read_only_file_succeeds(self):
        """The point of the guarantee: a read works where a write could not."""
        with registry.get_conn(self.path) as conn:
            registry.upsert_entity(conn, "bank", "Read Only Bank", key="local:ro")
        os.chmod(self.path, 0o444)
        self.addCleanup(os.chmod, self.path, 0o644)
        self.assertEqual(main(["--db", self.path, "--json", "list", "--limit", "5"]), 0)
        self.assertEqual(main(["--db", self.path, "--json", "stats"]), 0)

    def test_a_write_command_on_a_read_only_file_fails_clearly(self):
        with registry.get_conn(self.path) as conn:
            registry.upsert_entity(conn, "bank", "Read Only Bank", key="local:ro")
        os.chmod(self.path, 0o444)
        self.addCleanup(os.chmod, self.path, 0o644)
        self.assertNotEqual(main(["--db", self.path, "--json", "import", "x.json"]), 0)

    def test_a_missing_registry_is_reported_not_created(self):
        absent = str(self.root / "absent.db")
        self.assertEqual(main(["--db", absent, "--json", "list"]), 1,
                         "the exit code for a missing registry is unchanged from before")
        self.assertFalse(os.path.exists(absent), "a read must not create the registry")

    def test_read_only_classification_covers_every_command(self):
        """Every command is classified, so none can silently write while reading.

        The matrix is exhaustive on purpose: a command added later that is
        neither listed nor in the write set gets a read-write handle by default,
        which is safe but slow, while one wrongly listed here would open
        read-only and fail at the first write with a message about the database
        rather than about the bug.
        """
        parser = build_parser()
        # command -> (minimal valid argv, expected read-only)
        matrix = {
            "list": ([], True), "show": (["1"], True), "stats": ([], True),
            "countries": ([], True), "runs": ([], True), "tree": (["1"], True),
            "analyze": (["1"], True), "finmap": (["1"], True), "events": (["1", "p.json"], True),
            "project": (["1", "p.json"], True), "relationships": (["1"], True),
            "doctor": ([], True), "sources": ([], True), "kinds": ([], True),
            "export": (["--format", "json", "--output", "out.json"], True),
            "init": ([], False), "import": (["f.json"], False), "import-history": (["1", "p.json"], False),
            "fetch": (["gleif"], False),
            "sentiment": (["1", "--text", "x", "--source", "s"], False),
            "backup": (["b.db"], False), "edges": (["--kind", "fund", "--source", "gleif"], False),
            "moves": (["1"], False), "scan": (["1"], False),
            "fetch-sentiment": ([], False), "fetch-stablecoins": ([], False),
            "fetch-market-activity": (["1"], False), "fetch-form4": (["1"], False),
            "fetch-history": (["1", "BTCUSDT"], False),
            "fetch-13f": (["1"], False),
            "fetch-awards": (["1", "--recipient-uei", "G4KDGE4JFFK7"], False),
            "fetch-prices": (["1", "binance", "BTCUSDT", "--output", "o.json"], False),
            "fetch-evidence": (["1", "binance-futures", "BTCUSDT", "--output", "o.json"], False),
            "fetch-news": (["1", "bitcoin", "--output", "o.json"], False),
            "fetch-news-feed": (["1", "bitcoin", "--output", "o.json"], False),
            "resolve": (["list"], True),
            "links": (["list"], True), "flows": (["list"], True),
            "observations": (["list"], True), "sanctions": (["candidates"], True),
            "indicators": (["list"], True), "causes": (["profile", "1"], True),
            "worldstate": (["1", "p.json", "--evidence", "e.json"], True),
            "episodes": (["1", "p.json"], True),
            "volatility-analyze": (["1", "p.json"], True),
            "fetch-comtrade": (["1"], False), "fetch-census-trade": (["1"], False),
            "fetch-lobbying": (["1", "--client-id", "58116"], False), "fetch-opensky": (["1", "states"], False),
            "fetch-formadv": ([], False), "fetch-formadv-individual": ([], False),
            "fetch-formd": (["1"], False), "fetch-nport": (["1"], False),
            "fetch-eia": (["1", "PET.RWTC.D", "--api-key", "k"], False),
            "fetch-bls": (["1", "LNS14000000"], False),
            "fetch-cot": (["1", "cftc", "BITCOIN-CME", "--output", "o.json"], False),
            "fetch-short": (["1", "finra", "AAPL", "--output", "o.json"], False),
            "fetch-material": (["1"], False),
            "fetch-treasury": ([], False), "fetch-political": ([], False),
            "compare": (["1", "p.json"], True),
            "explain": (["1", "--from", "2026-01-01T00:00:00+00:00",
                         "--to", "2026-01-08T00:00:00+00:00"], True),
            "capital": (["1", "--from", "2026-01-01T00:00:00+00:00",
                         "--to", "2026-01-08T00:00:00+00:00"], True),
            "store-evidence": (["1", "binance-futures", "BTCUSDT"], False),
            "prospective": (["score", "--ledger", "l.jsonl"], True),
        }
        for command, (extra, expected) in matrix.items():
            with self.subTest(command=command):
                args = parser.parse_args([command, *extra])
                self.assertEqual(_read_only(command, args), expected)
        actions = [
            (("links", "set", ["1", "https://x.test"]), False),
            (("links", "list", []), True),
            (("links", "remove", ["1", "https://x.test"]), False),
            (("flows", "import", ["f.json"]), False),
            (("flows", "summary", ["1"]), True),
            (("observations", "import", ["f.json"]), False),
            (("observations", "list", []), True),
            (("sanctions", "fetch", ["ofac-sdn"]), False),
            (("sanctions", "link", ["1", "2"]), False),
            (("sanctions", "candidates", []), True),
            (("resolve", "merge", ["1", "2"]), False),
            (("resolve", "candidates", []), True),
            (("indicators", "compute", ["--entity-id", "1",
                                        "--indicator", "insider_net_buy_v1"]), False),
            (("indicators", "spec", ["--indicator", "insider_net_buy_v1"]), True),
            (("causes", "attribute", ["1"]), False),
            (("causes", "context", ["1"]), True),
            (("rag", "build-graph", []), True),
            (("rag", "store-events", ["events.json"]), False),
            (("rag", "indicator", ["--instrument-key", "binance:BTCUSDT"]), True),
        ]
        for (command, action, extra), expected in actions:
            with self.subTest(command=command, action=action):
                args = parser.parse_args([command, action, *extra])
                self.assertEqual(_read_only(command, args), expected)
        covered = set(matrix) | {entry[0][0] for entry in actions}
        # These three never open the registry at all, so there is nothing to
        # classify. `menu` recurses into `main`, so every command it runs is
        # classified on its own behalf.
        no_registry = {"version", "menu", "help"}
        self.assertEqual(READ_ONLY_COMMANDS - covered, set(),
                         "every read-only command must be reachable from this test")
        self.assertEqual(set(COMMANDS) - covered - no_registry, set(),
                         "every command that opens the registry must appear here, so a new "
                         "one cannot be added without deciding whether it reads or writes")
        for command in no_registry & set(COMMANDS):
            self.assertNotIn(command, READ_ONLY_COMMANDS)


class InMemoryTests(unittest.TestCase):

    def test_in_memory_never_creates_a_file(self):
        """`:memory:` is a name, not a path.

        Running it through `abspath` produced a real 370 KB file called
        `:memory:` in the working directory, so every assertion of the form "no
        database was created" was quietly false wherever a test used it.
        """
        before = set(os.listdir("."))
        with registry.get_conn(":memory:") as conn:
            registry.upsert_entity(conn, "bank", "In Memory Bank", key="local:mem")
            self.assertEqual(registry.stats(conn)["entities"], 1)
        self.assertEqual(set(os.listdir(".")), before)
        self.assertFalse(os.path.exists(":memory:"))

    def test_an_in_memory_registry_has_no_separate_read_handle(self):
        with self.assertRaisesRegex(ValueError, "in-memory"):
            db.read_connect(":memory:")
        self.assertFalse(os.path.exists(":memory:"))


class MigrationSafety(RegistryCase):

    def test_initialize_reports_whether_it_wrote(self):
        conn = self.connect()
        self.assertFalse(db.initialize(conn), "an up-to-date database needs no migration")
        with db.get_conn(self.path) as writer:
            registry.upsert_entity(writer, "bank", "Written", key="local:w")
        self.assertFalse(db.initialize(conn),
                         "adding a row does not make the schema stale")

    def test_a_claimed_version_with_a_missing_table_is_refused(self):
        with db.get_conn(self.path) as conn:
            conn.execute("DROP TABLE observations")
        with self.assertRaises(db.RegistryMigrationError) as caught:
            db.initialize(self.connect())
        self.assertIn("observations", str(caught.exception))
        self.assertIn("backup", str(caught.exception),
                      "the operator has to be told how to recover")

    def test_a_newer_version_is_refused_rather_than_downgraded(self):
        with db.get_conn(self.path) as conn:
            conn.execute("UPDATE meta SET v='999' WHERE k='schema_version'")
        with self.assertRaises(db.RegistryMigrationError):
            db.initialize(self.connect())

    def test_health_check_reports_instead_of_raising(self):
        with db.get_conn(self.path) as conn:
            conn.execute("UPDATE meta SET v='nonsense' WHERE k='schema_version'")
        report = db.health_check(self.path)
        self.assertEqual(report["status"], "issues")
        self.assertIn("error", report["registry"])

    def test_health_check_notices_a_missing_object(self):
        with db.get_conn(self.path) as conn:
            conn.execute("DROP INDEX idx_move_events_tier")
        report = db.health_check(self.path)
        self.assertEqual(report["status"], "incomplete")
        self.assertIn("idx_move_events_tier", report["registry"]["missing_objects"])

    def test_doctor_does_not_create_or_migrate(self):
        absent = str(self.root / "absent.db")
        report = db.health_check(absent)
        self.assertFalse(report["registry"]["present"])
        self.assertFalse(os.path.exists(absent))


class NoLeakedConnections(RegistryCase):

    def test_every_session_closes_its_connection(self):
        import gc
        import warnings

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            for _ in range(5):
                with registry.get_conn(self.path) as conn:
                    registry.counts(conn)
                with registry.get_read_conn(self.path) as conn:
                    registry.counts(conn)
            gc.collect()
        self.assertEqual([str(item.message) for item in caught
                          if "unclosed database" in str(item.message)], [])

    def test_a_failing_session_still_closes(self):
        import gc
        import warnings

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            for _ in range(3):
                with self.assertRaises(RuntimeError):
                    with registry.get_conn(self.path) as conn:
                        conn.execute("SELECT 1")
                        raise RuntimeError("after the write")
            gc.collect()
        self.assertEqual([str(item.message) for item in caught
                          if "unclosed database" in str(item.message)], [])
        with db.get_conn(self.path) as conn:
            conn.execute("SELECT count(*) FROM entities").fetchone()

    def test_a_read_only_open_that_fails_does_not_leak_the_first_handle(self):
        """The fallback must close the handle it abandoned before retrying.

        The read-only open can succeed and then fail on its first query, which
        used to reassign the variable and lose the connection without closing it.
        """
        import gc
        import warnings

        original = db.sqlite3.connect
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            conn = original(*args, **kwargs)
            if calls["n"] % 2 == 1:
                conn.close()
                raise sqlite3.OperationalError("cannot open read-only")
            return conn

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with patch.object(db.sqlite3, "connect", side_effect=flaky):
                for _ in range(3):
                    conn = db.read_connect(self.path)
                    self.assertFalse(conn.lele_readonly)
                    conn.close()
            gc.collect()
        self.assertGreater(calls["n"], 1, "the fallback path must actually be exercised")
        self.assertEqual([str(item.message) for item in caught
                          if "unclosed database" in str(item.message)], [])


if __name__ == "__main__":
    unittest.main()
