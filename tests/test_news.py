import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, UTC
from pathlib import Path
from unittest.mock import patch

from lele.analysis import events
from lele.cli.main import main
from lele.core import registry
from lele.core.constants import EVIDENCE_ENDPOINTS
from lele.fetchers import news

START = datetime(2024, 1, 1, tzinfo=UTC)
END = "2024-01-01T12:00:00Z"


def seendate(value):
    return value.strftime("%Y%m%dT%H%M%SZ")


def article(identifier, seen, url=None, title=None, **extra):
    row = {
        "url": url or f"https://example.com/{identifier}",
        "title": title or f"Headline {identifier}",
        "seendate": seendate(seen), "domain": "example.com", "language": "English",
        "sourcecountry": "United States",
    }
    row.update(extra)
    return row


class FakeNewsClient:
    def __init__(self, *args, **kwargs):
        self.retrieved_at = "2024-01-01T12:00:00.500000+00:00"
        self.warnings = []
        self.url = None
        self.payload = {"articles": [
            article("a", START + timedelta(hours=2)),
            article("b", START + timedelta(hours=3), sourcecountry="United Kingdom"),
        ]}

    def get_json(self, url):
        if not url.startswith(EVIDENCE_ENDPOINTS["GDELT"]):
            raise AssertionError(f"unexpected URL {url}")
        self.url = url
        return self.payload


class NewsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db = self.root / "registry.db"
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(registry._SCHEMA)
        self.eid = registry.upsert_entity(self.conn, "instrument", "Bitcoin", key="fixture:btc")
        self.conn.commit()
        self.addCleanup(self.conn.close)

    def fetch(self, fake=FakeNewsClient, topic="bitcoin", **kwargs):
        with patch.object(news, "HTTPClient", fake):
            return news.fetch_news(self.conn, self.eid, topic, end=END, **kwargs)

    def test_builds_news_event_observations(self):
        payload, report = self.fetch()
        self.assertEqual(report["by_kind"], {"news_event": 2})
        self.assertEqual(report["observations"], 2)
        self.assertEqual(report["skipped"], {"non_https": 0, "invalid": 0, "outside_window": 0,
                                             "duplicate": 0})
        self.assertFalse(report["truth_verified"])
        self.assertIn("query=bitcoin", report["source_url"])
        self.assertIn("artlist", report["source_url"])
        rows = payload["observations"]
        first = next(row for row in rows if row["source_url"].endswith("/a"))
        self.assertEqual(first["kind"], "news_event")
        self.assertIsNone(first["measurement"])
        self.assertEqual(first["observed_at"], "2024-01-01T02:00:00+00:00")
        self.assertEqual(first["available_at"], first["observed_at"])
        self.assertEqual(first["platform"], "example.com")
        self.assertEqual(report["output_sha256"],
                         hashlib.sha256(
                             json.dumps(payload, ensure_ascii=True, allow_nan=False,
                                        separators=(",", ":")).encode()).hexdigest())

    def test_round_trip_through_evidence_validator(self):
        payload, _ = self.fetch()
        path = self.root / "news.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        records, digest = events._evidence(path, "fixture:btc")
        self.assertEqual(len(records), 2)
        self.assertEqual(len(digest), 64)

    def test_skips_non_https_outside_and_duplicates(self):
        class Variant(FakeNewsClient):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.payload = {"articles": [
                    article("a", START + timedelta(hours=2)),
                    article("a", START + timedelta(hours=2)),
                    article("http", START + timedelta(hours=2), url="http://example.com/http"),
                    article("old", START - timedelta(days=2)),
                ]}
        payload, report = self.fetch(fake=Variant)
        self.assertEqual(len(payload["observations"]), 1)
        self.assertEqual(report["skipped"], {"non_https": 1, "invalid": 0, "outside_window": 1,
                                             "duplicate": 1})

    def test_empty_articles_is_valid_empty_evidence(self):
        class Empty(FakeNewsClient):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.payload = {"articles": []}
        payload, report = self.fetch(fake=Empty)
        self.assertEqual((payload["observations"], report["by_kind"]), ([], {"news_event": 0}))
        path = self.root / "empty.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        records, _ = events._evidence(path, "fixture:btc")
        self.assertEqual(records, [])

    def test_malformed_response_rejected(self):
        for payload in ([], {"articles": "nope"}, "text"):
            with self.subTest(payload=payload):
                class Variant(FakeNewsClient):
                    def __init__(self, *args, _payload=payload, **kwargs):
                        super().__init__(*args, **kwargs)
                        self.payload = _payload
                with patch.object(news, "HTTPClient", Variant), self.assertRaises(ValueError):
                    news.fetch_news(self.conn, self.eid, "bitcoin", end=END)

    def test_request_window_bounds(self):
        now = datetime(2024, 1, 1, 12, tzinfo=UTC)
        for hours in (0, news.MAX_HOURS + 1, True, "24"):
            with self.subTest(hours=hours), self.assertRaises(ValueError):
                news.request_news_window(hours, None, now)
        with self.assertRaises(ValueError):
            news.request_news_window(24, "2999-01-01T00:00:00Z", now)
        with self.assertRaises(ValueError):
            news.request_news_window(24, "not-a-timestamp", now)
        self.assertEqual(news.request_news_window(6, None, now),
                         (datetime(2024, 1, 1, 6, tzinfo=UTC), now))
        self.assertEqual(news.request_news_window(24, END, now),
                         (datetime(2023, 12, 31, 12, tzinfo=UTC), now))

    def test_topic_limit_and_entity_validation(self):
        for topic in ("bitcoin$", "", "x" * 65):
            with self.subTest(topic=topic), self.assertRaises(ValueError):
                self.fetch(topic=topic)
        for limit in (0, news.MAX_ARTICLES + 1, True, "10"):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.fetch(limit=limit)
        with patch.object(news, "HTTPClient", FakeNewsClient):
            with self.assertRaises(ValueError):
                news.fetch_news(self.conn, 999, "bitcoin", end=END)

    def test_cli_roundtrip_read_only(self):
        output = self.root / "news.json"
        before = self.db.read_bytes()
        argv = ["--db", str(self.db), "--json", "fetch-news", str(self.eid), "bitcoin",
                "--hours", "24", "--end", END, "--output", str(output)]
        out, err = io.StringIO(), io.StringIO()
        with patch.object(news, "HTTPClient", FakeNewsClient), \
                patch("socket.socket", side_effect=AssertionError("network forbidden")), \
                redirect_stdout(out), redirect_stderr(err):
            status = main(argv)
        self.assertEqual((status, err.getvalue()), (0, ""))
        result = json.loads(out.getvalue())
        self.assertEqual(result["by_kind"], {"news_event": 2})
        written = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(len(written["observations"]), 2)
        self.assertEqual(self.db.read_bytes(), before)

    def test_cli_invalid_args_before_database(self):
        missing = self.root / "absent.db"
        cases = [
            ["bitcoin!", "--hours", "24", "--limit", "10"],
            ["bitcoin", "--hours", "0", "--limit", "10"],
            ["bitcoin", "--hours", "24", "--limit", "0"],
            ["bitcoin", "--hours", "24", "--limit", "251"],
        ]
        for topic, *extra in cases:
            with self.subTest(topic=topic, extra=extra), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main(["--db", str(missing), "fetch-news", "1", topic,
                                       *extra, "--output", str(self.root / "x.json")]), 2)
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
