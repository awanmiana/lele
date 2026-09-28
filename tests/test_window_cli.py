"""The CLI surface for asking about a window, tested end to end and offline.

Covers the argument contract as much as the output: an ambiguous timestamp has
to be refused before a database is opened, a read-only command must leave the
file untouched, and `--summary` must print exactly one line and nothing else.
"""
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path

from lele.cli.main import _read_only, main
from lele.core import registry

KEY = "binance:BTCUSDT"
DAY = 86400
BASE = datetime(2026, 3, 1, tzinfo=UTC)
START = (BASE + timedelta(days=30)).isoformat()
END = (BASE + timedelta(days=37)).isoformat()


def invoke(*args):
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        status = main(list(args))
    return status, out.getvalue(), err.getvalue()


class WindowCliTests(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db = str(self.root / "registry.db")
        registry.init_db(self.db)
        with registry.get_conn(self.db) as conn:
            self.eid = registry.upsert_entity(conn, "instrument", "Bitcoin", key=KEY)
            for day in range(90):
                moment = BASE + timedelta(days=day)
                close = 100.0 + day * 0.5 - (18.0 if 60 <= day <= 64 else 0.0)
                registry.add_price_bar(
                    conn, instrument_key=KEY, interval_seconds=DAY,
                    open_time=moment.isoformat(),
                    close_time=(moment + timedelta(days=1)).isoformat(),
                    open=str(close), high=str(close * 1.02), low=str(close * 0.98),
                    close=str(close), volume="1000", source="binance",
                    retrieved_at=moment.isoformat(), source_url="https://api.binance.com")
            for day in range(25, 45):
                when = (BASE + timedelta(days=day)).isoformat()
                registry.add_observation(
                    conn, source="defillama", external_id=f"ss{day}",
                    kind="stablecoin_supply", description="fixture supply",
                    instrument_key="", amount="300000000000", unit="usd", currency="USD",
                    basis="observed", occurred_at=when, observed_at=when,
                    available_at=when, source_url="https://example.test",
                    evidence=json.dumps({"fixture": True}))

    def explain(self, *extra, as_json=True):
        args = ["--db", self.db]
        if as_json:
            args.append("--json")
        return invoke(*args, "explain", str(self.eid), "--from", START, "--to", END, *extra)

    # --- argument contract

    def test_a_bare_date_is_refused_as_ambiguous(self):
        """A window with no offset cannot be compared against recorded times."""
        for start in ("2026-03-31", "2026-03-31T00:00:00", "2026-03-31 00:00"):
            with self.subTest(start=start):
                status, out, err = invoke(
                    "--db", self.db, "--json", "explain", str(self.eid),
                    "--from", start, "--to", END)
                self.assertEqual(status, 2)
                self.assertIn("timezone offset", err)
                self.assertEqual(out, "")

    def test_nonsense_timestamps_are_refused(self):
        for start in ("yesterday", "2026-13-45", "175", ""):
            with self.subTest(start=start):
                status, out, err = invoke(
                    "--db", self.db, "--json", "explain", str(self.eid),
                    "--from", start, "--to", END)
                self.assertEqual(status, 2)
                self.assertTrue(err)
                self.assertEqual(out, "")

    def test_an_unparsable_instant_is_refused(self):
        status, out, err = invoke("--db", self.db, "--json", "explain", str(self.eid),
                                  "--from", "not-a-time", "--to", END)
        self.assertEqual(status, 2)
        self.assertIn("ISO-8601", err)

    def test_a_reversed_window_is_refused(self):
        status, out, err = invoke("--db", self.db, "--json", "explain", str(self.eid),
                                  "--from", END, "--to", START)
        self.assertEqual(status, 2)
        self.assertIn("before", err)

    def test_a_bad_interval_is_refused_by_argparse(self):
        status, out, err = self.explain("--interval", "7d")
        self.assertEqual(status, 2)
        self.assertIn("interval", err)

    def test_a_reversed_window_is_a_usage_error_not_a_crash(self):
        status, out, err = invoke("--db", self.db, "--json", "explain", str(self.eid),
                                  "--from", END, "--to", START)
        self.assertEqual(status, 2, "bad input is exit 2, the same as a bad argument")
        self.assertIn("before", err)
        self.assertEqual(out, "")

    def test_a_window_over_ten_years_is_refused(self):
        status, out, err = invoke("--db", self.db, "--json", "explain", str(self.eid),
                                  "--from", "2000-01-01T00:00:00+00:00",
                                  "--to", "2026-01-01T00:00:00+00:00")
        self.assertEqual(status, 2)
        self.assertIn("ten years", err)

    def test_an_unknown_entity_is_refused_with_a_usable_message(self):
        status, out, err = invoke("--db", self.db, "--json", "explain", "9999",
                                  "--from", START, "--to", END)
        self.assertEqual(status, 2)
        self.assertIn("not found", err)
        self.assertNotIn("Traceback", err)

    def test_a_missing_registry_is_reported_not_created(self):
        absent = str(self.root / "absent.db")
        status, out, err = invoke("--db", absent, "--json", "explain", "1",
                                  "--from", START, "--to", END)
        self.assertEqual(status, 1)
        self.assertIn("registry not found", err)
        self.assertFalse(Path(absent).exists())

    # --- output contract

    def test_explain_returns_a_full_report_as_json(self):
        status, out, err = self.explain()
        self.assertEqual((status, err), (0, ""))
        report = json.loads(out)
        self.assertEqual(report["method"], "window_explain_v1")
        self.assertEqual(report["instrument"]["entity_key"], KEY)
        self.assertTrue(report["price"]["measured"])
        self.assertTrue(report["coverage"]["channels"])
        self.assertFalse(any(row["is_cause"]
                             for row in report["records"]["inside_window"]))

    def test_summary_prints_exactly_one_line_and_nothing_else(self):
        status, out, err = self.explain("--summary", as_json=False)
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertIn("nothing here is marked as a cause", out)

    def test_capital_returns_labelled_series(self):
        status, out, err = invoke("--db", self.db, "--json", "capital", str(self.eid),
                                  "--from", START, "--to", END)
        self.assertEqual((status, err), (0, ""))
        report = json.loads(out)
        self.assertEqual(report["method"], "capital_positioning_view_v1")
        self.assertIn("stablecoin_supply", report["measured_series"])
        label = report["what_each_number_is"]["stablecoin_supply"]
        self.assertIn("not a net flow", label["is_net_flow"])
        self.assertTrue(report["not_available"])

    def test_capital_summary_is_one_line(self):
        status, out, err = invoke("--db", self.db, "capital", str(self.eid),
                                  "--from", START, "--to", END, "--summary")
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertIn("not a net flow", out)

    def test_market_wide_can_be_excluded(self):
        _, out, _ = self.explain("--no-market-wide")
        report = json.loads(out)
        self.assertNotIn("stablecoin_supply",
                         report["coverage"]["stored_observations"]["kinds"])

    def test_reading_a_window_does_not_write_to_the_registry(self):
        import hashlib

        def digest():
            return hashlib.sha256(Path(self.db).read_bytes()).hexdigest()

        before = digest()
        self.explain()
        invoke("--db", self.db, "--json", "capital", str(self.eid),
               "--from", START, "--to", END)
        self.assertEqual(before, digest(),
                         "explain and capital are read-only and must leave the file alone")

    def test_reading_a_window_works_on_a_read_only_file(self):
        import os
        os.chmod(self.db, 0o444)
        self.addCleanup(os.chmod, self.db, 0o644)
        status, out, err = self.explain()
        self.assertEqual((status, err), (0, ""))
        self.assertTrue(json.loads(out)["coverage"]["channels"])

    def test_the_window_is_reproducible(self):
        _, first, _ = self.explain()
        _, second, _ = self.explain()
        self.assertEqual(json.loads(first), json.loads(second),
                         "the same window must give the same answer")

    def test_both_commands_are_classified_read_only(self):
        from lele.cli.main import build_parser
        parser = build_parser()
        for command in ("explain", "capital"):
            with self.subTest(command=command):
                args = parser.parse_args([command, "1", "--from", START, "--to", END])
                self.assertTrue(_read_only(command, args))

    def test_the_help_text_states_what_the_command_does_not_do(self):
        status, out, err = invoke("explain", "--help")
        self.assertEqual(status, 0)
        combined = out + err
        self.assertIn("what is recorded", combined.lower())

    def test_a_wide_window_spanning_a_gap_still_reports_the_gap(self):
        with registry.get_conn(self.db) as conn:
            conn.execute("DELETE FROM price_bars WHERE open_time=?",
                         ((BASE + timedelta(days=33)).isoformat(),))
        _, out, _ = self.explain()
        span = json.loads(out)["coverage"]["bar_history"]
        self.assertGreater(span["gaps"], 0)
        self.assertFalse(span["usable"])
        self.assertEqual(span["completeness"], "unknown")


if __name__ == "__main__":
    unittest.main()
