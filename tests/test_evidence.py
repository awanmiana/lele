import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, UTC
from pathlib import Path
from unittest.mock import patch

from lele.analysis import events
from lele.cli.main import main
from lele.core import registry
from lele.core.constants import EVIDENCE_ENDPOINTS
from lele.fetchers import evidence

START = 1704067200  # 2024-01-01T00:00:00Z
END = "2024-01-01T00:20:00Z"


def kline(opening, quote, taker):
    return [opening * 1000, "100", "110", "90", "105", 5, (opening + 300) * 1000 - 1,
            quote, 20, "3", taker, "0"]


class FakeClient:
    def __init__(self, *args, **kwargs):
        self.retrieved_at = "2024-01-01T00:20:00+00:00"
        self.warnings = []
        self.payloads = {
            "KLINE": [kline(START, 1000, 600), kline(START + 300, 1000, 400),
                      kline(START + 600, 2000, 1500)],
            "OPEN_INTEREST": [
                {"symbol": "BTCUSDT", "sumOpenInterest": "10", "sumOpenInterestValue": "1000",
                 "timestamp": (START + 300) * 1000},
                {"symbol": "BTCUSDT", "sumOpenInterest": "11", "sumOpenInterestValue": "1100",
                 "timestamp": (START + 600) * 1000},
            ],
            "FUNDING": [{"symbol": "BTCUSDT", "fundingTime": START * 1000, "fundingRate": "0.00010000"}],
            "GLOBAL_LONG_SHORT": [{"symbol": "BTCUSDT", "longAccount": "0.56", "shortAccount": "0.44",
                                   "longShortRatio": "1.2727", "timestamp": (START + 600) * 1000}],
            "TOP_LONG_SHORT": [{"symbol": "BTCUSDT", "longAccount": "0.70", "shortAccount": "0.30",
                                "longShortRatio": "2.3333", "timestamp": (START + 900) * 1000}],
            "DEPTH": {"lastUpdateId": 9, "E": (START + 1200) * 1000, "T": (START + 1200) * 1000,
                      "bids": [["100", "2"], ["99", "1"]], "asks": [["101", "1"], ["102", "1"]]},
        }

    def get_json(self, url):
        for key, payload in self.payloads.items():
            if url.startswith(EVIDENCE_ENDPOINTS[key]):
                return payload
        raise AssertionError(f"unexpected URL {url}")


class FakeCotClient:
    def __init__(self, *args, **kwargs):
        self.retrieved_at = "2024-01-01T00:20:00.123456+00:00"
        self.warnings = []
        self.payloads = [
            {"market_and_exchange_names": "BITCOIN - CHICAGO MERCANTILE EXCHANGE",
             "report_date_as_yyyy_mm_dd": "2023-12-26T00:00:00.000", "open_interest_all": "20000",
             "noncomm_positions_long_all": "15000", "noncomm_positions_short_all": "14000"},
            {"market_and_exchange_names": "BITCOIN - CHICAGO MERCANTILE EXCHANGE",
             "report_date_as_yyyy_mm_dd": "2023-12-19T00:00:00.000", "open_interest_all": "19000",
             "noncomm_positions_long_all": "16000", "noncomm_positions_short_all": "12000"},
        ]

    def get_json(self, url):
        if not url.startswith(EVIDENCE_ENDPOINTS["COT"]):
            raise AssertionError(f"unexpected URL {url}")
        return self.payloads


class FakeShortClient:
    def __init__(self, *args, **kwargs):
        self.retrieved_at = "2024-01-01T00:20:00.654321+00:00"
        self.warnings = []
        self.calls = []
        self.payload = [
            {"symbolCode": "AAPL", "issueName": "Apple Inc. Common Stock",
             "settlementDate": "2023-12-29", "currentShortPositionQuantity": 1000,
             "previousShortPositionQuantity": 900, "averageDailyVolumeQuantity": 5000},
            {"symbolCode": "AAPL", "issueName": "Apple Inc. Common Stock",
             "settlementDate": "2023-12-15", "currentShortPositionQuantity": 1200,
             "previousShortPositionQuantity": 1100, "averageDailyVolumeQuantity": 4000},
        ]

    def post_json(self, url, payload):
        if not url.startswith(EVIDENCE_ENDPOINTS["SHORT_INTEREST"]):
            raise AssertionError(f"unexpected URL {url}")
        self.calls.append((url, payload))
        return self.payload


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db = self.root / "registry.db"
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(registry._SCHEMA)
        self.eid = registry.upsert_entity(self.conn, "instrument", "BTC", key="fixture:btc")
        self.conn.commit()
        self.addCleanup(self.conn.close)

    def fetch(self, fake=None, **kwargs):
        with patch.object(evidence, "HTTPClient", FakeClient):
            return evidence.fetch_evidence(self.conn, self.eid, "binance-futures", "BTCUSDT",
                                           end=END, **kwargs)

    def test_builds_point_in_time_observations(self):
        payload, report = self.fetch()
        self.assertEqual(set(payload), {"observations"})
        rows = payload["observations"]
        self.assertTrue(all(row["entity_key"] == "fixture:btc" for row in rows))
        self.assertEqual(report["by_kind"], {
            "order_flow": 3, "open_interest": 1, "funding_rate": 1,
            "long_short_account_ratio": 1, "top_long_short_position_ratio": 1,
            "order_book_imbalance": 1,
        })
        self.assertEqual(report["timestamp_semantics"].split(";")[0],
                         "candle end boundary for order flow")
        self.assertEqual(report["snapshot"]["order_book_levels"], evidence.DEPTH_LEVELS)
        self.assertEqual(report["snapshot"]["observed_at"], "2024-01-01T00:20:00+00:00")
        self.assertFalse(report["truth_verified"])
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(by_id["binance-futures:order_flow:1704067500"]["measurement"]["value"], "200")
        self.assertEqual(by_id["binance-futures:order_flow:1704067800"]["measurement"]["value"], "-200")
        self.assertEqual(by_id["binance-futures:order_flow:1704068100"]["measurement"]["value"], "1000")
        self.assertEqual(by_id["binance-futures:open_interest:1704067800"]["measurement"]["value"], "100")
        self.assertEqual(by_id["binance-futures:funding_rate:1704067200"]["measurement"]["value"],
                         "0.00010000")
        self.assertEqual(by_id["binance-futures:order_book_imbalance:1704068400"]["measurement"]["value"],
                         "0.191235059761")
        self.assertIsNone(
            by_id["binance-futures:order_book_imbalance:1704068400"]["measurement"]["currency"])
        self.assertEqual(by_id["binance-futures:long_short_account_ratio:1704067800"]["measurement"]["value"],
                         "1.2727")
        self.assertEqual(by_id["binance-futures:top_long_short_position_ratio:1704068100"]["measurement"]["value"],
                         "2.3333")
        self.assertEqual(by_id["binance-futures:order_flow:1704067500"]["observed_at"],
                         "2024-01-01T00:05:00+00:00")
        self.assertEqual(by_id["binance-futures:order_flow:1704067500"]["available_at"],
                         "2024-01-01T00:05:00+00:00")
        self.assertIsNone(by_id["binance-futures:order_flow:1704067500"]["mapping"])
        self.assertEqual(report["output_sha256"],
                         hashlib.sha256(
                             json.dumps(payload, ensure_ascii=True, allow_nan=False,
                                        separators=(",", ":")).encode()).hexdigest())

    def test_observations_round_trip_through_evidence_validator(self):
        payload, _ = self.fetch()
        path = self.root / "evidence.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        records, digest = events._evidence(path, "fixture:btc")
        self.assertEqual(len(records), 8)
        self.assertEqual(len(digest), 64)

    def test_depth_snapshot_falls_back_to_retrieval_time(self):
        class NoTime(FakeClient):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.payloads["DEPTH"] = {"lastUpdateId": 1, "bids": [["100", "2"]],
                                          "asks": [["101", "1"]]}
        with patch.object(evidence, "HTTPClient", NoTime):
            payload, report = evidence.fetch_evidence(self.conn, self.eid, "binance-futures",
                                                      "BTCUSDT", end=END)
        row = next(obs for obs in payload["observations"] if obs["kind"] == "order_book_imbalance")
        self.assertEqual(row["observed_at"], "2024-01-01T00:20:00+00:00")
        self.assertEqual(report["snapshot"]["observed_at"], row["observed_at"])

    def test_malformed_responses_rejected(self):
        fixtures = {
            "bad_close": {"KLINE": [kline(START, 1000, 600)[:6] + [1] + kline(START, 1000, 600)[7:]]},
            "taker_over_quote": {"KLINE": [kline(START, 1000, 2000)]},
            "negative_oi": {"OPEN_INTEREST": [{"symbol": "BTCUSDT", "sumOpenInterest": "1",
                                                "sumOpenInterestValue": "-5", "timestamp": (START + 300) * 1000}]},
            "bad_funding": {"FUNDING": [{"symbol": "BTCUSDT", "fundingTime": START * 1000,
                                         "fundingRate": "nan"}]},
            "empty_depth": {"DEPTH": {"lastUpdateId": 9, "bids": [], "asks": [["101", "1"]]}},
            "bad_depth_level": {"DEPTH": {"lastUpdateId": 9, "bids": [["100"]], "asks": [["101", "1"]]}},
            "bad_ratio": {"GLOBAL_LONG_SHORT": [{"symbol": "BTCUSDT", "longShortRatio": "nan",
                                                 "timestamp": (START + 600) * 1000}]},
            "negative_ratio": {"TOP_LONG_SHORT": [{"symbol": "BTCUSDT", "longShortRatio": "-1",
                                                   "timestamp": (START + 900) * 1000}]},
        }
        for name, replacement in fixtures.items():
            with self.subTest(name=name):
                class Variant(FakeClient):
                    def __init__(self, *args, _replacement=replacement, **kwargs):
                        super().__init__(*args, **kwargs)
                        self.payloads.update(_replacement)
                with patch.object(evidence, "HTTPClient", Variant), self.assertRaises(ValueError):
                    evidence.fetch_evidence(self.conn, self.eid, "binance-futures", "BTCUSDT", end=END)

    def test_unsupported_source_symbol_and_entity(self):
        with self.assertRaises(ValueError):
            evidence.fetch_evidence(self.conn, self.eid, "yahoo", "BTCUSDT", end=END)
        with self.assertRaises(ValueError):
            evidence.fetch_evidence(self.conn, self.eid, "binance-futures", "AAPL", end=END)
        with patch.object(evidence, "HTTPClient", FakeClient):
            with self.assertRaises(ValueError):
                evidence.fetch_evidence(self.conn, 999, "binance-futures", "BTCUSDT", end=END)

    def test_request_window_bounds(self):
        for limit in (0, 1, 301, True, "288"):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                evidence.request_window(limit, END, evidence._now())
        start, finish = evidence.request_window(288, END, evidence._now())
        self.assertEqual(finish, START + 1200)
        self.assertEqual(start, START + 1200 - 288 * 300)

    def test_tokenized_gold_symbol(self):
        self.assertEqual(evidence.SOURCES["binance-futures"], ("BTCUSDT", "PAXGUSDT"))
        start, finish = evidence.request_window(288, END, evidence._now(), "PAXGUSDT")
        self.assertEqual((start, finish), evidence.request_window(288, END, evidence._now()))
        with self.assertRaises(ValueError):
            evidence.request_window(288, END, evidence._now(), "NOPE")
        with patch.object(evidence, "HTTPClient", FakeClient):
            payload, report = evidence.fetch_evidence(self.conn, self.eid, "binance-futures",
                                                      "PAXGUSDT", end=END)
        self.assertEqual(report["symbol"], "PAXGUSDT")
        self.assertTrue(all(row["entity_key"] == "fixture:btc" for row in payload["observations"]))

    def test_cli_roundtrip_read_only(self):
        output = self.root / "evidence.json"
        before = self.db.read_bytes()
        argv = ["--db", str(self.db), "--json", "fetch-evidence", str(self.eid), "binance-futures",
                "BTCUSDT", "--end", END, "--output", str(output)]
        out, err = io.StringIO(), io.StringIO()
        with patch.object(evidence, "HTTPClient", FakeClient), \
                patch("socket.socket", side_effect=AssertionError("network forbidden")), \
                redirect_stdout(out), redirect_stderr(err):
            status = main(argv)
        self.assertEqual((status, err.getvalue()), (0, ""))
        result = json.loads(out.getvalue())
        self.assertEqual(result["by_kind"], {
            "order_flow": 3, "open_interest": 1, "funding_rate": 1,
            "long_short_account_ratio": 1, "top_long_short_position_ratio": 1,
            "order_book_imbalance": 1,
        })
        self.assertEqual(result["output"], str(output))
        written = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(len(written["observations"]), 8)
        self.assertTrue(all(set(row) == events.FIELDS for row in written["observations"]))
        self.assertEqual(self.db.read_bytes(), before)
        self.assertFalse((self.root / "evidence.json.tmp").exists())

    def test_cli_refuses_overwrite(self):
        output = self.root / "evidence.json"
        output.write_text("{}", encoding="utf-8")
        argv = ["--db", str(self.db), "fetch-evidence", str(self.eid), "binance-futures",
                "BTCUSDT", "--end", END, "--output", str(output)]
        with patch.object(evidence, "HTTPClient", FakeClient):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main(argv), 1)

    def test_cli_invalid_args_before_database(self):
        missing = self.root / "absent.db"
        for limit in ("1", "301"):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main(["--db", str(missing), "fetch-evidence", "1", "binance-futures",
                                       "BTCUSDT", "--limit", limit, "--output", str(self.root / "x.json")]), 2)
        self.assertFalse(missing.exists())


class CotTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db = self.root / "registry.db"
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(registry._SCHEMA)
        self.eid = registry.upsert_entity(self.conn, "instrument", "BTC", key="fixture:btc")
        self.conn.commit()
        self.addCleanup(self.conn.close)

    def fetch(self, fake=FakeCotClient, **kwargs):
        with patch.object(evidence, "HTTPClient", fake):
            return evidence.fetch_cot(self.conn, self.eid, "cftc", "BITCOIN-CME",
                                      end="2024-01-01", **kwargs)

    def test_builds_measured_observations(self):
        payload, report = self.fetch()
        self.assertEqual(report["by_kind"], {"cot_open_interest": 2, "cot_positioning": 2})
        self.assertEqual(report["market"], "BITCOIN - CHICAGO MERCANTILE EXCHANGE")
        self.assertIn("retrieval", report["timestamp_semantics"])
        self.assertFalse(report["truth_verified"])
        by_id = {row["id"]: row for row in payload["observations"]}
        self.assertEqual(by_id["cftc-cot:cot_open_interest:2023-12-26"]["measurement"]["value"],
                         "20000")
        self.assertEqual(by_id["cftc-cot:cot_open_interest:2023-12-26"]["observed_at"],
                         "2023-12-26T00:00:00+00:00")
        self.assertEqual(by_id["cftc-cot:cot_open_interest:2023-12-26"]["available_at"],
                         "2024-01-01T00:20:00+00:00")
        self.assertEqual(by_id["cftc-cot:cot_positioning:2023-12-26"]["measurement"]["value"], "1000")
        self.assertEqual(by_id["cftc-cot:cot_positioning:2023-12-19"]["measurement"]["value"], "4000")
        self.assertIsNone(by_id["cftc-cot:cot_positioning:2023-12-26"]["measurement"]["currency"])
        self.assertEqual(report["output_sha256"],
                         hashlib.sha256(
                             json.dumps(payload, ensure_ascii=True, allow_nan=False,
                                        separators=(",", ":")).encode()).hexdigest())

    def test_alternate_markets(self):
        self.assertEqual(set(evidence.COT_MARKETS),
                         {"BITCOIN-CME", "GOLD-COMEX", "WTI-NYMEX"})

        class Gold(FakeCotClient):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.payloads = [dict(self.payloads[0],
                                      market_and_exchange_names="GOLD - COMMODITY EXCHANGE INC.",
                                      open_interest_all="500000",
                                      noncomm_positions_long_all="200000",
                                      noncomm_positions_short_all="150000")]
        with patch.object(evidence, "HTTPClient", Gold):
            payload, report = evidence.fetch_cot(self.conn, self.eid, "cftc", "GOLD-COMEX",
                                                 end="2024-01-01")
        self.assertEqual(report["market"], "GOLD - COMMODITY EXCHANGE INC.")
        self.assertEqual(report["by_kind"], {"cot_open_interest": 1, "cot_positioning": 1})
        position = next(row for row in payload["observations"] if row["kind"] == "cot_positioning")
        self.assertEqual(position["measurement"]["value"], "50000")

    def test_round_trip_through_evidence_validator(self):
        payload, _ = self.fetch()
        path = self.root / "cot.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        records, digest = events._evidence(path, "fixture:btc")
        self.assertEqual(len(records), 4)
        self.assertEqual(len(digest), 64)

    def test_request_window_bounds(self):
        now = datetime(2024, 1, 1, tzinfo=UTC)
        for reports in (0, 53, True, "12"):
            with self.subTest(reports=reports), self.assertRaises(ValueError):
                evidence.request_cot_window(reports, None, now)
        with self.assertRaises(ValueError):
            evidence.request_cot_window(12, "2999-01-01", now)
        with self.assertRaises(ValueError):
            evidence.request_cot_window(12, "not-a-date", now)
        start, finish = evidence.request_cot_window(12, "2024-01-01", now)
        self.assertEqual((start, finish), (date(2023, 10, 9), date(2024, 1, 1)))
        self.assertEqual(evidence.request_cot_window(4, None, now), (date(2023, 12, 4), date(2024, 1, 1)))

    def test_cot_malformed_rejected(self):
        cases = {
            "bad_date": {"report_date_as_yyyy_mm_dd": "2023-13-40T00:00:00.000"},
            "nonzero_time": {"report_date_as_yyyy_mm_dd": "2023-12-26T12:00:00.000"},
            "negative_oi": {"open_interest_all": "-1"},
            "bad_position": {"noncomm_positions_long_all": "nan"},
            "market_mismatch": {"market_and_exchange_names": "GOLD - COMMODITY EXCHANGE INC."},
        }
        for name, change in cases.items():
            with self.subTest(name=name):
                class Variant(FakeCotClient):
                    def __init__(self, *args, _change=change, **kwargs):
                        super().__init__(*args, **kwargs)
                        self.payloads = [dict(self.payloads[0], **_change)]
                with patch.object(evidence, "HTTPClient", Variant), self.assertRaises(ValueError):
                    evidence.fetch_cot(self.conn, self.eid, "cftc", "BITCOIN-CME", end="2024-01-01")

    def test_unsupported_source_symbol_and_entity(self):
        with self.assertRaises(ValueError):
            evidence.fetch_cot(self.conn, self.eid, "binance-futures", "BITCOIN-CME", end="2024-01-01")
        with self.assertRaises(ValueError):
            evidence.fetch_cot(self.conn, self.eid, "cftc", "GOLD-CME", end="2024-01-01")
        with patch.object(evidence, "HTTPClient", FakeCotClient):
            with self.assertRaises(ValueError):
                evidence.fetch_cot(self.conn, 999, "cftc", "BITCOIN-CME", end="2024-01-01")

    def test_cli_roundtrip_read_only(self):
        output = self.root / "cot.json"
        before = self.db.read_bytes()
        argv = ["--db", str(self.db), "--json", "fetch-cot", str(self.eid), "cftc", "BITCOIN-CME",
                "--end", "2024-01-01", "--output", str(output)]
        out, err = io.StringIO(), io.StringIO()
        with patch.object(evidence, "HTTPClient", FakeCotClient), \
                patch("socket.socket", side_effect=AssertionError("network forbidden")), \
                redirect_stdout(out), redirect_stderr(err):
            status = main(argv)
        self.assertEqual((status, err.getvalue()), (0, ""))
        result = json.loads(out.getvalue())
        self.assertEqual(result["by_kind"], {"cot_open_interest": 2, "cot_positioning": 2})
        self.assertEqual(result["output"], str(output))
        written = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(len(written["observations"]), 4)
        self.assertEqual(self.db.read_bytes(), before)

    def test_cli_invalid_args_before_database(self):
        missing = self.root / "absent.db"
        for limit in ("0", "53"):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main(["--db", str(missing), "fetch-cot", "1", "cftc", "BITCOIN-CME",
                                       "--limit", limit, "--output", str(self.root / "x.json")]), 2)
        self.assertFalse(missing.exists())


class ShortTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db = self.root / "registry.db"
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(registry._SCHEMA)
        self.eid = registry.upsert_entity(self.conn, "instrument", "Apple", key="fixture:aapl")
        self.conn.commit()
        self.addCleanup(self.conn.close)

    def fetch(self, fake=FakeShortClient, **kwargs):
        with patch.object(evidence, "HTTPClient", fake):
            return evidence.fetch_short(self.conn, self.eid, "finra", "AAPL",
                                        end="2024-01-01", **kwargs)

    def test_builds_short_interest_observations(self):
        payload, report = self.fetch()
        self.assertEqual(report["by_kind"], {"short_interest": 2})
        self.assertIn("short interest", report["cadence"])
        self.assertFalse(report["truth_verified"])
        by_id = {row["id"]: row for row in payload["observations"]}
        first = by_id["finra:short_interest:AAPL:2023-12-29"]
        self.assertEqual(first["measurement"], {"value": "1000", "unit": "share", "currency": None,
                                                "basis": "observed"})
        self.assertEqual(first["observed_at"], "2023-12-29T00:00:00+00:00")
        self.assertEqual(first["available_at"], "2024-01-01T00:20:00+00:00")
        self.assertEqual(report["output_sha256"],
                         hashlib.sha256(
                             json.dumps(payload, ensure_ascii=True, allow_nan=False,
                                        separators=(",", ":")).encode()).hexdigest())

    def test_request_payload_filters(self):
        fake = FakeShortClient()
        with patch.object(evidence, "HTTPClient", return_value=fake):
            evidence.fetch_short(self.conn, self.eid, "finra", "AAPL", months=6, end="2024-01-01")
        _, request = fake.calls[0]
        self.assertEqual(request["compareFilters"],
                         [{"fieldName": "symbolCode", "fieldValue": "AAPL", "compareType": "EQUAL"}])
        self.assertEqual(request["dateRangeFilters"],
                         [{"fieldName": "settlementDate", "startDate": "2023-07-01",
                           "endDate": "2024-01-01"}])

    def test_round_trip_through_evidence_validator(self):
        payload, _ = self.fetch()
        path = self.root / "short.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        records, digest = events._evidence(path, "fixture:aapl")
        self.assertEqual(len(records), 2)
        self.assertEqual(len(digest), 64)

    def test_request_window_bounds(self):
        now = datetime(2024, 1, 1, tzinfo=UTC)
        for months in (0, 37, True, "12"):
            with self.subTest(months=months), self.assertRaises(ValueError):
                evidence.request_short_window(months, None, now)
        with self.assertRaises(ValueError):
            evidence.request_short_window(12, "2999-01-01", now)
        with self.assertRaises(ValueError):
            evidence.request_short_window(12, "not-a-date", now)
        self.assertEqual(evidence.request_short_window(12, "2024-01-01", now),
                         (date(2023, 1, 1), date(2024, 1, 1)))
        self.assertEqual(evidence.request_short_window(1, None, now),
                         (date(2023, 12, 1), date(2024, 1, 1)))

    def test_short_malformed_and_mismatch_rejected(self):
        cases = {
            "bad_date": {"settlementDate": "not-a-date"},
            "negative": {"currentShortPositionQuantity": -1},
            "mismatch": {"symbolCode": "MSFT"},
            "bad_quantity": {"currentShortPositionQuantity": "nan"},
        }
        for name, change in cases.items():
            with self.subTest(name=name):
                class Variant(FakeShortClient):
                    def __init__(self, *args, _change=change, **kwargs):
                        super().__init__(*args, **kwargs)
                        self.payload = [dict(self.payload[0], **_change)]
                with patch.object(evidence, "HTTPClient", Variant), self.assertRaises(ValueError):
                    evidence.fetch_short(self.conn, self.eid, "finra", "AAPL", end="2024-01-01")

    def test_empty_and_unsupported_rejected(self):
        class Empty(FakeShortClient):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.payload = []
        with patch.object(evidence, "HTTPClient", Empty), self.assertRaisesRegex(ValueError, "no short interest"):
            evidence.fetch_short(self.conn, self.eid, "finra", "AAPL", end="2024-01-01")
        with self.assertRaises(ValueError):
            evidence.fetch_short(self.conn, self.eid, "cftc", "AAPL", end="2024-01-01")
        with self.assertRaises(ValueError):
            evidence.fetch_short(self.conn, self.eid, "finra", "aapl", end="2024-01-01")
        with patch.object(evidence, "HTTPClient", FakeShortClient):
            with self.assertRaises(ValueError):
                evidence.fetch_short(self.conn, 999, "finra", "AAPL", end="2024-01-01")

    def test_cli_roundtrip_read_only(self):
        output = self.root / "short.json"
        before = self.db.read_bytes()
        argv = ["--db", str(self.db), "--json", "fetch-short", str(self.eid), "finra", "AAPL",
                "--months", "6", "--end", "2024-01-01", "--output", str(output)]
        out, err = io.StringIO(), io.StringIO()
        with patch.object(evidence, "HTTPClient", FakeShortClient), \
                patch("socket.socket", side_effect=AssertionError("network forbidden")), \
                redirect_stdout(out), redirect_stderr(err):
            status = main(argv)
        self.assertEqual((status, err.getvalue()), (0, ""))
        result = json.loads(out.getvalue())
        self.assertEqual(result["by_kind"], {"short_interest": 2})
        written = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(len(written["observations"]), 2)
        self.assertEqual(self.db.read_bytes(), before)

    def test_cli_invalid_args_before_database(self):
        missing = self.root / "absent.db"
        cases = [["AAPL", "--months", "0"], ["AAPL", "--months", "37"], ["aapl", "--months", "6"]]
        for symbol, *extra in cases:
            with self.subTest(symbol=symbol, extra=extra), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main(["--db", str(missing), "fetch-short", "1", "finra", symbol,
                                       *extra, "--output", str(self.root / "x.json")]), 2)
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
