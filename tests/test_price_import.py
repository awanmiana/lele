"""A stored bar must carry the provenance that makes it usable later.

`fetch-history` reaches years back for Binance spot pairs and nothing else, so a
listed equity or an exchange-traded commodity has no stored bars, and therefore
no measured window: `explain` can report an issuer's filings and holdings while
having no price to measure them against. That gap is a data gap, and the only
permitted way to close it is for the user to supply an export from a source they
may use, since every free no-key provider either restricts automated use or, in
Stooq's case, answers with a browser-verification challenge that is not ours to
defeat.

So these tests pin what makes such an import trustworthy: that every bar is
validated before anything is written, that the same bar expressed in two UTC
offsets is one bar rather than two, that an unstated adjustment basis is reported
unknown instead of being assumed, and that nothing in the path reaches the
network.

No network. The exports are synthetic fixtures.
"""
import inspect
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, UTC
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from lele.analysis import price_import
from lele.cli.main import main
from lele.core import registry

KEY = "test:EQUITY-A"
BASE = datetime(2026, 3, 2, tzinfo=UTC)          # a Monday
DAY = 86400
# Far enough from any plausible run that the "has closed" rule cannot depend on
# today's date. A fixture dated today is what made this suite decay once already.
FROZEN = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


def payload(**overrides):
    """Four consecutive daily bars, Monday to Thursday, with a weekend behind them."""
    bars = []
    for index, (day, close) in enumerate(((2, "100.00"), (3, "101.00"), (4, "99.50"),
                                          (5, "103.00"))):
        opened = BASE + timedelta(days=day - 2)
        bars.append({"open_time": opened.isoformat(),
                     "close_time": (opened + timedelta(days=1)).isoformat(),
                     "open": close, "high": "104.00", "low": "98.00",
                     "close": close, "volume": "1000000"})
    document = {
        "instrument": {"symbol": "EQUITY-A", "venue": "TESTEX", "currency": "USD",
                       "asset_class": "equity", "adjustment": "adjusted",
                       "rights_basis": "synthetic fixture"},
        "source": "fixture",
        "source_url": "local:synthetic-fixture",
        "retrieved_at": FROZEN.isoformat(),
        "bars": bars,
    }
    document.update(overrides)
    return document


def week(**overrides):
    """Nine trading days across one weekend: Mon-Fri, then Mon-Wed.

    A two-bar series cannot distinguish a weekend from the cadence, because the
    only spacing it contains *is* the cadence. A real week is what makes
    "one day apart, except across a weekend" a measurable statement rather than a
    guess.
    """
    days = [2, 3, 4, 5, 6, 9, 10, 11, 12]
    bars = []
    for index, day in enumerate(days):
        opened = BASE + timedelta(days=day - 2)
        close = f"{100 + index}.00"
        bars.append({"open_time": opened.isoformat(),
                     "close_time": (opened + timedelta(days=1)).isoformat(),
                     "open": close, "high": "120.00", "low": "90.00",
                     "close": close, "volume": "1000000"})
    return payload(bars=bars, **overrides)


class ImportCase(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.conn = sqlite3.connect(Path(directory.name) / "registry.db")
        self.conn.row_factory = sqlite3.Row
        registry._initialize(self.conn)
        self.addCleanup(self.conn.close)
        self.entity_id = registry.upsert_entity(conn=self.conn, kind="instrument",
                                                name="Equity A", key=KEY)
        # Committed, so a rejected import rolling back its own transaction cannot
        # take the fixture entity with it and fail the next assertion for the
        # wrong reason.
        self.conn.commit()
        self.directory = directory.name

    def write(self, document=None, name="bars.json"):
        path = Path(self.directory) / name
        path.write_text(json.dumps(document if document is not None else payload()),
                        encoding="utf-8")
        return str(path)

    def run_import(self, document=None, name="bars.json", **kwargs):
        path = self.write(document, name)
        with self.conn:
            return price_import.import_price_history(self.conn, self.entity_id, path,
                                                     now=FROZEN, **kwargs)

    def stored(self):
        return registry.list_price_bars(self.conn, KEY, DAY, order="asc")


class StoredTests(ImportCase):

    def test_bars_are_stored_with_measured_coverage(self):
        report = self.run_import()
        self.assertEqual(report["coverage"]["stored_this_run"], 4)
        self.assertEqual(report["coverage"]["retained_bars"], 4)
        self.assertEqual([row["close"] for row in self.stored()],
                         ["100.00", "101.00", "99.50", "103.00"])
        self.assertEqual(report["coverage"]["oldest_open_time"], BASE.isoformat())
        self.assertEqual(report["coverage"]["newest_open_time"],
                         (BASE + timedelta(days=3)).isoformat())

    def test_cadence_is_measured_not_assumed(self):
        # A 1d label over a week containing a weekend is not one day apart
        # throughout, so the report must measure the dominant spacing rather than
        # repeat the interval the caller passed.
        report = self.run_import(week())
        self.assertEqual(report["coverage"]["measured_cadence_seconds"], DAY)
        self.assertIn("cadence_note", report["coverage"])

    def test_a_weekend_hole_is_counted_rather_than_hidden(self):
        report = self.run_import(week())
        self.assertEqual(report["coverage"]["stored_this_run"], 9)
        self.assertEqual(report["coverage"]["gaps_detected"], 1)
        self.assertFalse(report["coverage"]["complete"])
        self.assertIn("may start late", report["coverage"]["completeness_note"])

    def test_provenance_and_unknowns_are_recorded(self):
        report = self.run_import()
        self.assertEqual(report["provenance"]["source_url"], "local:synthetic-fixture")
        self.assertEqual(report["provenance"]["retrieved_at"], FROZEN.isoformat())
        self.assertFalse(report["provenance"]["rights_verified"])
        self.assertEqual(len(report["provenance"]["bars_sha256"]), 64)
        self.assertEqual(report["instrument"]["adjustment"], "adjusted")
        self.assertEqual(report["instrument"]["currency"], "USD")
        self.assertEqual(report["unknowns"], [])

    def test_ingest_run_is_recorded_for_later_health_checks(self):
        run_id = self.run_import()["provenance"]["ingest_run_id"]
        row = self.conn.execute("SELECT source, query, indicator, stored, total, records_sha256"
                                " FROM ingest_runs WHERE id=?", (run_id,)).fetchone()
        self.assertEqual(tuple(row)[:5], ("import-history", "EQUITY-A", "1d", 4, 4))
        self.assertEqual(len(row["records_sha256"]), 64)

    def test_no_network_path_exists_in_this_module(self):
        # The report claims network_access is false. That claim is only worth
        # something if the module cannot reach out, so the absence is asserted
        # rather than left to a reader.
        source = inspect.getsource(price_import)
        self.assertNotIn("HTTPClient", source)
        self.assertNotIn("urlopen", source)
        self.assertNotIn("urllib.request", source)
        self.assertFalse(self.run_import()["parameters"]["network_access"])

    def test_row_evidence_records_the_stated_basis(self):
        self.run_import()
        evidence = json.loads(self.stored()[0]["evidence"])
        self.assertEqual(evidence["adjustment"], "adjusted")
        self.assertEqual(evidence["venue"], "TESTEX")
        self.assertEqual(evidence["asset_class"], "equity")
        self.assertFalse(evidence["trades_applicable"])
        self.assertEqual(self.stored()[0]["trades"], 0)


class IdempotenceTests(ImportCase):

    def test_the_same_bar_in_two_offsets_is_one_bar(self):
        # `price_bars` is keyed on open time as text. The same session written as
        # a local New York morning and as UTC would otherwise be two rows for one
        # bar, so the re-import would duplicate instead of refine.
        document = payload(bars=[payload()["bars"][0]])
        document["bars"][0]["open_time"] = "2026-03-02T09:30:00-05:00"
        document["bars"][0]["close_time"] = "2026-03-03T09:30:00-05:00"
        self.run_import(document)
        self.assertEqual(len(self.stored()), 1)
        self.assertEqual(self.stored()[0]["open_time"], "2026-03-02T14:30:00+00:00")
        self.run_import(document, name="again.json")
        self.assertEqual(len(self.stored()), 1)

    def test_a_repeated_import_updates_rather_than_duplicates(self):
        first = self.run_import()["provenance"]["bars_sha256"]
        second = self.run_import(name="second.json")
        self.assertEqual(len(self.stored()), 4)
        self.assertEqual(second["provenance"]["bars_sha256"], first)
        self.assertEqual(second["coverage"]["stored_this_run"], 4)

    def test_duplicate_open_times_in_one_file_are_collapsed_and_reported(self):
        document = payload()
        document["bars"].append(dict(document["bars"][0]))
        report = self.run_import(document)
        self.assertEqual(report["skipped"]["duplicate"], 1)
        self.assertEqual(report["coverage"]["stored_this_run"], 4)
        self.assertEqual(len(self.stored()), 4)

    def test_a_close_time_is_derived_when_omitted_and_says_so(self):
        document = payload(bars=[payload()["bars"][0]])
        document["bars"][0].pop("close_time")
        self.run_import(document)
        row = self.stored()[0]
        self.assertEqual(row["close_time"], (BASE + timedelta(days=1)).isoformat())
        self.assertTrue(json.loads(row["evidence"])["close_time_derived"])


class ValidationTests(ImportCase):

    def assertRejected(self, document, fragment):
        with self.assertRaises(ValueError) as caught:
            self.run_import(document)
        self.assertIn(fragment, str(caught.exception))
        self.assertEqual(self.stored(), [], "a rejected file must store nothing")

    def test_adjustment_must_be_stated(self):
        document = payload()
        document["instrument"].pop("adjustment")
        self.assertRejected(document, "instrument requires adjustment")
        document = payload()
        document["instrument"]["adjustment"] = "split-adjusted"
        self.assertRejected(document, "adjustment must be one of")

    def test_an_unknown_adjustment_is_reported_as_unknown(self):
        document = payload()
        document["instrument"]["adjustment"] = "unknown"
        report = self.run_import(document)
        self.assertTrue(any("adjustment" in item for item in report["unknowns"]))

    def test_absent_optional_fields_are_unknown_rather_than_guessed(self):
        document = payload()
        for field in ("venue", "asset_class", "rights_basis"):
            document["instrument"].pop(field)
        report = self.run_import(document)
        self.assertEqual(report["instrument"]["venue"], "unknown")
        self.assertEqual(report["instrument"]["asset_class"], "unknown")
        self.assertEqual(len(report["unknowns"]), 3)
        self.assertEqual(json.loads(self.stored()[0]["evidence"])["venue"], "unknown")

    def test_a_bar_that_closes_in_the_future_is_not_a_measurement(self):
        document = payload()
        document["bars"][-1]["close_time"] = (FROZEN + timedelta(days=1)).isoformat()
        self.assertRejected(document, "close_time is in the future")

    def test_a_naive_timestamp_is_refused(self):
        document = payload()
        document["bars"][0]["open_time"] = "2026-03-02T09:30:00"
        self.assertRejected(document, "aware ISO8601")

    def test_an_inconsistent_bar_stores_nothing(self):
        # The bad row is last on purpose: a validator that checked only the first
        # bars would already have written them.
        document = payload()
        document["bars"][-1]["high"] = "1.00"
        document["bars"][-1]["low"] = "98.00"
        self.assertRejected(document, "high must not be below low")

    def test_a_close_outside_the_range_is_refused(self):
        document = payload()
        document["bars"][-1]["close"] = "500.00"
        self.assertRejected(document, "close must lie within")

    def test_a_missing_volume_is_refused_rather_than_read_as_zero(self):
        document = payload()
        document["bars"][0].pop("volume")
        self.assertRejected(document, "no unknown representation")

    def test_an_unknown_field_is_refused(self):
        document = payload()
        document["bars"][0]["adj_close"] = "99.00"
        self.assertRejected(document, "Invalid fields in bar")

    def test_a_non_https_source_reference_is_refused(self):
        self.assertRejected(payload(source_url="http://intranet.test/export.csv"),
                            "public HTTPS")

    def test_a_local_reference_is_accepted(self):
        report = self.run_import(payload(source_url="local:broker-export-2026-03"))
        self.assertEqual(report["provenance"]["source_url"], "local:broker-export-2026-03")

    def test_a_nonfinite_price_is_refused(self):
        path = Path(self.directory) / "nan.json"
        path.write_text(json.dumps(payload()).replace('"101.00"', "NaN"), encoding="utf-8")
        with self.assertRaises(ValueError):
            price_import.import_price_history(self.conn, self.entity_id, str(path),
                                             now=FROZEN)
        self.assertEqual(self.stored(), [])

    def test_a_missing_required_top_level_field_is_refused(self):
        for field in ("source", "source_url", "retrieved_at", "bars", "instrument"):
            document = payload()
            document.pop(field)
            self.assertRejected(document, f"payload requires {field}")

    def test_an_empty_bar_list_is_refused(self):
        self.assertRejected(payload(bars=[]), "at least one record")

    def test_an_unknown_interval_is_refused_before_the_file_is_read(self):
        with self.assertRaises(ValueError) as caught:
            price_import.validate_request("7d", FROZEN)
        self.assertIn("interval must be one of", str(caught.exception))

    def test_an_unknown_entity_is_refused(self):
        path = self.write()
        with self.assertRaises(ValueError) as caught:
            price_import.import_price_history(self.conn, 987654, path, now=FROZEN)
        self.assertIn("not found", str(caught.exception))

    def test_a_duplicate_json_field_is_refused(self):
        path = Path(self.directory) / "dup.json"
        text = json.dumps(payload())
        path.write_text(text[:-1] + f', "retrieved_at": "{FROZEN.isoformat()}"}}',
                        encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            price_import.import_price_history(self.conn, self.entity_id, str(path), now=FROZEN)
        self.assertIn("Duplicate JSON field", str(caught.exception))

    def test_an_oversized_file_is_refused(self):
        path = Path(self.directory) / "big.json"
        path.write_bytes(b"{" + b"x" * (price_import.MAX_FILE_BYTES + 1) + b"}")
        with self.assertRaises(ValueError) as caught:
            price_import.import_price_history(self.conn, self.entity_id, str(path), now=FROZEN)
        self.assertIn("10 MiB", str(caught.exception))


def long_series(days=30, drop_at=22, drop_percent=Decimal("14")):
    """`days` consecutive daily bars with one clear fall on `drop_at`.

    The level shift is permanent, so the series has one large move rather than a
    drop followed by a rebound that would be the larger of the two. Consecutive
    calendar days keep the cadence unambiguous; the only question this test asks
    is whether a non-Binance instrument can be measured at all once its bars are
    in the registry.
    """
    bars = []
    for index in range(days):
        level = Decimal("100.00") * (Decimal("1.002") ** index)
        if index >= drop_at:
            level = level * (Decimal(1) - drop_percent / Decimal(100))
        opened = BASE + timedelta(days=index)
        bars.append({"open_time": opened.isoformat(),
                     "close_time": (opened + timedelta(days=1)).isoformat(),
                     "open": str(level.quantize(Decimal("0.01"))),
                     "high": str((level * Decimal("1.01")).quantize(Decimal("0.01"))),
                     "low": str((level * Decimal("0.99")).quantize(Decimal("0.01"))),
                     "close": str(level.quantize(Decimal("0.01"))),
                     "volume": "1000000"})
    return payload(bars=bars)


class CliTests(unittest.TestCase):
    """The command path, because a library nobody can invoke is not a feature."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "registry.db"
        self.export = self.root / "equity.json"
        self.export.write_text(json.dumps(long_series()), encoding="utf-8")
        entities = self.root / "entities.json"
        entities.write_text(json.dumps({"entities": [
            {"key": KEY, "kind": "instrument", "name": "Equity A", "country": "US",
             "attributes": {"source_url": "local:synthetic-fixture"}}]}), encoding="utf-8")
        # If this import ever reached out, its no-network claim would be a report
        # field rather than a property, so the client itself is made unusable.
        guard = patch("lele.fetchers.http.HTTPClient",
                      side_effect=AssertionError("import-history must not reach the network"))
        guard.start()
        self.addCleanup(guard.stop)

    def invoke(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = main(["--db", str(self.db), "--json", *args])
        self.assertIs(type(status), int)
        return status, out.getvalue(), err.getvalue()

    def success(self, *args):
        status, out, err = self.invoke(*args)
        self.assertEqual((status, err), (0, ""), out)
        return json.loads(out)

    def entity_id(self):
        self.success("init")
        self.success("import", str(self.root / "entities.json"))
        return self.success("list", "--kind", "instrument")[0]["id"]

    def test_a_listed_equity_gets_a_measured_window(self):
        # The gap this closes: an issuer's SEC filings and holdings were stored and
        # reportable while there was no price to measure any window against.
        entity = self.entity_id()
        report = self.success("import-history", str(entity), str(self.export))
        self.assertEqual(report["coverage"]["stored_this_run"], 30)
        self.assertEqual(report["instrument"]["symbol"], "EQUITY-A")
        self.assertFalse(report["provenance"]["rights_verified"])

        detected = self.success("moves", str(entity), "--interval", "1d", "--move-hours", "24",
                                "--thresholds", "5", "--no-store")
        self.assertEqual(detected["coverage"]["cadence_seconds"], 86400)
        retained = detected["moves"]
        self.assertTrue(retained, f"no move detected: {detected}")
        biggest = max(retained, key=lambda move: abs(Decimal(move["change_percent"])))
        self.assertEqual(biggest["direction"], "down")
        self.assertGreater(abs(Decimal(biggest["change_percent"])), 13)

    def test_a_missing_file_is_refused_before_the_registry_is_opened(self):
        status, _, err = self.invoke("import-history", "1", str(self.root / "absent.json"))
        self.assertEqual(status, 2)
        self.assertIn("no such file", err)
        self.assertFalse(self.db.exists())

    def test_an_unknown_entity_is_a_cli_error(self):
        self.success("init")
        status, _, err = self.invoke("import-history", "4242", str(self.export))
        self.assertEqual(status, 2)
        self.assertIn("4242 not found", err)

    def test_an_unknown_interval_is_refused_by_the_parser(self):
        status, out, err = self.invoke("import-history", "1", str(self.export), "--interval", "7d")
        self.assertEqual((status, out), (2, ""))
        self.assertIn("invalid choice", err)


if __name__ == "__main__":
    unittest.main()
