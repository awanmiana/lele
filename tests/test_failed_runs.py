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

    def test_the_no_run_history_list_names_real_fetch_commands_and_no_run_history_writes(self):
        from lele.cli.main import COMMANDS
        self.assertEqual(set(provider_health.NO_RUN_HISTORY) - set(COMMANDS), set())
        for command in provider_health.NO_RUN_HISTORY:
            self.assertTrue(command.startswith(("fetch", "store")), command)
        for module in ("crypto_context.py", "evidence.py"):
            text = (Path(__file__).resolve().parent.parent / "lele" / "fetchers" / module).read_text(
                encoding="utf-8")
            self.assertNotIn("record_ingest_run(", text,
                             f"{module} now records runs, so it belongs in the recorded family "
                             "and not in NO_RUN_HISTORY")

class ClearingOnSuccess(FileRegistry):
    """The registry this test writes to, with somewhere to put an export file."""

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
        self.assertIn("invisible to this report in both directions",
                      report["failure_recording_note"])
        self.assertIn("fetch-sentiment", report["no_run_history"])

    def test_a_provider_report_still_blames_no_source_for_a_command_that_records_nothing(self):
        with registry.get_read_conn(self.path) as conn:
            report = provider_health.report(conn)
        self.assertEqual(report["window"]["series"], 0)
        self.assertIn("record no run at all", report["failure_recording_note"])


if __name__ == "__main__":
    unittest.main()
