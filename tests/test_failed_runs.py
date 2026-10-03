"""Offline tests for recording a failed ingest run.

The property under test is the one that makes the row worth having and the one that
could have broken the registry: **a failed fetch stores nothing, and the row saying
it failed survives the rollback that discards what it stored.** Those two halves
pull in opposite directions. The row has to be written outside the transaction that
is about to roll back, which is a deliberate exception to "`get_conn` owns every
commit", and it has to be written without the exception's message, which is the
§3G1 rule about never echoing one.

The failure modes pinned here are a row that is rolled back with everything else (the
monitor stays blind and nothing says so), a row that survives but carries a secret,
and a row that reports itself written when it was not.
"""
import ast
import io
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch

from lele.analysis import provider_health
from lele.cli.main import main
from lele.core import db, registry
from lele.fetchers.http import SourceError

START = "2026-09-20T12:00:00+00:00"
SECRET = "token=SECRET-do-not-store"


class FileRegistry(unittest.TestCase):
    """A file-backed registry, because a rollback is what makes the row a test."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = directory.name
        self.path = os.path.join(directory.name, "registry.db")
        db.init_db(self.path)

    def runs(self):
        with registry.get_read_conn(self.path) as conn:
            return registry.list_ingest_runs(conn, limit=100)


def failing_session(path, **fields):
    """Register a failure recorder, store a row, then fail -- the real sequence."""
    with registry.get_conn(path) as conn:
        registry.set_failure_recorder(
            conn, registry.failed_run(source="fdic", started_at=START, **fields))
        registry.upsert_entity(conn, "bank", "Should Not Exist", key="local:rolled-back")
        raise RuntimeError("the fetch failed after storing a row")


class Recording(FileRegistry):

    def test_a_failed_fetch_stores_nothing_and_the_failure_row_survives(self):
        """The two halves, in one test, because either alone would pass a weaker one.

        The `raise` is load-bearing: it is what makes `get_conn` roll back, and the
        row recorded a moment earlier has to outlive that rollback.
        """
        with self.assertRaises(RuntimeError):
            failing_session(self.path)
        with registry.get_read_conn(self.path) as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM entities").fetchone()[0], 0)
        with registry.get_read_conn(self.path) as conn:
            runs = registry.list_ingest_runs(conn, limit=10)
        self.assertEqual(len(runs), 1, "the row must outlive the rollback that discarded the fetch")
        self.assertEqual(runs[0]["status"], "failed")
        self.assertEqual(runs[0]["source"], "fdic")
        self.assertEqual(runs[0]["started_at"], START)
        self.assertEqual(runs[0]["fetched"], 0)

    def test_the_failure_row_is_not_in_the_rolled_back_transaction(self):
        """Proved by looking from the other side: the row is there after the rollback."""
        with self.assertRaises(RuntimeError):
            failing_session(self.path)
        self.assertEqual(self.runs()[0]["status"], "failed")
        with registry.get_read_conn(self.path) as conn:
            self.assertIsNone(registry.entity_id_by_key(conn, "local:rolled-back"))

    def test_the_recorder_is_cleared_after_it_is_used_so_one_failure_records_once(self):
        with self.assertRaises(RuntimeError):
            failing_session(self.path)
        self.assertEqual(len(self.runs()), 1)

    def test_a_recorder_that_raises_does_not_replace_the_failure(self):
        """A failure to record a failure must not become the failure a caller sees."""
        def broken(conn):
            raise sqlite3.OperationalError("the recorder itself failed")
        with self.assertRaisesRegex(RuntimeError, "the original failure"):
            with registry.get_conn(self.path) as conn:
                registry.set_failure_recorder(conn, broken)
                raise RuntimeError("the original failure")

    def test_no_recorder_registered_means_no_row_after_a_failure(self):
        with self.assertRaises(RuntimeError):
            with registry.get_conn(self.path):
                raise RuntimeError("nothing was registered")
        self.assertEqual(self.runs(), [])

    def test_a_second_failed_run_is_appended_rather_than_replacing_the_first(self):
        for query in ("first", "second"):
            with registry.get_conn(self.path) as conn:
                registry.record_failed_run(
                    conn, source="fdic", query=query, started_at=START,
                    error=sqlite3.OperationalError("locked"))
        runs = self.runs()
        self.assertEqual([row["query"] for row in runs], ["second", "first"])
        self.assertTrue(all(row["status"] == "failed" for row in runs))

    def test_a_successful_run_records_completed_and_no_extra_row(self):
        with registry.get_conn(self.path) as conn:
            registry.record_ingest_run(conn, "fdic", START, START, fetched=3, stored=3)
        runs = self.runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "completed")

    def test_the_request_identity_is_carried_so_a_failure_is_attributable(self):
        with registry.get_conn(self.path) as conn:
            outcome = registry.record_failed_run(
                conn, source="fdic", query="x", indicator="y", category="z",
                started_at=START, request_sha256="abc123", error=ValueError(SECRET))
        self.assertTrue(outcome["recorded"])
        row = self.runs()[0]
        self.assertEqual(row["request_sha256"], "abc123")
        self.assertEqual((row["query"], row["indicator"], row["category"]), ("x", "y", "z"))


class TheMessageIsNeverStored(FileRegistry):

    def test_a_secret_in_the_exception_message_reaches_neither_field(self):
        for error in (SourceError(SECRET), ValueError(SECRET), sqlite3.OperationalError(SECRET),
                      OSError(SECRET)):
            with registry.get_conn(self.path) as conn:
                outcome = registry.record_failed_run(
                    conn, source="fdic", started_at=START, error=error)
            self.assertTrue(outcome["recorded"])
        with registry.get_read_conn(self.path) as conn:
            rows = registry.list_ingest_runs(conn, limit=100)
        blob = json.dumps(rows)
        self.assertNotIn("SECRET", blob, "an exception message can carry a credential")
        self.assertTrue(all(row["status"] == "failed" for row in rows))

    def test_the_reason_comes_from_a_closed_vocabulary_and_the_class_name_is_kept(self):
        cases = {
            SourceError(SECRET): "source_request",
            sqlite3.IntegrityError(SECRET): "database",
            OSError(SECRET): "file",
            ValueError(SECRET): "data",
            KeyboardInterrupt(): "cancelled",
            RuntimeError(SECRET): "unexpected",
        }
        for error, expected in cases.items():
            with self.subTest(error=type(error).__name__):
                reason, name = registry.classify_failure(error)
                self.assertEqual(reason, expected)
                self.assertEqual(name, type(error).__name__)
                self.assertNotIn(SECRET, reason + name)

    def test_every_failure_reason_is_named_in_the_vocabulary(self):
        """A reason a reader cannot look up is a reason that does not get looked up."""
        for reason in registry.FAILURE_REASONS.values():
            self.assertTrue(reason.strip())
            self.assertTrue(reason.startswith(("the ", "a ")))


class Adoption(unittest.TestCase):
    """Checked over the source, because a list of adopting commands is a list to forget.

    Every module that records a completed run must also register a failure recorder.
    That is the whole coverage claim in `provider_health`, and asserting it with `ast`
    means a new fetcher cannot join the family without joining both halves.
    """

    def _modules(self):
        root = Path(__file__).resolve().parent.parent / "lele"
        for path in sorted(root.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            yield path, ast.parse(text), text

    def test_every_module_recording_a_completed_run_also_records_a_failure(self):
        checked = 0
        for path, tree, text in self._modules():
            if "record_ingest_run(" not in text:
                continue
            checked += 1
            with self.subTest(module=path.name):
                self.assertIn("record_failures(", text,
                              f"{path.name} records a completed run but never registers a "
                              "failure recorder, so a failure of its fetcher leaves no row")
                self.assertIn("clear_failure_recorder(conn)", text,
                              f"{path.name} registers a failure recorder and never clears it, so "
                              "a later failure in the same session would be recorded against "
                              "this fetcher")
        self.assertGreaterEqual(checked, 16, "the family is smaller than expected")

    def test_the_crypto_context_and_evidence_commands_are_now_recorded(self):
        """The five commands that recorded nothing now record both outcomes.

        `crypto_context.py` gained its run rows in this round and `store-evidence`
        its own, so neither belongs in the list of commands with nothing to record --
        which is why the assertion is that the modules *do* record, not that they do not.
        """
        root = Path(__file__).resolve().parent.parent / "lele"
        context = (root / "fetchers" / "crypto_context.py").read_text(encoding="utf-8")
        for source in ('SENTIMENT_SOURCE = "crypto-fear-greed"',
                       'STABLECOIN_SOURCE = "crypto-stablecoin-supply"',
                       'ACTIVITY_SOURCE = "crypto-market-activity"'):
            self.assertIn(source, context,
                          "a run-history label is defined once and used by both the failure "
                          "registration and the success row, so the two cannot drift apart")
        self.assertEqual(context.count("registry.record_failures("), 3)
        cli = (root / "cli" / "main.py").read_text(encoding="utf-8")
        self.assertIn('source="store-evidence"', cli)
        evidence = (root / "fetchers" / "evidence.py").read_text(encoding="utf-8")
        self.assertNotIn("record_ingest_run(", evidence,
                         "evidence.py only exports; it must not start recording runs")

class ClearingOnSuccess(FileRegistry):
    """The registry this test writes to, with somewhere to put an export file."""

    def test_a_v19_registry_keeps_its_fingerprint_and_gains_the_response_column(self):
        """A rename that recomputes looks identical to a rename that invents.

        So the value is written on the old column, the migration runs, and the carried
        value is read back -- with the rename recorded in `meta.migration_notes`, which
        is what a reader has instead of the git history.
        """
        with registry.get_conn(self.path) as conn:
            conn.execute("ALTER TABLE ingest_runs DROP COLUMN payload_sha256")
            conn.execute("ALTER TABLE ingest_runs RENAME COLUMN retrieval_sha256"
                         " TO records_sha256")
            conn.execute("DELETE FROM ingest_runs")
            conn.execute(
                "INSERT INTO ingest_runs(source, query, started_at, finished_at, status,"
                " records_sha256) VALUES('fdic','q','2026-09-20T00:00:00+00:00',"
                "'2026-09-20T00:01:00+00:00','completed','c0ffee')")
            conn.execute("UPDATE meta SET v='19' WHERE k='schema_version'")
            conn.execute("DELETE FROM meta WHERE k='migration_notes'")
        with registry.get_conn(self.path) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(ingest_runs)")}
            self.assertNotIn("records_sha256", columns)
            self.assertIn("retrieval_sha256", columns)
            self.assertIn("payload_sha256", columns)
            row = registry.list_ingest_runs(conn, limit=1)[0]
            self.assertEqual(row["retrieval_sha256"], "c0ffee",
                             "the value must be carried across, not recomputed")
            self.assertEqual(row["payload_sha256"], "")
            notes = str([item[0] for item in
                         conn.execute("SELECT v FROM meta WHERE k='migration_notes'")])
            self.assertIn("retrieval_sha256", notes,
                          "a silent rename must leave a note saying what it did")

    def test_no_call_site_calls_the_response_column_a_record_hash(self):
        """The rename is only honest while no name in the tree claims otherwise."""
        root = Path(__file__).resolve().parent.parent / "lele"
        # `db.py` performs the rename, `registry.py` reads whichever column a
        # registry actually has, and `provider_health.py` documents what each one
        # held; those three may name the old column in code. Every other module may
        # only mention it in a comment explaining the history.
        offenders = []
        for path in sorted(root.rglob("*.py")):
            if path.name in ("db.py", "registry.py", "provider_health.py"):
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if "records_sha256" not in line:
                    continue
                code = line.split("#", 1)[0]
                if "records_sha256" in code:
                    offenders.append(f"{path.relative_to(root)}:{number}")
        self.assertEqual(offenders, [], f"these still name the old column in code: {offenders}")

    def test_a_fetcher_that_records_a_success_leaves_no_failure_recorded(self):
        """The clear on success is the half that is easy to leave out.

        A fetcher that registered a recorder and returned normally without clearing it
        would attribute a later failure in the same session to this fetcher, so the
        session runs a fetch and then fails, and only the fetch's own run may exist.
        `import-history` is used because it needs no provider and no fixture beyond a
        file, and it writes bars, so it exercises the same rollback as any other.
        """
        from lele.analysis import price_import
        rows = [{"open_time": f"2026-01-{day:02d}T00:00:00+00:00",
                 "open": "100", "high": "110", "low": "99", "close": "105",
                 "volume": "1"} for day in range(1, 6)]
        path = os.path.join(self.root, "export.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"instrument": {"symbol": "EQUITY-A", "venue": "TESTEX",
                                      "currency": "USD", "asset_class": "equity",
                                      "adjustment": "adjusted",
                                      "rights_basis": "synthetic fixture"},
                       "source": "fixture", "source_url": "local:synthetic-fixture",
                       "retrieved_at": "2026-01-31T00:00:00+00:00",
                       "bars": rows}, handle)
        with registry.get_conn(self.path) as conn:
            entity_id = registry.upsert_entity(conn, "instrument", "Equity A",
                                               key="local:equity-a")
            price_import.import_price_history(conn, entity_id, path, "1d",
                                              now=datetime(2026, 2, 1, tzinfo=UTC))
        # A later failure in the same session must not be recorded against the import.
        with self.assertRaises(RuntimeError):
            with registry.get_conn(self.path) as conn:
                raise RuntimeError("something else failed later")
        runs = self.runs()
        self.assertEqual([row["status"] for row in runs], ["completed"],
                         "a successful import recorded one completed run and no failure")
        self.assertEqual(runs[0]["source"], "import-history")


class PayloadFingerprints(unittest.TestCase):
    """What a run row can now say about the document it read."""

    def test_a_client_fingerprint_is_reached_through_the_registry(self):
        class Client:
            def payload_fingerprint(self):
                return "a" * 64
        with sqlite3.connect(":memory:", factory=db.Connection) as conn:
            conn.row_factory = sqlite3.Row
            registry._initialize(conn)
            self.assertTrue(registry.set_payload_fingerprint(conn, Client()))
            registry.record_ingest_run(conn, "x", START, START)
            row = registry.list_ingest_runs(conn, limit=1)[0]
        self.assertEqual(row["payload_sha256"], "a" * 64)

    def test_a_client_that_cannot_fingerprint_records_none_rather_than_a_junk_value(self):
        """A column that can hold a non-string cannot be compared later.

        The way that arrives in practice is a client replaced by a test double, so
        the guard belongs in the registry rather than in twenty-two callers.
        """
        class NoFingerprint:
            pass
        class Mock:
            def payload_fingerprint(self):
                return object()
        for client in (NoFingerprint(), Mock(), None, 42):
            with self.subTest(client=type(client).__name__):
                with sqlite3.connect(":memory:", factory=db.Connection) as conn:
                    conn.row_factory = sqlite3.Row
                    registry._initialize(conn)
                    self.assertFalse(registry.set_payload_fingerprint(conn, client))
                    registry.record_ingest_run(conn, "x", START, START)
                    self.assertEqual(registry.list_ingest_runs(conn, limit=1)[0]["payload_sha256"],
                                     "")

    def test_an_explicit_empty_fingerprint_differs_from_not_saying_which(self):
        with sqlite3.connect(":memory:", factory=db.Connection) as conn:
            conn.row_factory = sqlite3.Row
            registry._initialize(conn)
            registry.set_payload_fingerprint(conn, "b" * 64)
            registry.record_ingest_run(conn, "inherited", START, START)
            registry.record_ingest_run(conn, "explicit-none", START, START, payload_sha256="")
            rows = {row["source"]: row["payload_sha256"]
                    for row in registry.list_ingest_runs(conn, limit=5)}
        self.assertEqual(rows["inherited"], "b" * 64)
        self.assertEqual(rows["explicit-none"], "",
                         "a run that deliberately records none is different from one that "
                         "inherited whatever the session read")

    def test_every_fetcher_that_writes_a_run_row_sets_the_fingerprint(self):
        """Checked over the source, so a new fetcher cannot join without it.

        Every fetcher in this project reads through one client, and the run row is
        written from the same session, so the fingerprint is one statement beside
        the row rather than a judgement twenty-two times over. `store-evidence` is
        exempt and named: the client that read its document belongs to
        `evidence.fetch_evidence`, so there is nothing in that function's scope to
        ask, and the row records none rather than a guess.
        """
        root = Path(__file__).resolve().parent.parent / "lele"
        missing = []
        for path in sorted(root.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            # `main.py` writes the one run row whose fetcher owns its own client.
            if "record_ingest_run(" not in text or path.name in ("registry.py", "main.py"):
                continue
            if "set_payload_fingerprint(" not in text:
                missing.append(str(path.relative_to(root)))
        self.assertEqual(missing, [], f"these write a run row without fingerprinting: {missing}")


class InMemory(FileRegistry):

    def test_an_in_memory_registry_records_the_failure_too(self):
        """The earlier design needed a second connection and could not do this.

        Writing on the same connection after the rollback removes the special case
        entirely, which is worth a test: a registry held only in memory is exactly
        what a test or a throwaway run uses, and a failure recorded everywhere else
        but not there is a blind spot in the wrong place. The recorder's own view is
        used to check the ordering, because an in-memory database exists only for as
        long as its connection.
        """
        seen = []

        def recorder(conn):
            seen.append((conn.execute("SELECT count(*) FROM entities").fetchone()[0],
                         registry.record_failed_run(
                             conn, error=RuntimeError("unrecorded detail"),
                             source="fdic", started_at=START)["recorded"]))

        with self.assertRaises(RuntimeError):
            with registry.get_conn(":memory:") as conn:
                registry.set_failure_recorder(conn, recorder)
                registry.upsert_entity(conn, "bank", "Gone", key="local:in-memory")
                raise RuntimeError("failed")
        self.assertEqual(seen, [(0, True)],
                         "the recorder ran after the rollback and wrote its row")


class ThroughTheFetcher(FileRegistry):

    def setUp(self):
        super().setUp()
        self.root = self.path

    def test_a_failing_fetch_through_the_command_leaves_a_row_and_stores_nothing(self):
        with registry.get_conn(self.path) as conn:
            registry.upsert_entity(conn, "bank", "Example Bank", key="local:example")
        with patch("lele.fetchers.sources.HTTPClient.get_json",
                   side_effect=SourceError(SECRET)):
            stream = io.StringIO()
            with redirect_stdout(stream):
                status = main(["--db", self.path, "--json", "fetch", "fdic"])
        self.assertEqual(status, 1)
        self.assertNotIn("SECRET", stream.getvalue())
        with registry.get_read_conn(self.path) as conn:
            runs = registry.list_ingest_runs(conn, limit=10)
            entities = conn.execute("SELECT count(*) FROM entities").fetchone()[0]
        self.assertEqual(entities, 1, "the entity stored before the fetch is still there")
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "failed")
        self.assertEqual(runs[0]["source"], "fdic")
        self.assertNotIn("SECRET", json.dumps(runs))

    def test_an_argument_refused_before_any_request_records_no_run(self):
        """A run row exists for a fetch that reached the request stage and for
        nothing else: a refused argument never became a run."""
        for argv in (["fetch", "fdic", "--limit", "0"], ["fetch", "nonesuch"],
                     ["fetch", "worldbank", "--query", "acme"]):
            with self.subTest(argv=argv):
                with redirect_stdout(io.StringIO()):
                    self.assertNotEqual(main(["--db", self.path, "--json", *argv]), 0)
        with registry.get_read_conn(self.path) as conn:
            self.assertEqual(registry.list_ingest_runs(conn, limit=10), [])

    def test_a_provider_report_counts_the_recorded_failure(self):
        from lele.analysis import provider_health
        with registry.get_conn(self.path) as conn:
            registry.record_failed_run(
                conn, source="fdic", started_at="2026-10-03T00:00:00+00:00",
                error=SourceError("connection refused"))
        with registry.get_read_conn(self.path) as conn:
            report = provider_health.report(conn)
        series = report["series"][0]
        self.assertEqual(series["failed_runs"], 1)
        self.assertIn("recorded_failure", report["flags"])
        self.assertIn("source_request", " ".join(series["failure_reasons"]))
        self.assertIn("also records a failed one", report["failure_recording"])
        self.assertIn("no rows at all", report["failure_recording_note"])
        self.assertIn("fetch-cot", report["export_only_commands"])

    def test_a_provider_report_still_blames_no_source_for_a_command_that_records_nothing(self):
        with registry.get_read_conn(self.path) as conn:
            report = provider_health.report(conn)
        self.assertEqual(report["window"]["series"], 0)
        self.assertIn("no rows at all", report["failure_recording_note"])


if __name__ == "__main__":
    unittest.main()
