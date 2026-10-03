"""Offline tests for provider health: what the recorded runs can say, and what they cannot.

The failure modes these pin are the ones this project keeps finding in its own
reports. A monitor that calls a source healthy because it is quiet, when a failed
run is never recorded at all. A comparison made across two different requests and
reported as a change in the provider. A threshold presented as a verdict, with the
numbers it fired on withheld. A bound that hides a series and reports the source as
having no runs.
"""
import io
import json
import sqlite3
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from lele.analysis import provider_health
from lele.cli.main import main
from lele.core import db, registry

BASE = datetime(2026, 9, 20, tzinfo=UTC)
NOW = datetime(2026, 9, 21, tzinfo=UTC)


def iso(moment):
    return moment.isoformat()


def run(conn, *, source="opensky", query="all states", fetched=100, stored=10, skipped=0,
        missing=0, pages=1, truncated=False, request="req-1", records="rec-hash",
        warnings=None, started=None, indicator="", country="", category=""):
    """One recorded completed run, as a fetcher writes it.

    A record hash is passed by default so a series built from these runs has no
    flags at all, which is what lets a test assert that a wobbling count is *not*
    flagged. `no_record_hash` is raised by passing `records=""`, and that is how
    17 of the 25 real recording sites behave.
    """
    moment = started if started is not None else NOW - timedelta(hours=1)
    return registry.record_ingest_run(
        conn, source=source, started_at=iso(moment), finished_at=iso(moment),
        query=query, country=country, indicator=indicator, category=category,
        fetched=fetched, stored=stored, skipped=skipped, missing=missing, pages=pages,
        truncated=truncated, request_sha256=request,
        records_sha256=records, warnings=warnings or [])


class RegistryCase(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        registry._initialize(self.conn)
        self.addCleanup(self.conn.close)

    def report(self, **kwargs):
        kwargs.setdefault("now", NOW)
        return provider_health.report(self.conn, **kwargs)


class WhatIsRecorded(RegistryCase):

    def test_an_empty_registry_reports_no_runs_rather_than_no_problems(self):
        report = self.report()
        self.assertEqual(report["status"], "no_recorded_runs")
        self.assertEqual(report["series"], [])
        self.assertEqual(report["flagged_series"], 0)

    def test_runs_group_into_one_series_per_source_and_query(self):
        run(self.conn, fetched=100)
        run(self.conn, fetched=120, started=NOW - timedelta(hours=2))
        run(self.conn, query="positions", fetched=5)
        report = self.report()
        self.assertEqual(report["window"]["series"], 2)
        series = {row["query"]: row for row in report["series"]}
        self.assertEqual(series["all states"]["runs"], 2)
        self.assertEqual(series["positions"]["runs"], 1)

    def test_the_oldest_and_newest_counts_are_published_with_the_ones_it_fired_on(self):
        """A reader must be able to apply their own threshold to the raw numbers."""
        run(self.conn, fetched=100, stored=10, started=NOW - timedelta(hours=3))
        run(self.conn, fetched=90, stored=9, started=NOW - timedelta(hours=2))
        run(self.conn, fetched=95, stored=11, started=NOW - timedelta(hours=1))
        series = self.report(stale_after_hours=0)["series"][0]
        self.assertEqual(series["fetched"], {"first": 100, "last": 95, "min": 90, "max": 100})
        self.assertEqual(series["stored"], {"first": 10, "last": 11, "min": 9, "max": 11})
        self.assertEqual(series["pages"], {"first": 1, "last": 1, "min": 1, "max": 1})
        self.assertEqual(self.report(stale_after_hours=0)["flags"], {},
                         "a source that wobbles by 10 percent is not flagged")

    def test_a_bound_that_hides_runs_is_reported(self):
        with patch.object(provider_health, "MAX_RUNS", 2):
            for index in range(4):
                run(self.conn, fetched=10 + index, started=NOW - timedelta(hours=index + 1))
            report = self.report()
        self.assertEqual(report["window"]["runs_read"], 2)
        self.assertTrue(report["window"]["run_read_bound_reached"])
        self.assertEqual(report["window"]["runs_outside_window"], 0)

    def test_a_series_bound_is_reported_rather_than_silently_truncating(self):
        run(self.conn, query="a")
        run(self.conn, query="b")
        report = self.report(limit=1)
        self.assertEqual(report["window"]["series"], 1)
        self.assertTrue(report["window"]["series_bound_reached"])

    def test_a_window_excludes_old_runs_and_says_how_many(self):
        run(self.conn, fetched=100, started=NOW - timedelta(hours=1))
        run(self.conn, fetched=1, started=NOW - timedelta(hours=100))
        report = self.report(since_hours=24, stale_after_hours=0)
        self.assertEqual(report["window"]["runs_considered"], 1)
        self.assertEqual(report["window"]["runs_outside_window"], 1)
        self.assertEqual(report["series"][0]["fetched"]["last"], 100)


class Flags(RegistryCase):

    def names(self, **kwargs):
        return {entry["flag"] for row in self.report(stale_after_hours=0, **kwargs)["series"]
                for entry in row["flags"]}

    def test_a_changed_recorded_request_stops_the_comparison_being_made(self):
        run(self.conn, fetched=500, request="req-1", started=NOW - timedelta(hours=2))
        run(self.conn, fetched=10, request="req-2", started=NOW - timedelta(hours=1))
        series = self.report(stale_after_hours=0)["series"][0]
        self.assertFalse(series["request_identity_stable"])
        self.assertEqual([entry["flag"] for entry in series["flags"]], ["request_changed"])
        self.assertEqual((series["fetched"]["first"], series["fetched"]["last"]), (500, 10),
                         "the run history is published oldest-first, so the comparison a reader "
                         "would make by hand is visible")

    def test_a_fetched_count_that_collapses_is_flagged_with_its_numbers(self):
        run(self.conn, fetched=500, started=NOW - timedelta(hours=2))
        run(self.conn, fetched=40, started=NOW - timedelta(hours=1))
        entry = next(item for item in self.report(stale_after_hours=0)["series"][0]["flags"]
                     if item["flag"] == "count_collapse")
        self.assertEqual(entry["peak"], 500)
        self.assertEqual(entry["latest"], 40)
        self.assertEqual(entry["threshold"], "0.5")

    def test_the_collapse_threshold_is_published_and_the_flag_does_not_fire_above_it(self):
        run(self.conn, fetched=100, started=NOW - timedelta(hours=2))
        run(self.conn, fetched=60, started=NOW - timedelta(hours=1))
        report = self.report(stale_after_hours=0)
        self.assertEqual(report["collapse_threshold"], "0.5")
        self.assertNotIn("count_collapse", {entry["flag"] for row in report["series"]
                                            for entry in row["flags"]})

    def test_a_series_that_stored_nothing_and_then_stored_rows_is_flagged_without_a_verdict(self):
        run(self.conn, fetched=11, stored=0, started=NOW - timedelta(hours=2))
        run(self.conn, fetched=11, stored=11, started=NOW - timedelta(hours=1))
        entry = next(item for item in self.report(stale_after_hours=0)["series"][0]["flags"]
                     if item["flag"] == "stored_nothing_once")
        self.assertIn("cannot say whether the source or this program changed", entry["detail"])

    def test_rows_fetched_and_none_stored_today_is_flagged(self):
        run(self.conn, fetched=9, stored=9, started=NOW - timedelta(hours=2))
        run(self.conn, fetched=9, stored=0, started=NOW - timedelta(hours=1))
        flags = {entry["flag"] for entry in self.report(stale_after_hours=0)["series"][0]["flags"]}
        self.assertIn("stored_zero_while_fetched", flags)

    def test_a_source_that_stopped_returning_anything_is_flagged(self):
        run(self.conn, fetched=500, started=NOW - timedelta(hours=2))
        run(self.conn, fetched=0, stored=0, started=NOW - timedelta(hours=1))
        flags = {entry["flag"] for entry in self.report(stale_after_hours=0)["series"][0]["flags"]}
        self.assertIn("returned_nothing", flags)

    def test_a_series_that_never_stored_anything_says_it_cannot_tell_why(self):
        run(self.conn, fetched=0, stored=0)
        entry = next(item for item in self.report(stale_after_hours=0)["series"][0]["flags"]
                     if item["flag"] == "never_stored")
        self.assertIn("a source with nothing to report and a source that stopped answering", entry["detail"])

    def test_always_truncated_is_reported_as_incomplete_coverage_not_as_a_fault(self):
        run(self.conn, truncated=True)
        run(self.conn, truncated=True, started=NOW - timedelta(hours=2))
        entry = next(item for item in self.report(stale_after_hours=0)["series"][0]["flags"]
                     if item["flag"] == "always_truncated")
        self.assertIn("completeness is unknown", entry["detail"])
        self.assertEqual(self.report(stale_after_hours=0)["completeness"], "unknown")

    def test_a_series_with_no_record_hash_says_content_change_cannot_be_seen(self):
        run(self.conn, records="")
        entry = next(item for item in self.report(stale_after_hours=0)["series"][0]["flags"]
                     if item["flag"] == "no_record_hash")
        self.assertIn("hash counts rather than record contents", entry["detail"])

    def test_staleness_is_an_age_and_says_which_of_two_things_it_cannot_tell(self):
        run(self.conn, started=NOW - timedelta(hours=100))
        entry = next(item for item in self.report(stale_after_hours=24)["series"][0]["flags"]
                     if item["flag"] == "stale")
        self.assertEqual(entry["age_hours"], "100.00")
        self.assertIn("either a source that stopped producing or an operator", entry["detail"])

    def test_staleness_can_be_switched_off_rather_than_reported_anyway(self):
        run(self.conn, started=NOW - timedelta(hours=100))
        self.assertEqual(self.report(stale_after_hours=0)["flags"], {})

    def test_every_flag_name_is_reachable_from_some_input(self):
        """A flag nothing can raise is one a reader cannot rely on being told."""
        scenarios = {
            "request_changed": [dict(fetched=5, request="a"),
                                dict(fetched=500, request="b")],
            "count_collapse": [dict(fetched=500), dict(fetched=40, stored=4)],
            "stored_nothing_once": [dict(fetched=11, stored=0), dict(fetched=11, stored=11)],
            "stored_zero_while_fetched": [dict(fetched=9, stored=9), dict(fetched=9, stored=0)],
            "returned_nothing": [dict(fetched=500, stored=5), dict(fetched=0, stored=0)],
            "never_stored": [dict(fetched=0, stored=0)],
            "always_truncated": [dict(truncated=True), dict(truncated=True)],
            "no_record_hash": [dict(records="")],
            "stale": [dict(started=NOW - timedelta(hours=100))],
        }
        triggered: dict = {}
        for name, steps in scenarios.items():
            conn = self._fresh()
            for index, step in enumerate(steps):
                # Oldest first, so the last step is the run a "latest" flag reads.
                run(conn, **dict({"started": NOW - timedelta(hours=len(steps) - index)},
                                 **step))
            with self.subTest(flag=name):
                flags = {entry["flag"] for row in
                         provider_health.report(conn, now=NOW,
                                                stale_after_hours=24)["series"]
                         for entry in row["flags"]}
                self.assertIn(name, flags, "the flag under test did not fire")
                triggered.update(dict.fromkeys(flags, True))
        self.assertEqual(set(provider_health.FLAGS) - set(triggered), set())

    def _fresh(self):
        """A separate in-memory registry, so one scenario cannot prime another."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        registry._initialize(conn)
        self.addCleanup(conn.close)
        return conn

    def test_a_run_with_no_readable_timestamp_does_not_break_the_report(self):
        run(self.conn, started=NOW - timedelta(hours=1))
        self.conn.execute("UPDATE ingest_runs SET started_at='not a time' WHERE id=1")
        report = self.report(stale_after_hours=0)
        self.assertEqual(report["window"]["series"], 1)
        self.assertIsNone(report["series"][0]["last_started_at"])
        self.assertIsNone(report["series"][0]["age_hours"],
                          "an unplaceable run has no age rather than an invented one")
        windowed = self.report(since_hours=24, stale_after_hours=0)
        self.assertEqual(windowed["window"]["series"], 0)
        self.assertEqual(windowed["window"]["runs_outside_window"], 1,
                         "a run that cannot be placed in a window is counted, not dropped")


class WhatItRefusesToSay(RegistryCase):

    def test_no_status_or_field_claims_a_source_is_healthy_or_failing(self):
        """The table cannot see a failure, so nothing may assert one.

        A failed run rolls back with its transaction and is never recorded, so the
        absence of a run is an absence of evidence. A field or status that said
        "healthy" would be read as a check that passed, and "failing" as one that
        failed; neither is available. The words appear only in the sentence saying
        so, which this also pins.
        """
        run(self.conn, fetched=100, stored=100, truncated=False)
        report = self.report()
        self.assertEqual(report["status"], "no_flags")
        self.assertEqual(provider_health.summary(self.conn, now=NOW)["status"], "no_flags")
        self.assertIn(report["status"], ("no_flags", "no_recorded_runs", "flags_present"))
        for row in report["series"] + [report]:
            for key in row:
                self.assertNotIn(key.lower(), ("healthy", "failing", "failed", "ok", "up",
                                               "down", "pass", "fail"))
        self.assertIn("nothing in this report can call a source failing or healthy",
                      " ".join(report["what_this_is_not"]))

    def test_the_report_states_the_failure_blind_spot_every_time(self):
        run(self.conn)
        report = self.report()
        joined = " ".join(report["what_this_is_not"]).lower()
        self.assertIn("a failed run rolls back with its transaction and is never recorded",
                      joined)
        self.assertIn("no run records a hash of the records themselves", joined)
        self.assertEqual(report["what_this_is_not"], list(provider_health.NOT_A_CHECK))

    def test_the_request_hash_is_not_uniformly_a_request_hash(self):
        run(self.conn)
        report = self.report()
        self.assertIn("sanctions stores the downloaded", " ".join(report["what_this_is_not"]))

    def test_completeness_is_unknown_even_with_every_series_reported(self):
        run(self.conn)
        report = self.report()
        self.assertEqual(report["completeness"], "unknown")
        self.assertIn("cannot show that a source has nothing else", report["completeness_note"])

    def test_an_unusable_window_is_refused_before_a_connection_is_read(self):
        for kwargs in ({"since_hours": -1}, {"stale_after_hours": -1}, {"limit": 0},
                       {"limit": provider_health.MAX_SERIES + 1}):
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    self.report(**kwargs)

    def test_the_report_survives_the_json_encoder(self):
        run(self.conn, warnings=["bounded partial coverage"], truncated=True)
        run(self.conn, fetched=1, stored=0, started=NOW - timedelta(hours=2))
        json.dumps(self.report(), allow_nan=False)
        json.dumps(provider_health.summary(self.conn), allow_nan=False)


class Sources(RegistryCase):

    def test_a_source_filter_narrows_the_read(self):
        run(self.conn, source="opensky")
        run(self.conn, source="sec", query="0000320193")
        report = self.report(source="sec", stale_after_hours=0)
        self.assertEqual([row["source"] for row in report["series"]], ["sec"])
        self.assertEqual(report["window"]["source_filter"], "sec")

    def test_the_source_rollup_counts_series_runs_and_flagged_series(self):
        run(self.conn, source="opensky", fetched=1, stored=0)
        run(self.conn, source="opensky", query="other", fetched=1, stored=1)
        run(self.conn, source="sec", query="0000320193", fetched=5, stored=5)
        rollup = self.report(stale_after_hours=0)["sources"]
        self.assertEqual(rollup["opensky"]["series"], 2)
        self.assertEqual(rollup["opensky"]["runs"], 2)
        self.assertEqual(rollup["opensky"]["flagged"], 1)
        self.assertEqual(rollup["sec"]["flagged"], 0)

    def test_warnings_are_collected_across_the_series_history(self):
        run(self.conn, warnings=["first"], started=NOW - timedelta(hours=2))
        run(self.conn, warnings=["first", "second"], started=NOW - timedelta(hours=1))
        self.assertEqual(self.report(stale_after_hours=0)["series"][0]["warnings"],
                         ["first", "second"])


class CommandLineAndDoctor(unittest.TestCase):

    def setUp(self):
        import os
        import tempfile
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = os.path.join(directory.name, "registry.db")
        db.init_db(self.path)
        with registry.get_conn(self.path) as conn:
            run(conn, source="opensky", fetched=500, stored=5, started=NOW - timedelta(hours=30))
            run(conn, source="opensky", fetched=10, stored=1, started=NOW - timedelta(hours=29))

    def test_providers_through_the_cli_writes_nothing(self):
        import hashlib

        def fingerprint():
            with open(self.path, "rb") as handle:
                return hashlib.sha256(handle.read()).hexdigest()

        before = fingerprint()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--db", self.path, "--json", "providers"]), 0)
        self.assertEqual(before, fingerprint(), "a read must not write, and this is a read")

    def test_the_cli_report_is_serialisable_and_names_its_flags(self):
        stream = io.StringIO()
        with redirect_stdout(stream):
            self.assertEqual(main(["--db", self.path, "--json", "providers"]), 0)
        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["flags_named"], list(provider_health.FLAGS))
        self.assertIn("count_collapse", payload["flags"])

    def test_doctor_carries_a_reduced_provider_block(self):
        stream = io.StringIO()
        with redirect_stdout(stream):
            self.assertEqual(main(["--db", self.path, "--json", "doctor"]), 0)
        health = json.loads(stream.getvalue())
        block = health["providers"]
        self.assertEqual(block["sources"], 1)
        self.assertEqual(block["flagged_series"], 1)
        self.assertEqual(block["worst"][0]["source"], "opensky")
        self.assertIn("A failed run rolls back", " ".join(block["what_this_is_not"]))

    def test_doctor_reports_provider_health_as_unreadable_rather_than_omitting_it(self):
        with registry.get_conn(self.path) as conn:
            conn.execute("DROP TABLE ingest_runs")
        stream = io.StringIO()
        with redirect_stdout(stream):
            self.assertEqual(main(["--db", self.path, "--json", "doctor"]), 0,
                             "doctor reports what it found rather than failing on it")
        health = json.loads(stream.getvalue())
        self.assertEqual(health["status"], "incomplete",
                         "the registry itself is reported incomplete for the missing table")
        self.assertEqual(health["providers"]["status"], "unreadable")
        self.assertNotIn("error", health["providers"],
                         "the message is classified, not the exception echoed")
        self.assertIn("no source is reported here in either direction",
                      health["providers"]["note"])


if __name__ == "__main__":
    unittest.main()
