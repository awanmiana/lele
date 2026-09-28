"""Offline tests for measured crypto context and the market-wide stored scope.

No network: provider payloads are stubbed, and every assertion pins a value a
wrong parse or a wrong scope rule would change.
"""
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, UTC

from lele.analysis import events, signals
from lele.core import clock, registry
from lele.fetchers import crypto_context as ctx
from lele.fetchers import political

KEY = "binance:BTCUSDT"
NOW = datetime(2026, 9, 26, 12, tzinfo=UTC)


def sentiment_entry(day, value, classification):
    return {"value": str(value), "value_classification": classification,
            "timestamp": str(int(day.replace(hour=0, minute=0, second=0, microsecond=0)
                                   .timestamp()))}


def supply_entry(day, circulating, minted=None, bridged=None):
    return {"date": str(int(day.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())),
            "totalCirculating": {"peggedUSD": circulating},
            "totalCirculatingUSD": {"peggedUSD": circulating, "peggedEUR": 5},
            "totalMintedUSD": {"peggedUSD": minted} if minted is not None else {},
            "totalBridgedToUSD": {"peggedUSD": bridged} if bridged is not None else {}}


class ContextCase(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        registry._initialize(self.conn)
        self.addCleanup(self.conn.close)
        self.original_get = ctx.HTTPClient

    def stub(self, payload):
        class Fake:
            warnings = []
            retrieved_at = NOW.isoformat()
            cache_dir = None
            ttl = 0

            def __init__(self, *args, **kwargs):
                pass

            def get_json(self, url):
                self.url = url
                return payload
        ctx.HTTPClient = Fake
        self.addCleanup(lambda: setattr(ctx, "HTTPClient", self.original_get))


class SentimentTests(ContextCase):

    def test_index_entries_become_market_wide_sentiment(self):
        self.stub({"data": [sentiment_entry(NOW - timedelta(days=1), 71, "Greed"),
                            sentiment_entry(NOW - timedelta(days=2), 12, "Extreme Fear")]})
        payload, report = ctx.fetch_sentiment(self.conn, limit=10)
        self.assertEqual(report["stored"], 2)
        row = payload["observations"][0]
        self.assertEqual(row["kind"], "social_sentiment")
        self.assertEqual(row["instrument_key"], "", "a market-wide index is not bound to an asset")
        self.assertEqual(row["amount"], "12", "the payload is returned oldest first")
        self.assertEqual(row["unit"], "index_0_100")
        self.assertEqual(row["basis"], "estimated", "a composite index is not a raw observation")
        self.assertIn("Extreme Fear", row["description"])
        moment = datetime.fromisoformat(row["observed_at"])
        self.assertEqual(datetime.fromisoformat(row["available_at"]), moment + timedelta(days=1),
                         "a daily value must be available no earlier than the next day")
        evidence = json.loads(row["evidence"])
        self.assertEqual(evidence["index"], "crypto_fear_greed")
        self.assertEqual(evidence["value"], 12)
        self.assertIn("not a cause", " ".join(report["limitations"]).lower()
                      if " ".join(report["limitations"]) else "not a cause")

    def test_invalid_entries_are_skipped_and_counted(self):
        self.stub({"data": [sentiment_entry(NOW, 50, "Neutral"),
                            {"value": "50", "value_classification": "Neutral"},
                            {"value": "50", "value_classification": "Euphoria",
                             "timestamp": str(int(NOW.timestamp()))},
                            {"value": "500", "value_classification": "Greed",
                             "timestamp": str(int(NOW.timestamp()))},
                            "not an object"]})
        payload, report = ctx.fetch_sentiment(self.conn, limit=10)
        self.assertEqual(report["stored"], 1)
        self.assertEqual(report["skipped"]["invalid"], 4)

    def test_future_entries_are_refused(self):
        ahead = NOW + timedelta(days=2)
        with self.assertRaises(ValueError):
            ctx._sentiment_observation(sentiment_entry(ahead, 50, "Neutral"), NOW.isoformat(),
                                       NOW)

    def test_the_clock_is_the_only_source_of_now(self):
        """A frozen clock must decide, not the host's wall clock.

        The provider read and the refusal have to be judged against the same
        instant, or this test means something different every day it runs.
        """
        inside = NOW - timedelta(days=1)
        with clock.freeze(NOW):
            ctx._sentiment_observation(sentiment_entry(inside, 50, "Neutral"),
                                       NOW.isoformat())
            with self.assertRaises(ValueError):
                ctx._stablecoin_observation(
                    {"date": str(int((NOW + timedelta(days=1)).timestamp())),
                     "totalCirculatingUSD": {"peggedUSD": 1}}, NOW)
        with clock.freeze(NOW - timedelta(days=400)):
            with self.assertRaises(ValueError):
                ctx._sentiment_observation(sentiment_entry(inside, 50, "Neutral"),
                                           NOW.isoformat())

    def test_request_validation(self):
        for kwargs in ({"limit": 0}, {"limit": ctx.MAX_SENTIMENT_DAYS + 1},
                       {"end": "2030-01-01T00:00:00+00:00"}):
            arguments = {"limit": 10, "end": None, "now": NOW}
            arguments.update(kwargs)
            with self.assertRaises(ValueError):
                ctx.validate_sentiment(**arguments)


class StablecoinTests(ContextCase):

    def test_peg_currencies_are_summed_and_usd_part_kept(self):
        self.stub([supply_entry(NOW - timedelta(days=1), 100.0),
                   supply_entry(NOW - timedelta(days=2), 90.0)])
        payload, report = ctx.fetch_stablecoin_supply(self.conn, limit=10)
        self.assertEqual(report["stored"], 2)
        row = payload["observations"][0]
        self.assertEqual(row["kind"], "stablecoin_supply")
        self.assertEqual(row["instrument_key"], "")
        self.assertEqual(row["amount"], "95.00", "peggedUSD 90 plus peggedEUR 5")
        self.assertEqual(row["unit"], "usd")
        self.assertEqual(row["currency"], "USD")
        self.assertEqual(row["basis"], "observed")
        evidence = json.loads(row["evidence"])
        self.assertEqual(evidence["usd_pegged_circulating_usd"], "90.00")
        self.assertEqual(evidence["peg_currencies_counted"], 2)

    def test_absent_mint_and_bridge_objects_do_not_abort_the_series(self):
        self.stub([{"date": str(int((NOW - timedelta(days=1)).timestamp())),
                    "totalCirculatingUSD": {"peggedUSD": 10}}])
        payload, report = ctx.fetch_stablecoin_supply(self.conn, limit=10)
        self.assertEqual(report["stored"], 1)
        evidence = json.loads(payload["observations"][0]["evidence"])
        self.assertEqual(evidence["total_minted_usd"], "0.00")
        self.assertEqual(evidence["total_bridged_usd"], "0.00")

    def test_malformed_supply_entries_are_skipped(self):
        self.stub([supply_entry(NOW, 5.0), {"date": "not-a-date"},
                   {"date": str(int(NOW.timestamp()))},
                   {"date": str(int(NOW.timestamp())), "totalCirculatingUSD": {}}])
        payload, report = ctx.fetch_stablecoin_supply(self.conn, limit=10)
        self.assertEqual(report["stored"], 1)
        self.assertEqual(report["skipped"]["invalid"], 3)

    def test_empty_response_is_refused_rather_than_reported_as_no_supply(self):
        self.stub([])
        with self.assertRaises(ValueError):
            ctx.fetch_stablecoin_supply(self.conn, limit=10)

    def test_request_validation(self):
        for kwargs in ({"limit": 0}, {"limit": ctx.MAX_STABLECOIN_DAYS + 1},
                       {"end": "2030-01-01T00:00:00+00:00"}):
            arguments = {"limit": 10, "end": None, "now": NOW}
            arguments.update(kwargs)
            with self.assertRaises(ValueError):
                ctx.validate_stablecoins(**arguments)


class MarketWideScopeTests(ContextCase):

    def _add(self, kind, source, external_id, amount="1", unit="usd", occurred="2026-09-20T00:00:00+00:00"):
        registry.add_observation(
            conn=self.conn, source=source, external_id=external_id, kind=kind,
            description=f"{kind} record", instrument_key="",
            amount=amount or None, unit=unit if amount else "",
            currency="USD", basis="observed", occurred_at=occurred, observed_at=occurred,
            available_at=occurred, source_url="https://example.com/a")

    def test_market_wide_context_reaches_any_instrument(self):
        self._add("stablecoin_supply", "defillama", "s1", amount="313199000247.05")
        self._add("social_sentiment", "alternative-me", "e1", amount="71", unit="index_0_100")
        self._add("regulatory_action", "federal-register", "p1", amount="")
        self.conn.commit()
        found, coverage = signals.stored(
            self.conn, KEY, datetime(2026, 9, 19, tzinfo=UTC), NOW)
        families = {item["family"] for item in found}
        self.assertIn("money_movement", families, "stablecoin supply is money movement")
        self.assertIn("sentiment_emotion", families)
        self.assertIn("policy_decision", families)
        self.assertEqual(coverage["market_wide_signals"], 3)
        self.assertEqual(coverage["instrument_signals"], 0)
        supply = next(item for item in found if item["kind"] == "stablecoin_supply")
        self.assertEqual(supply["magnitude"], "313199000247.05")
        self.assertEqual(supply["detail"]["scope"], "market_wide")

    def test_a_global_routed_flow_about_two_named_organisations_is_excluded(self):
        self._add("fund_flow", "usaspending", "f1", amount="1000")
        self.conn.commit()
        found, coverage = signals.stored(
            self.conn, KEY, datetime(2026, 9, 19, tzinfo=UTC), NOW)
        self.assertEqual(found, [], "a flow between named parties is not market context")
        self.assertEqual(coverage["excluded_unmapped_kind"], 1)
        self.assertNotIn("fund_flow", signals.GLOBAL_CONTEXT_KINDS)

    def test_instrument_bound_rows_still_win_over_market_wide(self):
        self._add("stablecoin_supply", "defillama", "s2", amount="50")
        registry.add_observation(
            conn=self.conn, source="cftc-cot", external_id="c1", kind="cot_positioning",
            description="positioning", instrument_key=KEY, amount="703", unit="contract",
            occurred_at="2026-09-20T00:00:00+00:00",
            observed_at="2026-09-20T00:00:00+00:00",
            available_at="2026-09-20T00:00:00+00:00", source_url="https://example.com/c")
        self.conn.commit()
        found, coverage = signals.stored(
            self.conn, KEY, datetime(2026, 9, 19, tzinfo=UTC), NOW)
        self.assertEqual(coverage["instrument_signals"], 1)
        self.assertEqual(coverage["market_wide_signals"], 1)
        scoped = {item["detail"]["scope"] for item in found}
        self.assertEqual(scoped, {"instrument", "market_wide"})

    def test_every_global_kind_maps_to_a_known_family(self):
        for kind in signals.GLOBAL_CONTEXT_KINDS:
            self.assertIn(kind, signals.OBSERVATION_FAMILY, kind)
            self.assertIn(kind, events.KINDS, kind)


class FederalRegisterEvidenceTests(unittest.TestCase):
    """The stored evidence field is bounded text; verbose documents must not abort a fetch."""

    def _record(self, **overrides):
        record = {"document_number": "2026-19723", "title": "A Rule About Something",
                  "type": "Rule", "publication_date": "2026-09-25",
                  "html_url": "https://www.federalregister.gov/documents/2026/09/25/x",
                  "abstract": "word " * 400,
                  "agencies": [{"name": "A Very Long Agency Name Indeed For Testing " * 3}] * 12}
        record.update(overrides)
        return record

    def test_a_verbose_document_still_produces_bounded_evidence(self):
        row = political._observation(self._record(), "2026-09-26T00:00:00+00:00")
        self.assertLessEqual(len(row["evidence"]), political.EVIDENCE_LIMIT)
        payload = json.loads(row["evidence"])
        self.assertEqual(payload["document_number"], "2026-19723")
        self.assertTrue(payload["bounded_fields"]["dropped"],
                        "a reduced document must say what it dropped")
        self.assertIn("title", payload, "identity survives the reduction")

    def test_a_short_document_keeps_its_abstract_and_agencies(self):
        row = political._observation(
            self._record(abstract="A short abstract.", agencies=[{"name": "Agency A"}]),
            "2026-09-26T00:00:00+00:00")
        payload = json.loads(row["evidence"])
        self.assertEqual(payload["abstract"], "A short abstract.")
        self.assertEqual(payload["agencies"], ["Agency A"])
        self.assertEqual(payload["bounded_fields"]["dropped"], [])

    def test_document_url_is_carried_by_source_url_not_duplicated(self):
        row = political._observation(self._record(), "2026-09-26T00:00:00+00:00")
        payload = json.loads(row["evidence"])
        self.assertNotIn("html_url", payload)
        self.assertTrue(row["source_url"].startswith("https://"))

    def test_a_document_without_a_usable_date_or_title_is_skipped(self):
        for overrides in ({"publication_date": "not-a-date"}, {"publication_date": ""},
                          {"title": "  "}, {"type": None, "title": ""}):
            self.assertIsNone(
                political._observation(self._record(**overrides), "2026-09-26T00:00:00+00:00"),
                f"an unusable document must be skipped, not stored: {overrides}")


if __name__ == "__main__":
    unittest.main()
