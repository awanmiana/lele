"""Offline tests for price history, move detection and the pre-move signal scan.

No network: bars are written straight into the registry and the news channel is
exercised through a stubbed fetcher. Each test pins a value that a wrong
implementation would change.
"""
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, UTC
from decimal import Decimal
from pathlib import Path

from lele.analysis import causes, moves, signals
from lele.core import registry
from lele.fetchers import history, news

BASE = datetime(2026, 1, 1, tzinfo=UTC)
KEY = "binance:BTCUSDT"


def _recent_end():
    """A five-minute boundary inside the news provider's reach."""
    now = datetime.now(UTC)
    return (now - timedelta(hours=2)).replace(minute=(now.minute // 5) * 5, second=0,
                                            microsecond=0)


def article(identifier, seen, title, domain="example.com", language="English"):
    return {"url": f"https://{domain}/{identifier}", "url_mobile": "",
            "title": title, "seendate": seen.strftime("%Y%m%dT%H%M%SZ"),
            "socialimage": "", "domain": domain, "language": language,
            "sourcecountry": "US"}


def bars(conn, closes, interval_seconds=86400, start=BASE, volume="100"):
    step = timedelta(seconds=interval_seconds)
    for index, close in enumerate(closes):
        moment = start + step * index
        registry.add_price_bar(
            conn, instrument_key=KEY, interval_seconds=interval_seconds,
            open_time=moment.isoformat(), close_time=(moment + step).isoformat(),
            open=str(close), high=str(close * 1.01), low=str(close * 0.99), close=str(close),
            volume=volume, source="binance", retrieved_at=moment.isoformat(),
            source_url="https://api.binance.com/api/v3/klines")
    conn.commit()


class RegistryCase(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.conn = sqlite3.connect(Path(directory.name) / "registry.db")
        self.conn.row_factory = sqlite3.Row
        registry._initialize(self.conn)
        self.addCleanup(self.conn.close)
        self.entity_id = registry.upsert_entity(conn=self.conn, kind="instrument",
                                                name="Bitcoin", key=KEY)


class HistoryRequestTests(unittest.TestCase):

    def test_request_validation_rejects_bad_inputs(self):
        now = datetime(2026, 9, 26, tzinfo=UTC)
        for kwargs in ({"symbol": "DOGEUSDT"}, {"interval": "1m"}, {"limit": 1},
                       {"limit": history.MAX_BARS + 1}, {"pages": 0},
                       {"pages": history.MAX_PAGES + 1}, {"end": "2030-01-01T00:00:00+00:00"}):
            arguments = {"symbol": "BTCUSDT", "interval": "1d", "limit": 100, "pages": 1,
                         "end": None, "now": now}
            arguments.update(kwargs)
            with self.assertRaises(ValueError):
                history.validate_request(**arguments)
        self.assertIsNone(history.validate_request(symbol="BTCUSDT", interval="1d", limit=100,
                                                   pages=1, end=None, now=now))

    def test_bar_parsing_accepts_provider_text_numbers(self):
        row = [1767225600000, "100.50000000", "110.00000000", "99.00000000", "108.00000000",
               "12.5", 1767225600000 + 86400000 - 1, "1300.0", 42, "6.0", "640.0", "0"]
        bar = history._bar(row, 86400)
        self.assertEqual(bar["open"], "100.5")
        self.assertEqual(bar["close"], "108")
        self.assertEqual(bar["volume"], "12.5")
        self.assertEqual(bar["trades"], 42)
        self.assertEqual(bar["open_time"], "2026-01-01T00:00:00+00:00")

    def test_bar_parsing_rejects_off_grid_and_malformed_rows(self):
        base = [1767225600000, "100", "110", "99", "108", "1",
                1767225600000 + 86400000 - 1, "1", 1, "0", "0", "0"]
        for mutate in (lambda r: r.pop(),
                       lambda r: r.__setitem__(0, 1767225600001),
                       lambda r: r.__setitem__(6, 1),
                       lambda r: r.__setitem__(1, "abc"),
                       lambda r: r.__setitem__(8, -1)):
            row = list(base)
            mutate(row)
            with self.assertRaises(ValueError):
                history._bar(row, 86400)


class PriceBarStoreTests(RegistryCase):

    def test_bars_are_idempotent_and_validated(self):
        first = registry.add_price_bar(
            conn=self.conn, instrument_key=KEY, interval_seconds=86400,
            open_time="2026-01-01T00:00:00+00:00", close_time="2026-01-02T00:00:00+00:00",
            open="100", high="110", low="90", close="105", volume="5", source="binance",
            retrieved_at="2026-01-02T00:00:00+00:00")
        again = registry.add_price_bar(
            conn=self.conn, instrument_key=KEY, interval_seconds=86400,
            open_time="2026-01-01T00:00:00+00:00", close_time="2026-01-02T00:00:00+00:00",
            open="100", high="110", low="90", close="106", volume="6", source="binance",
            retrieved_at="2026-01-02T01:00:00+00:00")
        self.assertEqual(first, again)
        stored = registry.list_price_bars(self.conn, KEY, 86400)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["close"], "106")
        for kwargs in ({"high": "10"}, {"open": "200"}, {"close": "0"}, {"low": "-1"}):
            arguments = {"conn": self.conn, "instrument_key": KEY, "interval_seconds": 86400,
                         "open_time": "2026-01-03T00:00:00+00:00",
                         "close_time": "2026-01-04T00:00:00+00:00", "open": "100",
                         "high": "110", "low": "90", "close": "105", "volume": "5",
                         "source": "binance", "retrieved_at": "2026-01-04T00:00:00+00:00"}
            arguments.update(kwargs)
            with self.assertRaises(ValueError):
                registry.add_price_bar(**arguments)


class MoveDetectionTests(RegistryCase):

    def test_a_crash_is_not_counted_once_per_bar(self):
        closes = [100.0] * 40
        for index in range(40, 70):
            closes.append(closes[-1] * 0.93)
        bars(self.conn, closes)
        result = moves.detect(self.conn, KEY, 86400, move_hours=24, thresholds=(5, 10),
                              baseline_bars=30, limit=100, store=False)
        down = [item for item in result["moves"] if item["direction"] == "down"]
        falling_bars = 30
        self.assertEqual(result["coverage"]["candidates"], falling_bars,
                         "every qualifying bar is a candidate before selection")
        self.assertLessEqual(len(down), (falling_bars + 1) // 2,
                             "retained windows may not share a bar")
        self.assertGreaterEqual(len(down), 1)
        for item in down:
            self.assertIn("p5", item["tiers"])
            self.assertLess(Decimal(item["change_percent"]), Decimal(-5))
        self.assertEqual(result["summary"]["by_tier_direction"]["p5"]["down"], len(down))
        self.assertEqual(result["summary"]["by_tier_direction"]["p10"]["total"], 0)

    def test_cooldown_collapses_one_trend_into_one_move(self):
        closes = [100.0] * 40
        for index in range(40, 70):
            closes.append(closes[-1] * 0.93)
        bars(self.conn, closes)
        loose = moves.detect(self.conn, KEY, 86400, move_hours=24, thresholds=(5, 10),
                             baseline_bars=30, limit=100, store=False)
        tight = moves.detect(self.conn, KEY, 86400, move_hours=24, thresholds=(5, 10),
                             baseline_bars=30, limit=100, store=False,
                             cooldown_bars=10)
        loose_down = sum(1 for item in loose["moves"] if item["direction"] == "down")
        tight_down = sum(1 for item in tight["moves"] if item["direction"] == "down")
        self.assertGreater(loose_down, 1)
        self.assertEqual(tight_down, 1, "a cooldown must collapse one trend into one move")

    def test_moves_never_overlap(self):
        closes = [100.0]
        for index in range(1, 200):
            closes.append(closes[-1] * (1.06 if (index // 5) % 2 == 0 else 0.95))
        bars(self.conn, closes)
        result = moves.detect(self.conn, KEY, 86400, move_hours=24, thresholds=(4, 8),
                              baseline_bars=30, limit=100, store=False)
        self.assertGreater(len(result["moves"]), 2)
        windows = sorted((item["start_time"], item["end_time"]) for item in result["moves"])
        for (_, first_end), (second_start, _) in zip(windows, windows[1:]):
            self.assertLessEqual(first_end, second_start,
                                 "retained move windows must not overlap")

    def test_tier_thresholds_and_validation(self):
        closes = [100.0] * 40 + [106.0] * 3 + [106.0] * 3
        bars(self.conn, closes)
        medium_only = moves.detect(self.conn, KEY, 86400, move_hours=24, thresholds=(5, 50), baseline_bars=30, limit=10, store=False)
        self.assertTrue(medium_only["moves"])
        for item in medium_only["moves"]:
            self.assertIn("p5", item["tiers"],
                          "a 6% move clears a 5% threshold")
            self.assertNotIn("p50", item["tiers"],
                             "the same move must not clear the higher threshold")
        for kwargs in ({"move_hours": 0}, {"thresholds": (0, 5)}, {"thresholds": (5, 1)},
                       {"thresholds": ()}, {"thresholds": (3, 3)}, {"baseline_bars": 5},
                       {"limit": 0}, {"store": "yes"}):
            arguments = {"move_hours": 24, "thresholds": (5, 10), "baseline_bars": 30,
                         "limit": 10, "store": False}
            arguments.update(kwargs)
            with self.assertRaises(ValueError):
                moves.validate(**arguments)

    def test_baseline_uses_only_earlier_returns(self):
        closes = [100.0] * 60
        for index in range(30):
            closes.append(closes[-1] * 1.001)
        bars(self.conn, closes)
        quiet = moves.detect(self.conn, KEY, 86400, move_hours=24, thresholds=(2, 99), baseline_bars=40, limit=10, store=False)
        self.assertEqual(quiet["moves"], [])
        self.assertEqual(quiet["summary"]["status"], "ok")

    def test_insufficient_history_is_reported_not_guessed(self):
        bars(self.conn, [100.0, 101.0, 100.5])
        result = moves.detect(self.conn, KEY, 86400, store=False)
        self.assertEqual(result["summary"]["status"], "insufficient_bars")
        self.assertEqual(result["moves"], [])

    def test_detect_stores_and_reuses_rows(self):
        closes = [100.0] * 40 + [120.0]
        bars(self.conn, closes)
        first = moves.detect(self.conn, KEY, 86400, move_hours=24, thresholds=(5, 10),
                             baseline_bars=30, limit=10, store=True)
        self.assertGreaterEqual(first["stored"], 1)
        second = moves.detect(self.conn, KEY, 86400, move_hours=24, thresholds=(5, 10),
                              baseline_bars=30, limit=10, store=True)
        stored = registry.list_move_events(self.conn, KEY, 86400, 24, limit=100)
        self.assertEqual(len(stored), second["stored"],
                         "re-running must refine rows, not duplicate them")

    def test_current_uses_the_same_rule_as_detect(self):
        closes = [100.0] * 40 + [130.0]
        bars(self.conn, closes)
        current = moves.current(self.conn, KEY, 86400, 24)
        self.assertEqual(current["status"], "ok")
        self.assertEqual(current["direction"], "up")
        self.assertEqual(current["change_percent"], "30.0000")
        self.assertEqual(Decimal(current["start_price"]), Decimal(100))
        self.assertEqual(Decimal(current["end_price"]), Decimal(130))


class ClassifierTests(unittest.TestCase):

    def test_known_headlines_map_to_expected_categories(self):
        cases = {
            "SEC sues Binance under new securities law": ("regulation_legal",
                                                           "exchange_incident"),
            "Fed signals rate cut as inflation cools": ("macro_rates",),
            "Spot bitcoin ETF posts largest inflows of the year": ("etf_institutional",),
            "Whale transfers 50000 BTC to an exchange, on-chain data shows": ("whale_onchain",),
            "Local bakery wins a prize": (),
        }
        for headline, expected in cases.items():
            found, _ = causes.classify(headline)
            for category in expected:
                self.assertIn(category, found, headline)
            if not expected:
                self.assertEqual(found, [], headline)

    def test_classification_is_deterministic_and_bounded(self):
        headline = "SEC investigates exchange after whale transfers and Fed rate cut"
        first, matches = causes.classify(headline)
        second, _ = causes.classify(headline)
        self.assertEqual(first, second)
        self.assertGreater(len(first), 1, "one headline may raise several candidate reasons")
        self.assertEqual(set(first), set(matches))
        self.assertEqual(causes.classify("")[0], [])
        self.assertEqual(causes.classify(None)[0], [])

    def test_every_category_maps_to_a_family(self):
        for category in causes.categories():
            self.assertIn(category, signals.CATEGORY_FAMILY, category)
        for family in signals.families():
            self.assertIsInstance(family, str)


class StoredChannelTests(RegistryCase):

    def test_stored_observations_become_family_signals(self):
        registry.add_observation(
            conn=self.conn, source="test", external_id="flow-1", kind="etf_flow",
            description="spot ETF net inflow", instrument_key=KEY, amount="1200", unit="usd",
            currency="USD", occurred_at="2026-02-01T00:00:00+00:00",
            observed_at="2026-02-01T00:00:00+00:00",
            available_at="2026-02-01T00:00:00+00:00", source_url="https://example.com/a")
        registry.add_observation(
            conn=self.conn, source="test", external_id="pol-1", kind="regulatory_action",
            description="SEC adopts new custody rule", instrument_key=KEY,
            occurred_at="2026-02-02T00:00:00+00:00",
            observed_at="2026-02-02T00:00:00+00:00",
            available_at="2026-02-02T00:00:00+00:00", source_url="https://example.com/b")
        self.conn.commit()
        found, coverage = signals.stored(
            self.conn, KEY, datetime(2026, 1, 31, tzinfo=UTC),
            datetime(2026, 2, 3, tzinfo=UTC))
        families = {item["family"] for item in found}
        self.assertIn("money_movement", families)
        self.assertIn("policy_decision", families)
        self.assertEqual(coverage["signals"], 2)
        flow = next(item for item in found if item["kind"] == "etf_flow")
        self.assertEqual(flow["magnitude"], "1200")
        self.assertEqual(flow["unit"], "usd")

    def test_evidence_not_yet_available_is_counted_not_hidden(self):
        registry.add_observation(
            conn=self.conn, source="test", external_id="late-1", kind="etf_flow",
            description="flow", instrument_key=KEY, amount="5", unit="usd",
            occurred_at="2026-02-01T00:00:00+00:00",
            observed_at="2026-02-01T00:00:00+00:00",
            available_at="2026-03-01T00:00:00+00:00", source_url="https://example.com/a")
        self.conn.commit()
        found, coverage = signals.stored(
            self.conn, KEY, datetime(2026, 1, 31, tzinfo=UTC),
            datetime(2026, 2, 3, tzinfo=UTC))
        self.assertEqual(found, [], "a later retrieval must not enter an earlier window")
        self.assertEqual(coverage["excluded_available_after_window"], 1)


class MarketChannelTests(RegistryCase):

    def test_volume_spike_is_measured_against_the_trailing_baseline(self):
        for index in range(100):
            moment = BASE + timedelta(days=index)
            registry.add_price_bar(
                conn=self.conn, instrument_key=KEY, interval_seconds=86400,
                open_time=moment.isoformat(),
                close_time=(moment + timedelta(days=1)).isoformat(), open="100", high="101",
                low="99", close="100", volume=str(90 + index % 7), source="binance",
                retrieved_at=moment.isoformat())
        self.conn.commit()
        registry.add_price_bar(
            conn=self.conn, instrument_key=KEY, interval_seconds=86400,
            open_time=(BASE + timedelta(days=100)).isoformat(),
            close_time=(BASE + timedelta(days=101)).isoformat(), open="100", high="110",
            low="90", close="100", volume="10000", source="binance",
            retrieved_at=(BASE + timedelta(days=101)).isoformat())
        self.conn.commit()
        start = BASE + timedelta(days=100)
        found, coverage = signals.market(self.conn, KEY, 86400, start, start + timedelta(days=1),
                                         baseline_bars=60)
        kinds = {item["kind"] for item in found}
        self.assertIn("volume_zscore", kinds)
        z = next(Decimal(item["magnitude"]) for item in found if item["kind"] == "volume_zscore")
        self.assertGreater(z, Decimal(5), "a 100x volume jump must read as a large z-score")
        self.assertEqual(coverage["status"], "ok")
        self.assertEqual(coverage["volume_zscore_status"], "ok")

    def test_flat_volume_baseline_is_reported_not_scored(self):
        bars(self.conn, [100.0] * 100, volume="100")
        start = BASE + timedelta(days=99)
        found, coverage = signals.market(self.conn, KEY, 86400, start, start + timedelta(days=1),
                                         baseline_bars=60)
        self.assertFalse(any(item["kind"] == "volume_zscore" for item in found))
        self.assertEqual(coverage["volume_zscore_status"], "zero_variance_baseline")


class NewsChannelTests(RegistryCase):

    def setUp(self):
        super().setUp()
        self.original = news.fetch_news
        self.calls = []

    def tearDown(self):
        news.fetch_news = self.original

    def stub(self, rows_by_end):
        def fake(conn, entity_id, topic, hours=24, limit=100, end=None, language=None,
                 cache_seconds=0):
            self.calls.append({"topic": topic, "end": end, "hours": hours, "limit": limit,
                               "language": language, "cache_seconds": cache_seconds})
            rows = rows_by_end.get(end, [])
            payload = {"observations": [
                {"id": f"gdelt:news_event:{row['url']}", "entity_key": KEY,
                 "observed_at": row["seendate"], "available_at": row["seendate"],
                 "kind": "news_event", "source_url": row["url"],
                 "description": row["title"], "platform": row["domain"], "industry": None,
                 "location": "US", "measurement": None, "mapping": None}
                for row in rows]}
            report = {"source": "gdelt", "source_url": "https://api.gdeltproject.org/x",
                      "retrieved_at": "2026-02-01T00:00:00+00:00"}
            return payload, report
        news.fetch_news = fake

    def test_headlines_are_classified_scored_and_familied(self):
        end = _recent_end()
        start = end - timedelta(hours=24)
        self.stub({end.isoformat(): [
            article("a", start + timedelta(hours=6),
                    "SEC sues exchange in crackdown", domain="reuters.com"),
            article("b", start + timedelta(hours=7),
                    "Whale dumps coins, crash and plunge sparks panic", domain="cointelegraph.com"),
        ]})
        found, coverage = signals.news_signals(
            self.conn, self.entity_id, "bitcoin", start, end, None, 50)
        self.assertEqual(len(found), 2)
        families = {item["family"] for item in found}
        self.assertIn("policy_decision", families)
        self.assertIn("sentiment_emotion", families)
        self.assertEqual(coverage["articles_in_window"], 2)
        self.assertEqual(coverage["sentiment_scored"], 2,
                         "a financial headline must still receive a sentiment score")
        scored = [item for item in found if item["magnitude"] is not None]
        self.assertTrue(scored)
        self.assertTrue(all(item["detail"]["sentiment_method"] for item in scored))

    def test_articles_outside_the_window_are_dropped(self):
        end = _recent_end()
        start = end - timedelta(hours=24)
        self.stub({end.isoformat(): [
            article("late", end + timedelta(hours=6), "after the window"),
        ]})
        found, coverage = signals.news_signals(
            self.conn, self.entity_id, "bitcoin", start, end, None, 50)
        self.assertEqual(found, [])
        self.assertEqual(coverage["status"], "empty_recent_window")
        self.assertIn("lags its own live coverage", coverage["status_note"])

    def test_empty_old_window_is_plain_empty(self):
        end = _recent_end() - timedelta(days=30)
        self.stub({end.isoformat(): []})
        found, coverage = signals.news_signals(
            self.conn, self.entity_id, "bitcoin", end - timedelta(hours=24), end, None, 50)
        self.assertEqual(found, [])
        self.assertEqual(coverage["status"], "empty")

    def test_window_older_than_provider_reach_is_not_requested(self):
        self.stub({})
        found, coverage = signals.news_signals(
            self.conn, self.entity_id, "bitcoin", BASE, BASE + timedelta(hours=24), None, 50)
        self.assertEqual(found, [])
        self.assertEqual(coverage["status"], "outside_provider_reach")
        self.assertEqual(self.calls, [], "an unreachable window must not hit the provider")

    def test_provider_failure_is_reported_not_raised(self):
        def boom(*args, **kwargs):
            raise RuntimeError("provider down")
        news.fetch_news = boom
        found, coverage = signals.news_signals(
            self.conn, self.entity_id, "bitcoin",
            datetime.now(UTC) - timedelta(hours=6),
            datetime.now(UTC), None, 50, attempts=1, pause=0)
        self.assertEqual(found, [])
        self.assertEqual(coverage["status"], "failed")
        self.assertIn("errors", coverage)


class ScanTests(RegistryCase):

    def test_horizons_are_nested_and_marked_not_additive(self):
        end = datetime(2026, 3, 2, tzinfo=UTC)
        closes = [100.0] * 100
        bars(self.conn, closes, start=BASE)
        report = signals.detect(self.conn, self.entity_id, KEY, 86400, end=end.isoformat(),
                                horizons=(24, 72), channels=("market",))
        self.assertEqual(sorted(report["horizons"]), ["24", "72"])
        self.assertLessEqual(report["horizons"]["24"]["signals"],
                             report["horizons"]["72"]["signals"])
        for hours in ("24", "72"):
            self.assertEqual(report["horizons"][hours]["hours"], int(hours))

    def test_validation_rejects_bad_parameters(self):
        end = "2026-03-02T00:00:00+00:00"
        for kwargs in ({"horizons": ()}, {"horizons": (1, 1)}, {"horizons": (0,)},
                       {"channels": ("nope",)}, {"channels": ()}, {"topic": "bad!"},
                       {"articles": 0}, {"articles": 9999}, {"news_cache_seconds": -1}):
            arguments = {"conn": self.conn, "entity_id": self.entity_id, "instrument_key": KEY,
                         "interval_seconds": 86400, "end": end, "horizons": (24,),
                         "channels": ("market",), "topic": "bitcoin", "language": None,
                         "articles": 50, "include_structure": True}
            arguments.update(kwargs)
            with self.assertRaises(ValueError):
                signals.detect(**arguments)

    def test_detect_reports_every_channel_status(self):
        end = datetime(2026, 3, 2, tzinfo=UTC)
        bars(self.conn, [100.0] * 100)
        report = signals.detect(self.conn, self.entity_id, KEY, 86400, end=end.isoformat(),
                                horizons=(24,), channels=("market", "stored"))
        for channel in ("market", "stored"):
            self.assertIn(channel, report["coverage"]["channels"])
            self.assertIn("status", report["coverage"]["channels"][channel])
        self.assertFalse(report["coverage"]["truth_verified"])


class CausesScanTests(RegistryCase):
    """`causes.scan` had no caller and no test, and raised on entry.

    It passed `medium_percent` where `moves.validate` expects a sequence of
    thresholds, so every argument after `move_hours` landed in the wrong slot and
    the function could only ever raise `ValueError`. With no stored bars it
    returns before touching the network, so the entry path is testable offline.
    """

    def test_entry_path_validates_and_reports_rather_than_raising(self):
        report = causes.scan(self.conn, self.entity_id, KEY, 86400, medium_percent=5,
                             big_percent=10, baseline_bars=90)
        self.assertEqual(report["method"], causes.METHOD)
        self.assertEqual(report["parameters"]["medium_percent"], 5)
        self.assertEqual(report["parameters"]["big_percent"], 10)
        self.assertEqual(report["parameters"]["baseline_bars"], 90)
        self.assertFalse(report["truth_verified"])

    def test_its_thresholds_are_validated_as_a_sequence(self):
        """Descending or non-integer tiers must be refused, which the shifted
        call could never do because it never reached the check."""
        for medium, big in ((10, 5), (5, 5), (0, 10), (5, 10.5)):
            with self.assertRaises(ValueError):
                causes.scan(self.conn, self.entity_id, KEY, 86400, medium_percent=medium,
                            big_percent=big)

    def test_a_topic_the_provider_would_reject_is_refused(self):
        with self.assertRaises(ValueError):
            causes.scan(self.conn, self.entity_id, KEY, 86400, topic="bad!")


class ProfileTests(RegistryCase):

    def _cause(self, move_id, category, key="a"):
        registry.add_move_cause(
            conn=self.conn, move_event_id=move_id, role="move", category=category,
            article_id=f"gdelt:{key}", observed_at="2026-01-01T00:00:00+00:00",
            headline="headline", source="gdelt", retrieved_at="2026-01-01T00:00:00+00:00")

    def _move(self, index, tier="p10", direction="down"):
        return registry.add_move_event(
            conn=self.conn, instrument_key=KEY, interval_seconds=86400, move_hours=24,
            tier=tier, threshold_percent="10", direction=direction,
            start_time=f"2026-01-{index:02d}T00:00:00+00:00",
            end_time=f"2026-01-{index + 1:02d}T00:00:00+00:00", start_price="100",
            end_price="80", change_percent="-20", terminal_bar_range_percent="25",
            baseline_mean_percent="0", baseline_std_percent="2", z_score="-10",
            detected_at="2026-02-01T00:00:00+00:00",
            available_at="2026-02-01T00:00:00+00:00")

    def test_enrichment_and_permutation_p_value_are_reported(self):
        for index in range(1, 9):
            move_id = self._move(index)
            self._cause(move_id, "etf_institutional", key=f"m{index}")
        for index in range(1, 5):
            registry.add_move_cause(
                conn=self.conn, role="control", control_key=f"{KEY}|86400|2026-02-0{index}",
                category="etf_institutional", article_id=f"gdelt:c{index}",
                observed_at="2026-02-01T00:00:00+00:00", headline="headline", source="gdelt",
                retrieved_at="2026-02-01T00:00:00+00:00")
        for index in (1, 2):
            registry.add_move_cause(
                conn=self.conn, role="control", control_key=f"{KEY}|86400|2026-02-0{index}",
                category="geopolitics", article_id=f"gdelt:g{index}",
                observed_at="2026-02-01T00:00:00+00:00", headline="headline", source="gdelt",
                retrieved_at="2026-02-01T00:00:00+00:00")
        self.conn.commit()
        result = causes.profile(self.conn, KEY, 86400, 24, 24, permutations=500)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["move_windows"], 8, "only moves with causes count")
        self.assertEqual(result["control_windows"], 4)
        enriched = next(item for item in result["categories"]
                        if item["category"] == "etf_institutional")
        self.assertEqual(enriched["moves_with_category"], 8)
        self.assertEqual(enriched["move_share"], 1.0)
        self.assertEqual(enriched["control_share"], 1.0)
        self.assertEqual(enriched["enrichment"], 1.0)
        self.assertGreater(enriched["permutation_p"], 0.0)
        absent = next(item for item in result["categories"]
                     if item["category"] == "geopolitics")
        self.assertEqual(absent["moves_with_category"], 0)
        self.assertEqual(absent["enrichment"], 0.0,
                         "a category seen only in controls is depleted before moves")
        self.assertEqual(absent["control_share"], 0.5)
        self.assertIn("permutation_null", result)
        self.assertIn("multiple_testing", result)

    def test_profile_without_attribution_says_so(self):
        self._move(1)
        self.conn.commit()
        result = causes.profile(self.conn, KEY, 86400, 24, 24, permutations=200)
        self.assertEqual(result["status"], "no_attributed_moves")
        self.assertEqual(result["move_windows"], 0)

    def test_permutation_is_reproducible(self):
        first = causes._permutation([1, 1, 0, 0], [0, 0, 0, 0], 500,
                                    __import__("random").Random(1))
        second = causes._permutation([1, 1, 0, 0], [0, 0, 0, 0], 500,
                                     __import__("random").Random(1))
        self.assertEqual(first, second)
        self.assertGreaterEqual(first, 0.0)
        self.assertLessEqual(first, 1.0)


class CauseStatusTests(unittest.TestCase):

    def test_provider_failure_is_not_reported_as_no_reason(self):
        self.assertEqual(causes._cause_status({"status": "failed"}, 0), "provider_unavailable")
        self.assertEqual(causes._cause_status({"status": "outside_provider_reach"}, 0),
                         "provider_unavailable")
        self.assertEqual(causes._cause_status({"status": "empty"}, 0), "no_articles")
        self.assertEqual(causes._cause_status({"status": "ok"}, 3), "attributed")
        self.assertEqual(causes._cause_status({"status": "ok", "truncated_requests": 1}, 3),
                         "attributed_truncated")


if __name__ == "__main__":
    unittest.main()
