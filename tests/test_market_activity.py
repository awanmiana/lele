"""Offline tests for the second headline source and per-asset market activity.

No network: feed bodies and provider payloads are stubbed, and each assertion
pins a value a wrong parse or a wrong semantic label would change.
"""
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, UTC

from lele.analysis import events, signals
from lele.core import db, registry
from lele.fetchers import crypto_context as ctx
from lele.fetchers import news_rss

KEY = "binance:BTCUSDT"
NOW = datetime(2026, 9, 26, 12, tzinfo=UTC)
FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<item><title>SEC sues exchange in crackdown - Reuters</title>
<link>https://news.google.com/rss/articles/AAA</link>
<pubDate>Fri, 25 Sep 2026 18:05:37 GMT</pubDate>
<source url="https://www.reuters.com">Reuters</source></item>
<item><title>Whale dumps coins, crash sparks panic - CoinDesk</title>
<link>https://news.google.com/rss/articles/BBB</link>
<pubDate>Fri, 25 Sep 2026 20:00:00 GMT</pubDate>
<source url="https://www.coindesk.com">CoinDesk</source></item>
<item><title>Too old - Blog</title><link>https://news.google.com/rss/articles/CCC</link>
<pubDate>Fri, 01 Aug 2026 10:00:00 GMT</pubDate><source url="https://b.test">Blog</source></item>
<item><title>No link here</title><pubDate>Fri, 25 Sep 2026 19:00:00 GMT</pubDate></item>
</channel></rss>"""


def market_payload(days, start_cap=1000.0):
    base = int(datetime(2026, 9, 20, tzinfo=UTC).timestamp()) * 1000
    prices, caps, volumes = [], [], []
    for index in range(days):
        stamp = base + index * 86400000
        cap = start_cap * (1.01 ** index)
        prices.append([stamp, 100.0 + index])
        caps.append([stamp, cap])
        volumes.append([stamp, 5.0 + index])
    return {"prices": prices, "market_caps": caps, "total_volumes": volumes}


class FeedParseTests(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.conn = sqlite3.connect(":memory:", factory=db.Connection)
        self.conn.row_factory = sqlite3.Row
        registry._initialize(self.conn)
        self.addCleanup(self.conn.close)
        self.entity_id = registry.upsert_entity(conn=self.conn, kind="instrument",
                                                name="Bitcoin", key=KEY)
        self.original = news_rss.HTTPClient

    def stub(self, text):
        class Fake:
            warnings = []
            retrieved_at = NOW.isoformat()
            user_agent = "lele/0.1"
            timeout = 20
            max_bytes = 2 * 1024 * 1024

            def __init__(self, *args, **kwargs):
                pass

            def get_text(self, url, max_bytes=None):
                self.url = url
                return text
        news_rss.HTTPClient = Fake
        self.addCleanup(lambda: setattr(news_rss, "HTTPClient", self.original))

    def test_items_become_headlines_with_publisher_and_clean_title(self):
        self.stub(FEED)
        end = (datetime(2026, 9, 25, 12, tzinfo=UTC) + timedelta(hours=12)).isoformat()
        payload, report = news_rss.fetch_news_feed(self.conn, self.entity_id, "bitcoin",
                                                   hours=24, end=end)
        self.assertEqual(report["observations"], 2, "an old item and a linkless item drop out")
        first = payload["observations"][0]
        self.assertEqual(first["description"], "SEC sues exchange in crackdown",
                         "the publisher suffix must be trimmed from the headline")
        self.assertEqual(first["platform"], "Reuters")
        self.assertEqual(first["kind"], "news_event")
        self.assertTrue(first["source_url"].startswith("https://"))
        self.assertEqual(first["entity_key"], KEY)
        self.assertEqual(report["skipped"]["outside_window"], 1)
        self.assertEqual(report["skipped"]["invalid"], 1)

    def test_a_document_type_declaration_is_refused(self):
        self.stub('<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>' + FEED)
        with self.assertRaises(ValueError) as caught:
            news_rss.fetch_news_feed(self.conn, self.entity_id, "bitcoin", hours=24)
        self.assertIn("document type declaration", str(caught.exception))

    def test_malformed_xml_is_refused(self):
        self.stub("<rss><channel><item>")
        with self.assertRaises(ValueError):
            news_rss.fetch_news_feed(self.conn, self.entity_id, "bitcoin", hours=24)

    def test_request_validation(self):
        for kwargs in ({"hours": 0}, {"hours": news_rss.MAX_HOURS + 1},
                       {"end": "2030-01-01T00:00:00+00:00"}):
            arguments = {"hours": 24, "end": None, "now": NOW}
            arguments.update(kwargs)
            with self.assertRaises(ValueError):
                news_rss.request_window(**arguments)
        for limit in (0, news_rss.MAX_ARTICLES + 1):
            with self.assertRaises(ValueError):
                news_rss.fetch_news_feed(self.conn, self.entity_id, "bitcoin", limit=limit)
        with self.assertRaises(ValueError):
            news_rss.fetch_news_feed(self.conn, self.entity_id, "bad!")
        with self.assertRaises(ValueError):
            news_rss.fetch_news_feed(self.conn, 0, "bitcoin")

    def test_the_feed_is_a_known_provider(self):
        self.assertIn("google_news", signals.NEWS_PROVIDERS)


class MarketActivityTests(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.conn = sqlite3.connect(":memory:", factory=db.Connection)
        self.conn.row_factory = sqlite3.Row
        registry._initialize(self.conn)
        self.addCleanup(self.conn.close)
        self.entity_id = registry.upsert_entity(conn=self.conn, kind="instrument",
                                                name="Bitcoin", key=KEY)
        self.original = ctx.HTTPClient

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
        self.addCleanup(lambda: setattr(ctx, "HTTPClient", self.original))

    def test_daily_pairs_become_market_cap_change_bound_to_the_instrument(self):
        self.stub(market_payload(5))
        payload, report = ctx.fetch_market_activity(self.conn, self.entity_id, KEY,
                                                    coin="bitcoin", days=5)
        self.assertEqual(report["stored"], 4, "the first day has no prior day to compare")
        self.assertEqual(report["skipped"]["no_prior_day"], 1)
        row = payload["observations"][-1]
        self.assertEqual(row["kind"], "market_activity")
        self.assertEqual(row["instrument_key"], KEY, "this series is instrument bound")
        self.assertEqual(row["unit"], "usd_market_cap_change")
        self.assertEqual(row["basis"], "estimated")
        evidence = json.loads(row["evidence"])
        self.assertGreater(float(evidence["market_cap_change_usd"]), 0)
        self.assertAlmostEqual(float(evidence["market_cap_change_percent"]), 1.0, places=3)
        self.assertIn("cannot separate", evidence["semantics"])

    def test_a_capital_fall_is_recorded_as_a_negative_change(self):
        base = int(datetime(2026, 9, 20, tzinfo=UTC).timestamp()) * 1000
        caps = [[base + index * 86400000, cap] for index, cap in enumerate((100.0, 110.0, 55.0))]
        self.stub({"prices": [[point[0], 1.0] for point in caps], "market_caps": caps,
                   "total_volumes": [[point[0], 1.0] for point in caps]})
        stored, report = ctx.fetch_market_activity(self.conn, self.entity_id, KEY,
                                                   coin="bitcoin", days=3)
        evidence = json.loads(stored["observations"][-1]["evidence"])
        self.assertEqual(evidence["market_cap_change_usd"], "-55.0")
        self.assertEqual(evidence["market_cap_change_percent"], "-50.0")
        self.assertEqual(evidence["prior_market_cap_usd"], "110.0")

    def test_points_without_a_market_cap_shorten_the_series_instead_of_failing(self):
        payload = market_payload(5)
        payload["market_caps"] = payload["market_caps"][:3]
        self.stub(payload)
        stored, report = ctx.fetch_market_activity(self.conn, self.entity_id, KEY,
                                                   coin="bitcoin", days=5)
        self.assertEqual(report["stored"], 2, "only the days with a market cap are comparable")
        self.assertEqual(report["skipped"]["incomplete"], 2)

    def test_an_empty_series_is_refused_rather_than_reported_as_no_activity(self):
        self.stub({"prices": [], "market_caps": [], "total_volumes": []})
        with self.assertRaises(ValueError):
            ctx.fetch_market_activity(self.conn, self.entity_id, KEY, coin="bitcoin", days=5)

    def test_validation(self):
        for kwargs in ({"coin": "dogecoin"}, {"days": 0}, {"days": 9999},
                       {"instrument_key": ""}):
            arguments = {"coin": "bitcoin", "days": 10, "entity_id": self.entity_id,
                         "instrument_key": KEY}
            arguments.update(kwargs)
            with self.assertRaises(ValueError):
                ctx.validate_activity(**arguments)

    def test_the_kind_is_documented_and_familied(self):
        self.assertIn("market_activity", events.KINDS)
        self.assertEqual(signals.OBSERVATION_FAMILY["market_activity"], "money_movement")


if __name__ == "__main__":
    unittest.main()
