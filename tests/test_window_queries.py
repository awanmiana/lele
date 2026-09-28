"""Asking about a time window, and asking what capital or positioning moved.

The question the tool could not answer was "what is recorded for this instrument
between these two instants". Three things had to be true for it to be answered
honestly, and each has a test here.

* The window is an input, not a side effect. Nothing in `timeline` reads a clock,
  so the same call returns the same answer tomorrow.
* Absence is reported as absence. A channel with no records says so and names
  the command that would fill it, because "nothing was recorded" and "nothing
  happened" are different claims and only the first is knowable here.
* Nothing is called a cause. Every record carries `is_cause: false` and a
  relation that is only ever `coincident` or `precursor`.

No network. Bars and observations are written straight into a temporary
registry.
"""
import json
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lele.analysis import evidence_store, timeline
from lele.core import registry

KEY = "binance:BTCUSDT"
DAY = 86400
BASE = datetime(2026, 3, 1, tzinfo=UTC)
START = (BASE + timedelta(days=30)).isoformat()
END = (BASE + timedelta(days=37)).isoformat()


def bar(conn, day, close, instrument=KEY, interval=DAY):
    moment = BASE + timedelta(days=day)
    registry.add_price_bar(
        conn, instrument_key=instrument, interval_seconds=interval,
        open_time=moment.isoformat(), close_time=(moment + timedelta(days=1)).isoformat(),
        open=str(close), high=str(close * 1.02), low=str(close * 0.98), close=str(close),
        volume="1000", source="binance", retrieved_at=moment.isoformat(),
        source_url="https://api.binance.com/api/v3/klines")


def observe(conn, kind, external_id, occurred, amount=None, unit="usd", currency="USD",
            instrument="", source="fixture", basis="observed"):
    registry.add_observation(
        conn, source=source, external_id=external_id, kind=kind,
        description=f"{kind} fixture record", instrument_key=instrument,
        amount=str(amount) if amount is not None else None, unit=unit if amount else "",
        currency=currency, basis=basis, occurred_at=occurred, observed_at=occurred,
        available_at=occurred, source_url="https://example.test/source",
        evidence=json.dumps({"fixture": True}))


class WindowCase(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.conn = sqlite3.connect(Path(directory.name) / "registry.db")
        self.conn.row_factory = sqlite3.Row
        registry._initialize(self.conn)
        self.addCleanup(self.conn.close)
        self.eid = registry.upsert_entity(conn=self.conn, kind="instrument", name="Bitcoin",
                                          key=KEY)
        self.conn.commit()

    def seed_bars(self, days=90, instrument=KEY):
        for day in range(days):
            # a rise with a visible drawdown, so there is something to segment
            close = 100.0 + day * 0.5 - (18.0 if 60 <= day <= 64 else 0.0)
            bar(self.conn, day, close, instrument)

    def seed_context(self):
        for day in range(25, 45):
            when = (BASE + timedelta(days=day)).isoformat()
            observe(self.conn, "stablecoin_supply", f"ss{day}", when,
                    amount=300_000_000_000 + day * 1_000_000, instrument="",
                    source="defillama")
            observe(self.conn, "social_sentiment", f"sf{day}", when,
                    amount=50 + (day % 20), unit="index_0_100", currency="", instrument="",
                    source="alternative-me", basis="estimated")
            observe(self.conn, "open_interest", f"oi{day}", when,
                    amount=5_000_000 + day * 1000, unit="currency", currency="USDT")
            observe(self.conn, "regulatory_action", f"fr{day}", when, amount=None,
                    unit="", currency="", source="federal-register")


class ExplainTests(WindowCase):

    def report(self, **kwargs):
        arguments = {"interval_seconds": DAY, "pre_hours": 72, "threshold_percent": 3,
                     "move_hours": 24, "thresholds": (3, 5, 7, 11), "limit": 1000}
        arguments.update(kwargs)
        return timeline.explain(self.conn, self.eid, START, END, **arguments)

    def test_a_window_measures_from_stored_bars(self):
        self.seed_bars()
        report = self.report()
        price = report["price"]
        self.assertTrue(price["measured"])
        self.assertEqual(price["bars_in_window"], 7, "one week of daily bars")
        self.assertIsNotNone(price["change_percent"])
        self.assertIsNotNone(price["largest_bar_move_percent"])
        self.assertIsNotNone(price["largest_bar_move_at"])
        self.assertIn(price["largest_bar_move_direction"], ("up", "down"))

    def test_nothing_is_ever_marked_a_cause(self):
        self.seed_bars()
        self.seed_context()
        report = self.report()
        records = report["records"]["inside_window"] + report["records"]["before_window"]
        self.assertTrue(records)
        for row in records:
            self.assertFalse(row["is_cause"])
            self.assertIn(row["relation"], ("coincident", "precursor"))
        self.assertIn("marked as a cause", report["what_this_is"]["it_is_not"])

    def test_records_split_into_window_and_precursors(self):
        self.seed_context()
        report = self.report(pre_hours=72)
        inside = report["records"]["inside_window"]
        before = report["records"]["before_window"]
        self.assertTrue(inside)
        self.assertTrue(before)
        for row in inside:
            self.assertGreaterEqual(row["occurred_at"], START)
            self.assertLess(row["occurred_at"], END)
            self.assertEqual(row["relation"], "coincident")
        for row in before:
            self.assertLess(row["occurred_at"], START)
            self.assertEqual(row["relation"], "precursor")

    def test_records_carry_provenance(self):
        self.seed_context()
        report = self.report()
        for row in report["records"]["inside_window"]:
            self.assertTrue(row["source"], "every record names its source")
            self.assertTrue(row["source_url"], "every record names where it came from")
            self.assertTrue(row["available_at"], "every record says when it became known")
            self.assertIn(row["family"], timeline.signals.OBSERVATION_FAMILY.values())

    def test_missing_channels_are_reported_with_a_remedy(self):
        self.seed_bars()
        report = self.report()
        channels = {item["channel"]: item for item in report["coverage"]["channels"]}
        self.assertEqual(channels["stored_bars"]["status"], "present")
        self.assertEqual(channels["headlines"]["status"], "not_stored")
        self.assertTrue(channels["headlines"]["remedy"])
        self.assertIn("not persisted", channels["headlines"]["note"])
        for name, item in channels.items():
            if item["status"] == "no_coverage":
                self.assertTrue(item["remedy"], f"{name} has no way to be filled")

    def test_bar_history_coverage_is_reported_before_anything_is_measured(self):
        report = self.report()
        span = report["coverage"]["bar_history"]
        self.assertEqual(span["bars"], 0)
        self.assertFalse(span["usable"])
        self.assertIn("fetch-history", span["note"])
        self.assertFalse(report["price"]["measured"])
        self.assertIn("fewer than two stored bars", report["price"]["note"])

    def test_a_gap_in_the_history_is_reported(self):
        self.seed_bars()
        for day in range(33, 35):
            self.conn.execute("DELETE FROM price_bars WHERE open_time=?",
                              ((BASE + timedelta(days=day)).isoformat(),))
        self.conn.commit()
        span = self.report()["coverage"]["bar_history"]
        self.assertGreater(span["gaps"], 0)
        self.assertFalse(span["usable"])
        self.assertEqual(span["completeness"], "unknown",
                         "a bounded fetch cannot prove it reached the first bar")

    def test_episodes_come_from_the_stored_series(self):
        self.seed_bars()
        report = self.report(threshold_percent=3)
        self.assertTrue(report["episodes"], "an 18% drawdown must produce legs")
        for leg in report["episodes"]:
            self.assertIn(leg["direction"], ("up", "down"))
            self.assertLessEqual(len(leg["magnitude_percent"]), 12,
                                 "a percentage is rounded, not printed at 128 digits")
            self.assertLess(leg["start"], leg["end"])
            self.assertIn("started_before_window", leg)

    def test_the_result_is_reproducible(self):
        self.seed_bars()
        self.seed_context()
        first = json.dumps(self.report(), sort_keys=True, default=str)
        second = json.dumps(self.report(), sort_keys=True, default=str)
        self.assertEqual(first, second, "the same window must give the same answer")

    def test_a_window_with_no_stored_data_says_so_rather_than_guessing(self):
        report = self.report()
        self.assertEqual(report["price"]["bars_in_window"], 0)
        self.assertEqual(report["records"]["total_in_scope"], 0)
        self.assertEqual(report["episodes"], [])
        self.assertEqual(report["moves"]["by_tier"], [])

    def test_an_unknown_entity_is_refused(self):
        with self.assertRaisesRegex(ValueError, "not found"):
            timeline.explain(self.conn, 9999, START, END)

    def test_the_window_must_be_ordered_and_bounded(self):
        for bad_start, bad_end in ((END, START), (START, START)):
            with self.subTest(start=bad_start, end=bad_end), \
                    self.assertRaisesRegex(ValueError, "start must be before end"):
                timeline.explain(self.conn, self.eid, bad_start, bad_end)

    def test_a_window_covering_more_than_ten_years_is_refused(self):
        with self.assertRaisesRegex(ValueError, "ten years"):
            timeline.explain(self.conn, self.eid, "2000-01-01T00:00:00+00:00",
                             "2026-01-01T00:00:00+00:00")

    def test_market_wide_series_can_be_excluded(self):
        self.seed_context()
        with_global = self.report(include_market_wide=True)
        without = self.report(include_market_wide=False)
        self.assertIn("stablecoin_supply", with_global["coverage"]["stored_observations"]["kinds"])
        self.assertNotIn("stablecoin_supply",
                         without["coverage"]["stored_observations"]["kinds"])

    def test_sub_daily_intervals_are_measured_on_their_own_grid(self):
        step = timedelta(hours=4)
        for index in range(24 * 40):
            moment = BASE + step * index
            registry.add_price_bar(
                conn=self.conn, instrument_key=KEY, interval_seconds=14400,
                open_time=moment.isoformat(),
                close_time=(moment + step).isoformat(),
                open="100", high="101", low="99", close="100", volume="10",
                source="binance", retrieved_at=moment.isoformat(), source_url="u")
        self.conn.commit()
        report = self.report(interval_seconds=14400)
        self.assertEqual(report["coverage"]["bar_history"]["cadence_seconds"], 14400)
        self.assertTrue(report["price"]["measured"])
        self.assertEqual(report["price"]["bars_in_window"], 42, "a week of 4h bars")

    def test_the_summary_line_does_not_overstate(self):
        self.seed_bars()
        self.seed_context()
        line = timeline.summary_line(self.report())
        self.assertIn("nothing here is marked as a cause", line)
        self.assertNotIn("cause", line.replace("marked as a cause", ""))


class CapitalTests(WindowCase):

    def test_each_number_says_what_it_is(self):
        self.seed_context()
        report = timeline.capital(self.conn, self.eid, START, END)
        self.assertTrue(report["measured_series"])
        self.assertEqual(set(report["what_each_number_is"]),
                         set(report["measured_series"]),
                         "every series is labelled, and nothing is labelled that is not a series")
        for info in report["what_each_number_is"].values():
            self.assertTrue(info["what_it_is"])
            self.assertTrue(info["is_net_flow"])
            self.assertIn(info["scope"], ("instrument", "market_wide"))

    def test_a_supply_change_is_not_called_a_flow(self):
        self.seed_context()
        report = timeline.capital(self.conn, self.eid, START, END)
        label = report["what_each_number_is"]["stablecoin_supply"]
        self.assertIn("not a net flow", label["is_net_flow"])
        self.assertIn("supply total", label["is_net_flow"])
        self.assertIn("aggregate stablecoin supply", label["what_it_is"])
        self.assertEqual(label["scope"], "market_wide")

    def test_a_bounded_unit_gets_no_percent_change(self):
        self.seed_context()
        report = timeline.capital(self.conn, self.eid, START, END)
        sentiment = report["change_over_window"]["social_sentiment"]
        self.assertIsNone(sentiment["percent_change"],
                          "a percent change on a 0-100 index is not interpretable")
        self.assertIn("bounded unit", sentiment["percent_change_note"])
        self.assertIsNotNone(sentiment["absolute_change"])
        supply = report["change_over_window"]["stablecoin_supply"]
        self.assertIsNotNone(supply["percent_change"], "a currency total is additive")

    def test_a_per_interval_delta_reports_a_net_not_a_percent(self):
        """A delta has no level to grow from, so its endpoints mean nothing.

        `order_flow` and `open_interest` are changes over each interval. The
        percent change between two arbitrary intervals is a ratio of near-zero
        numbers; the sum across the window is the real quantity. Reporting the
        first and calling it a percentage change is the exact failure the audit
        warns about, so it is refused here.
        """
        base = BASE + timedelta(days=32)
        for index, value in enumerate([100.0, -40.0, 30.0, -15.0]):
            observe(self.conn, "order_flow", f"of{index}",
                    (base + timedelta(minutes=5 * index)).isoformat(),
                    amount=value, unit="currency", currency="USDT")
        for index, value in enumerate([500.0, 250.0, 125.0]):
            observe(self.conn, "open_interest", f"oi{index}",
                    (base + timedelta(minutes=5 * index)).isoformat(),
                    amount=value, unit="currency", currency="USDT")
        report = timeline.capital(self.conn, self.eid, START, END)
        for kind in ("order_flow", "open_interest"):
            entry = report["change_over_window"][kind]
            self.assertIsNone(entry["percent_change"],
                              f"{kind} is a per-interval delta, not a level")
            self.assertIn("net_over_window", entry)
            self.assertIn("not a level", entry["percent_change_note"])
        # 100 - 40 + 30 - 15, and 500 + 250 + 125, quantised for display
        self.assertEqual(report["change_over_window"]["order_flow"]["net_over_window"],
                         "75.000000")
        self.assertEqual(report["change_over_window"]["open_interest"]["net_over_window"],
                         "875.000000")
        self.assertIn("order_flow", timeline.DELTA_KINDS)
        self.assertIn("open_interest", timeline.DELTA_KINDS)

    def test_a_single_reading_has_no_change(self):
        observe(self.conn, "stablecoin_supply", "one", (BASE + timedelta(days=31)).isoformat(),
                amount=1000, instrument="")
        report = timeline.capital(self.conn, self.eid, START, END)
        entry = report["change_over_window"]["stablecoin_supply"]
        self.assertEqual(entry["points"], 1)
        self.assertIsNone(entry["absolute_change"])
        self.assertIn("single reading", entry["note"])

    def test_unavailable_capital_measures_are_named(self):
        report = timeline.capital(self.conn, self.eid, START, END)
        joined = " ".join(report["not_available"]).lower()
        self.assertIn("exchange", joined)
        self.assertIn("equity", joined)
        self.assertIn("order-level", joined)

    def test_documented_flows_are_kept_separate_from_measurements(self):
        src = registry.upsert_entity(self.conn, "company", "Counterparty")
        registry.add_money_flow(self.conn, self.eid, src, "investment", "1000", "USD",
                                occurred_at=(BASE + timedelta(days=32)).isoformat(),
                                source_url="https://example.test/filing")
        self.conn.commit()
        report = timeline.capital(self.conn, self.eid, START, END)
        self.assertEqual(report["documented_money_flows"]["count"], 1)
        self.assertEqual(report["documented_money_flows"]["flows"][0]["amount"], "1000")
        self.assertIn("supplied", report["documented_money_flows"]["note"])

    def test_the_summary_warns_that_a_change_is_not_a_flow(self):
        self.seed_context()
        line = timeline.capital_summary(timeline.capital(self.conn, self.eid, START, END))
        self.assertIn("not a net flow", line)


class EvidenceStoreTests(WindowCase):

    def payload(self, kinds=("open_interest", "funding_rate", "long_short_account_ratio")):
        rows = []
        for index, kind in enumerate(kinds):
            rows.append({
                "id": f"binance-futures:{kind}:{1000 + index}",
                "entity_key": KEY,
                "observed_at": (BASE + timedelta(days=32, minutes=5 * index)).isoformat(),
                "available_at": (BASE + timedelta(days=32, minutes=5 * index)).isoformat(),
                "kind": kind,
                "source_url": "https://fapi.binance.com/fapi/v1/klines",
                "description": f"{kind} reading",
                "platform": "binance-futures",
                "measurement": {"value": "12345.5", "unit": "currency",
                                "currency": "USDT", "basis": "observed"},
                "mapping": None,
            })
        return {"source_url": "https://fapi.binance.com", "observations": rows}

    def test_evidence_becomes_queryable_observations(self):
        result = evidence_store.store(self.conn, self.payload(), KEY, self.eid,
                                      "binance-futures", retrieved_at=BASE.isoformat())
        self.conn.commit()
        self.assertEqual(result["stored"], 3)
        self.assertEqual(sum(result["by_kind"].values()), 3)
        window = registry.observations_in_window(
            self.conn, start=START, end=END, instrument_key=KEY)
        self.assertEqual(window["total"], 3)
        self.assertEqual(window["rows"][0]["source"], "binance-futures")

    def test_nothing_is_added_to_the_stored_number(self):
        evidence_store.store(self.conn, self.payload(), KEY, self.eid, "binance-futures")
        self.conn.commit()
        row = registry.observations_in_window(
            self.conn, start=START, end=END, instrument_key=KEY)["rows"][0]
        self.assertEqual(row["amount"], "12345.5")
        self.assertEqual(row["unit"], "currency")
        self.assertEqual(row["currency"], "USDT")
        self.assertEqual(row["actor_key"], "", "no counterparty is invented")
        self.assertEqual(row["counterparty_key"], "")
        self.assertEqual(row["action"], "", "no action is invented")
        self.assertEqual(row["reason_basis"], "unknown")
        payload = json.loads(row["evidence"])
        self.assertIn("no counterparty", payload["conversion_note"])

    def test_an_unknown_kind_is_refused_rather_than_invented(self):
        payload = self.payload()
        payload["observations"][0]["kind"] = "not_a_real_kind"
        converted, skipped = evidence_store.convert(payload, KEY, self.eid, "test")
        self.assertEqual(len(converted), 2)
        self.assertEqual(skipped["unknown_kind"], 1)

    def test_a_record_without_a_measurement_is_skipped(self):
        payload = self.payload()
        payload["observations"][0]["measurement"] = None
        converted, skipped = evidence_store.convert(payload, KEY, self.eid, "test")
        self.assertEqual(skipped["no_measurement"], 1)
        self.assertEqual(len(converted), 2)

    def test_storing_twice_does_not_duplicate(self):
        first = evidence_store.store(self.conn, self.payload(), KEY, self.eid, "bf")
        self.conn.commit()
        second = evidence_store.store(self.conn, self.payload(), KEY, self.eid, "bf")
        self.conn.commit()
        self.assertEqual(first["stored"], 3)
        self.assertEqual(second["stored"], 3, "the same external ids refine, not add")
        window = registry.observations_in_window(
            self.conn, start=START, end=END, instrument_key=KEY)
        self.assertEqual(window["total"], 3)

    def test_a_malformed_payload_is_refused(self):
        for bad in ({}, {"observations": {}}, {"observations": None}, "not an object"):
            with self.subTest(payload=bad), self.assertRaises(ValueError):
                evidence_store.convert(bad, KEY, self.eid, "test")

    def test_a_malformed_row_is_skipped_and_counted(self):
        """A bad row is counted, not refused: one unusable record must not
        discard the rest of a document."""
        payload = self.payload()
        payload["observations"].insert(0, "not an object")
        converted, skipped = evidence_store.convert(payload, KEY, self.eid, "test")
        self.assertEqual(len(converted), 3)
        self.assertEqual(skipped["malformed"], 1)

    def test_the_result_can_be_found_by_a_window_query(self):
        evidence_store.store(self.conn, self.payload(), KEY, self.eid, "bf")
        self.conn.commit()
        report = timeline.explain(self.conn, self.eid, START, END)
        families = report["coverage"]["stored_observations"]["by_family"]
        self.assertIn("market_structure", families)
        self.assertIn("stored_observations",
                      {item["channel"] for item in report["coverage"]["channels"]
                       if item["status"] == "present"})
        kinds = {row["kind"] for row in report["records"]["inside_window"]}
        self.assertIn("open_interest", kinds)


class RegistryWindowTests(WindowCase):

    def test_a_window_query_reports_truncation_rather_than_hiding_it(self):
        for index in range(5):
            observe(self.conn, "stablecoin_supply", f"t{index}",
                    (BASE + timedelta(days=32, minutes=index)).isoformat(),
                    amount=100, instrument="")
        result = registry.observations_in_window(
            self.conn, start=START, end=END, instrument_key=KEY, limit=2)
        self.assertEqual(result["total"], 5)
        self.assertEqual(len(result["rows"]), 2)
        self.assertTrue(result["truncated"],
                        "a prefix must never be presented as the whole window")

    def test_coverage_lists_absent_kinds_not_just_present_ones(self):
        observe(self.conn, "stablecoin_supply", "c1", (BASE + timedelta(days=32)).isoformat(),
                amount=100, instrument="")
        observe(self.conn, "regulatory_action", "c2", (BASE + timedelta(days=32)).isoformat(),
                amount=None, instrument="")
        coverage = {row["kind"]: row for row in registry.observation_coverage(
            self.conn, start=START, end=END, instrument_key=KEY)}
        self.assertEqual(sorted(coverage), ["regulatory_action", "stablecoin_supply"])
        self.assertEqual(coverage["stablecoin_supply"]["measured"], 1)
        self.assertEqual(coverage["regulatory_action"]["measured"], 0,
                         "a record with no measurement is still coverage")
        never = registry.observation_kinds_ever(self.conn)
        self.assertIn("stablecoin_supply", never)
        self.assertNotIn("liquidation", never,
                         "a kind that was never stored is absent, not zero")

    def test_the_window_must_be_ordered(self):
        with self.assertRaisesRegex(ValueError, "start must be before end"):
            registry.observations_in_window(self.conn, start=END, end=START)

    def test_the_bar_span_reports_cadence_and_gaps(self):
        self.seed_bars(days=10)
        self.conn.execute("DELETE FROM price_bars WHERE open_time=?",
                          ((BASE + timedelta(days=5)).isoformat(),))
        self.conn.commit()
        span = registry.stored_bar_span(self.conn, KEY, DAY)
        self.assertEqual(span["bars"], 9)
        self.assertEqual(span["cadence_seconds"], DAY)
        self.assertEqual(span["gaps"], 1)
        self.assertFalse(span["usable"])


if __name__ == "__main__":
    unittest.main()
