import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, UTC
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from lele.analysis import projection
from lele.cli.main import main
from lele.core import registry
from lele.core.constants import PRICE_ENDPOINTS
from lele.fetchers import http, prices


NOW = datetime(2026, 9, 18, 0, 20, 30, tzinfo=UTC)
START = int(NOW.replace(minute=0, second=0).timestamp())


def kline(slot, close="100.00000000"):
    opening = (START + slot * 300) * 1000
    return [opening, "100", "100", "100", close, "1", opening + 299999,
            "100", 1, "1", "100", "0"]


def chart(symbol="GC=F", slots=(0, 1, 2, 3), closes=None):
    return {"chart": {"error": None, "result": [{
        "meta": {"symbol": symbol, "currency": "USD", "exchangeName": "TEST_EXCHANGE",
                 "instrumentType": "EQUITY" if symbol == "AAPL" else "FUTURE",
                 "dataGranularity": "5m"},
        "timestamp": [START + slot * 300 for slot in slots],
        "indicators": {"quote": [{"close": closes if closes is not None else [100.25] * len(slots)}],
                       "adjclose": [{"adjclose": [1] * len(slots)}]},
    }]}}


class PriceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db = self.root / "registry.db"
        self.output = self.root / "prices.json"
        with registry.get_conn(str(self.db)) as conn:
            self.eid = registry.upsert_entity(conn, "instrument", "Synthetic selected asset",
                                              key="user:existing-exact-key")
        self.client = Mock(warnings=[], retrieved_at=NOW.isoformat())
        self.client.get_json.return_value = [kline(i) for i in range(4)]
        for patcher in (
            patch.object(prices, "HTTPClient", return_value=self.client),
            patch.object(prices, "_now", return_value=NOW),
            patch.object(http, "build_opener", side_effect=AssertionError("Live HTTP forbidden")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def fetch(self, source="binance", symbol="BTCUSDT", **kwargs):
        with registry.get_conn(str(self.db)) as conn:
            before = conn.total_changes
            result = prices.fetch_prices(conn, self.eid, source, symbol, limit=kwargs.pop("limit", 4), **kwargs)
            self.assertEqual(conn.total_changes, before)
            return result

    def invoke(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--db", str(self.db), "--json", *args])
        return code, out.getvalue(), err.getvalue()

    def cli_fetch(self, *extra, output=None, source="binance", symbol="BTCUSDT", eid=None):
        return self.invoke("fetch-prices", str(self.eid if eid is None else eid), source, symbol,
                           "--limit", "4", "--output", str(output or self.output), *extra)

    def test_binance_end_stamps_and_exact_key(self):
        output, report = self.fetch()
        self.assertEqual(output["instrument"]["entity_key"], "user:existing-exact-key")
        self.assertEqual(output["instrument"]["currency"], "USDT")
        self.assertEqual(output["instrument"]["price_type"], "spot_5m_close")
        self.assertEqual(output["prices"][0], {"timestamp": "2026-09-18T00:05:00+00:00", "price": "100.00000000"})
        self.assertEqual(output["prices"][-1]["timestamp"], "2026-09-18T00:20:00+00:00")
        self.assertEqual(report["missing_slots"], 0)
        self.assertEqual(report["retrieved_at"], NOW.isoformat())
        self.assertFalse(report["truth_verified"])
        self.client.get_json.assert_called_once()
        params = parse_qs(urlsplit(report["source_url"]).query)
        self.assertEqual(params, {"symbol": ["BTCUSDT"], "interval": ["5m"],
                                 "startTime": [str(START * 1000)],
                                 "endTime": [str((START + 1200) * 1000 - 1)], "limit": ["4"]})
        self.assertIsNone(self.client.cache_dir)
        self.assertEqual(self.client.ttl, 0)

    def test_binance_tokenized_gold_symbol(self):
        output, report = self.fetch(symbol="PAXGUSDT")
        self.assertEqual(output["instrument"]["symbol"], "PAXGUSDT")
        self.assertEqual(output["instrument"]["asset_class"], "gold")
        self.assertEqual(output["instrument"]["unit"], "PAXG token")
        params = parse_qs(urlsplit(report["source_url"]).query)
        self.assertEqual(params["symbol"], ["PAXGUSDT"])

    def test_response_hash_is_labeled_canonical_not_wire(self):
        _, report = self.fetch()
        raw = json.dumps(self.client.get_json.return_value, sort_keys=True, ensure_ascii=True,
                         separators=(",", ":"), allow_nan=False).encode()
        self.assertEqual(report["response_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertIn("not wire", report["response_hash_encoding"])

    def test_all_yahoo_examples_metadata_and_unadjusted_close(self):
        for symbol, asset, unit in (("GC=F", "gold", "troy ounce"), ("CL=F", "oil", "barrel"),
                                    ("RB=F", "fuel", "US gallon"), ("AAPL", "stock", "share")):
            with self.subTest(symbol=symbol):
                self.client.get_json.return_value = chart(symbol)
                output, report = self.fetch("yahoo", symbol)
                self.assertEqual(output["instrument"]["asset_class"], asset)
                self.assertEqual(output["instrument"]["unit"], unit)
                self.assertEqual(output["instrument"]["venue"], "TEST_EXCHANGE")
                self.assertEqual(output["prices"][0]["price"], "100.25")
                self.assertIn("unsupported", " ".join(report["limitations"]))
                self.assertEqual(report["provider_metadata"]["symbol"], symbol)
                self.assertEqual(parse_qs(urlsplit(report["source_url"]).query), {
                    "interval": ["5m"], "period1": [str(START)], "period2": [str(START + 1200)],
                    "includePrePost": ["false"],
                })

    def test_unordered_rows_sorted_without_filling_gaps(self):
        for source, symbol, payload in (("binance", "BTCUSDT", [kline(3), kline(0), kline(1)]),
                                        ("yahoo", "GC=F", chart(slots=(3, 0, 1)))):
            with self.subTest(source=source):
                self.client.get_json.return_value = payload
                output, report = self.fetch(source, symbol)
                self.output.write_text(json.dumps(output))
                with registry.get_conn(str(self.db)) as conn:
                    evaluation = projection.project(conn, self.eid, self.output)["evaluation"]
                self.assertEqual(evaluation["eligible_predictions"], 1)
                self.assertEqual(evaluation["skipped_gap_pairs"], 1)
                self.assertEqual(report["missing_slots"], 1)

    def test_duplicates_rejected_even_identical_or_null(self):
        for close in ("100.00000000", None, "101"):
            self.client.get_json.return_value = [kline(0), kline(0, close), kline(1)]
            with self.subTest(close=close), self.assertRaisesRegex(ValueError, "duplicate"):
                self.fetch()
        self.client.get_json.return_value = chart(slots=(0, 1, 1))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.fetch("yahoo", "GC=F")

    def test_missing_closes_preserve_gap(self):
        for source, symbol, payload in (("binance", "BTCUSDT", [kline(0), kline(1, None), kline(2)]),
                                        ("yahoo", "GC=F", chart(slots=(0, 1, 2), closes=[100, None, 102]))):
            self.client.get_json.return_value = payload
            output, report = self.fetch(source, symbol)
            self.assertEqual(len(output["prices"]), 2)
            self.assertEqual(report["skipped"]["missing_closes"], 1)
            self.assertEqual(output["prices"][1]["timestamp"], "2026-09-18T00:15:00+00:00")

    def test_partial_future_and_outside_window_excluded(self):
        for source, symbol, payload in (("binance", "BTCUSDT", [kline(-1), kline(0), kline(1), kline(4), kline(5)]),
                                        ("yahoo", "GC=F", chart(slots=(-1, 0, 1, 4, 5)))):
            self.client.get_json.return_value = payload
            output, report = self.fetch(source, symbol)
            self.assertEqual(len(output["prices"]), 2)
            self.assertEqual(report["skipped"]["outside_window"], 1)
            self.assertEqual(report["skipped"]["unclosed"], 2)

    def test_retrieval_time_also_limits_closure(self):
        self.client.retrieved_at = "2026-09-18T00:12:00Z"
        output, report = self.fetch()
        self.assertEqual(len(output["prices"]), 2)
        self.assertEqual(report["skipped"]["unclosed"], 2)

    def test_malformed_binance_prices(self):
        for value in (True, 100, {}, [], "", "NaN", "Infinity", "-1", "0", "1e13", "1e-13", " 1", "1" * 65):
            self.client.get_json.return_value = [kline(0), kline(1, value)]
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.fetch()

    def test_malformed_yahoo_prices(self):
        for value in (True, "100", {}, [], "", float("nan"), float("inf"), -1, 0, 1e13, 1e-13):
            self.client.get_json.return_value = chart(slots=(0, 1), closes=[100, value])
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.fetch("yahoo", "GC=F")

    def test_malformed_binance_timestamps_and_row_shape(self):
        for index, value in ((0, True), (0, str(START * 1000)), (0, START * 1000 + 1),
                             (0, (START + 1) * 1000), (0, -300000), (0, 10**30),
                             (6, True), (6, START * 1000 + 300000), (6, None)):
            row = kline(0)
            row[index] = value
            self.client.get_json.return_value = [row, kline(1)]
            with self.subTest(index=index, value=value), self.assertRaises(ValueError):
                self.fetch()
        for payload in ({"code": -1}, None, [None], [kline(0)[:-1]], [kline(0) + [0]]):
            self.client.get_json.return_value = payload
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.fetch()

    def test_malformed_yahoo_timestamps(self):
        for value in (True, None, "123", START + 1, float(START), -300, 10**30, START * 1000):
            payload = chart()
            payload["chart"]["result"][0]["timestamp"][1] = value
            self.client.get_json.return_value = payload
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.fetch("yahoo", "GC=F")

    def test_yahoo_trailing_snapshot_strictness(self):
        def mutate(field, value):
            payload = chart(slots=(0, 1, 2, 3))
            rows = payload["chart"]["result"][0]
            rows["timestamp"].append(START + 1400 + 160)
            rows["indicators"]["quote"][0]["close"].append(100.5)
            rows["meta"]["regularMarketTime"] = START + 1400 + 160
            if field == "timestamp":
                rows["timestamp"][-1] = value
            elif field == "close":
                rows["indicators"]["quote"][0]["close"][-1] = value
            elif field == "regularMarketTime":
                rows["meta"]["regularMarketTime"] = value
            return payload

        for field, value in (
            ("close", None),
            ("close", "100"),
            ("close", -1),
            ("timestamp", "bad"),
            ("timestamp", None),
            ("timestamp", float(START + 1400)),
            ("regularMarketTime", None),
            ("regularMarketTime", "bad"),
            ("regularMarketTime", START + 1400 + 159),
            ("regularMarketTime", float(START + 1400 + 160)),
        ):
            self.client.get_json.return_value = mutate(field, value)
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.fetch("yahoo", "GC=F")

    def test_yahoo_trailing_snapshot_accepted_and_counted(self):
        payload = chart(slots=(0, 1, 2, 3))
        rows = payload["chart"]["result"][0]
        rows["timestamp"].append(START + 1400 + 160)
        rows["indicators"]["quote"][0]["close"].append(100.5)
        rows["meta"]["regularMarketTime"] = START + 1400 + 160
        rows["meta"]["regularMarketPrice"] = 100.499
        self.client.get_json.return_value = payload
        output, report = self.fetch("yahoo", "GC=F")
        self.assertEqual(len(output["prices"]), 4)
        self.assertEqual(report["received"], 5)
        self.assertEqual(report["skipped"], {"missing_closes": 0, "outside_window": 0,
                                             "unclosed": 0, "trailing_snapshots": 1})
        self.assertEqual(output["prices"][-1]["timestamp"], "2026-09-18T00:20:00+00:00")
        self.assertIn("trailing", " ".join(report["limitations"]))
        self.assertIn("trailing_snapshots", json.dumps(report))

    def test_yahoo_trailing_snapshot_negative_and_out_of_range_rejected(self):
        rows = chart(slots=(0, 1, 2, 3))["chart"]["result"][0]
        for value in (-140, 10**30):
            payload = chart(slots=(0, 1, 2, 3))
            rows = payload["chart"]["result"][0]
            rows["timestamp"][-1] = value
            self.client.get_json.return_value = payload
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.fetch("yahoo", "GC=F")

    def test_yahoo_trailing_snapshot_only_final_row_allowed(self):
        payload = chart(slots=(0, 1, 2, 3))
        payload["chart"]["result"][0]["timestamp"].insert(0, START - 140)
        payload["chart"]["result"][0]["indicators"]["quote"][0]["close"].insert(0, 99)
        with self.assertRaises(ValueError):
            self.fetch("yahoo", "GC=F")
    def test_yahoo_schema_shapes_and_missing_close_array(self):
        bad = [None, [], {}, {"chart": None}, {"chart": {"error": None, "result": []}},
               {"chart": {"error": None, "result": [None]}}]
        for field, value in (("timestamp", None), ("indicators", None), ("meta", [])):
            payload = chart()
            payload["chart"]["result"][0][field] = value
            bad.append(payload)
        for quote in ([], [None], [{}], [{"close": [1]}], [{"close": None}]):
            payload = chart()
            payload["chart"]["result"][0]["indicators"]["quote"] = quote
            bad.append(payload)
        for payload in bad:
            self.client.get_json.return_value = payload
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.fetch("yahoo", "GC=F")

    def test_yahoo_metadata_mismatch_rejected(self):
        for field, value in (("symbol", "CL=F"), ("currency", "EUR"), ("instrumentType", "EQUITY"),
                             ("dataGranularity", "1m"), ("exchangeName", ""), ("exchangeName", None)):
            payload = chart()
            payload["chart"]["result"][0]["meta"][field] = value
            self.client.get_json.return_value = payload
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.fetch("yahoo", "GC=F")

    def test_yahoo_provider_error_no_fallback(self):
        self.client.get_json.return_value = {"chart": {"error": {"code": "Unauthorized"}, "result": None}}
        with self.assertRaises(http.SourceError):
            self.fetch("yahoo", "GC=F")
        self.client.get_json.assert_called_once()

    def test_minimum_points_no_empty_export(self):
        for payload in ([], [kline(0)], [kline(0, None), kline(1, None)], [kline(4), kline(5)]):
            self.client.get_json.return_value = payload
            code, out, err = self.cli_fetch()
            self.assertEqual((code, out), (1, ""), err)
            self.assertFalse(self.output.exists())

    def test_request_bounds_before_network(self):
        for kwargs in ({"limit": 1}, {"limit": 1001}, {"limit": True}, {"limit": 2.5},
                       {"end": "2026-09-18T00:25:00Z"}, {"end": "2026-09-18T00:20:01Z"},
                       {"end": "2026-09-18T00:20:00"}, {"end": "1969-12-31T00:00:00Z"},
                       {"end": "bad"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.fetch(**kwargs)
        with self.assertRaises(ValueError):
            self.fetch("yahoo", "GC=F", end=(NOW - timedelta(days=61)).replace(second=0).isoformat())
        self.client.get_json.assert_not_called()

    def test_maximum_points_one_request_no_paging(self):
        self.client.get_json.return_value = [kline(i) for i in range(-996, 4)]
        output, report = self.fetch(limit=1000)
        self.assertEqual(len(output["prices"]), 1000)
        self.assertEqual(report["requested_slots"], 1000)
        self.client.get_json.assert_called_once()

    def test_oversized_response_points_and_bytes(self):
        for source, symbol, payload in (("binance", "BTCUSDT", [kline(i) for i in range(6)]),
                                        ("yahoo", "GC=F", chart(slots=tuple(range(6))))):
            self.client.get_json.return_value = payload
            with self.assertRaises(ValueError):
                self.fetch(source, symbol)
        payload = chart()
        payload["padding"] = "x" * prices.MAX_BYTES
        self.client.get_json.return_value = payload
        with self.assertRaisesRegex(ValueError, "2 MiB"):
            self.fetch("yahoo", "GC=F")

    def test_retrieval_provenance_required_not_invented(self):
        for value in ("", None, "bad", "2026-09-18T00:20:00", "2026-09-18T00:25:00Z"):
            self.client.retrieved_at = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.fetch()

    def test_explicit_end_offset_and_default_floor(self):
        output, report = self.fetch(end="2026-09-18T02:20:00+02:00")
        self.assertEqual(len(output["prices"]), 4)
        self.assertEqual(report["window"]["end_boundary_inclusive"], "2026-09-18T00:20:00+00:00")
        self.assertEqual(self.fetch()[0], output)

    def _stored_state(self):
        """Content of every registry table.

        Read-only analysis commands reopen the registry read-write, so the file
        can grow by a page when the schema statements are replayed. Asserting on
        stored rows tests the actual invariant, that analysis changes no data,
        and skips sqlite_sequence, which is SQLite's own autoincrement
        bookkeeping rather than registry content.
        """
        conn = sqlite3.connect(self.db)
        try:
            names = [row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
                if row[0] != "sqlite_sequence"]
            state = {}
            for name in names:
                rows = conn.execute(f'SELECT * FROM "{name}"').fetchall()
                state[name] = hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()
            return state
        finally:
            conn.close()

    def test_cli_project_and_events_roundtrip_all_assets(self):
        original = self._stored_state()
        for source, symbol in (("binance", "BTCUSDT"), ("yahoo", "GC=F"), ("yahoo", "CL=F"),
                               ("yahoo", "RB=F"), ("yahoo", "AAPL")):
            self.client.get_json.reset_mock()
            self.client.get_json.return_value = [kline(i) for i in range(4)] if source == "binance" else chart(symbol)
            code, out, err = self.cli_fetch("--force", source=source, symbol=symbol)
            self.assertEqual((code, err), (0, ""), out)
            self.client.get_json.assert_called_once()
            report = json.loads(out)
            self.assertEqual(report["output_sha256"], hashlib.sha256(self.output.read_bytes()).hexdigest())
            self.assertEqual(report["output_bytes"], self.output.stat().st_size)
            for command in ("project", "events"):
                code, out, err = self.invoke(command, str(self.eid), str(self.output))
                self.assertEqual((code, err), (0, ""), out)
                result = json.loads(out)
                if command == "project":
                    self.assertEqual(result["objective"]["status"], "not_achieved")
                    self.assertEqual(result["evaluation"]["eligible_predictions"], 3)
                else:
                    self.assertEqual(result["events"], [])
            self.assertEqual(self._stored_state(), original)

    def test_cli_invalid_pairs_urls_limits_and_missing_entity(self):
        for args in (("--limit", "1"), ("--limit", "1001"), ("--end", "bad"),
                     ("--url", "https://example.test")):
            self.assertNotEqual(self.cli_fetch(*args)[0], 0)
        for source, symbol in (("binance", "AAPL"), ("yahoo", "BTCUSDT"),
                               ("other", "BTCUSDT"), ("yahoo", "MSFT"),
                               ("yahoo", "https://example.test")):
            self.assertNotEqual(self.cli_fetch(source=source, symbol=symbol)[0], 0)
        self.assertEqual(self.cli_fetch(eid=999)[0], 1)
        self.client.get_json.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_cli_no_overwrite_force_and_parent_validation(self):
        self.output.write_bytes(b"existing data")
        self.assertEqual(self.cli_fetch()[0], 1)
        self.assertEqual(self.output.read_bytes(), b"existing data")
        self.assertEqual(self.cli_fetch(output=self.root / "missing" / "prices.json")[0], 1)
        self.assertEqual(self.cli_fetch("--force", output=self.root)[0], 1)
        self.client.get_json.assert_not_called()
        self.assertEqual(self.cli_fetch("--force")[0], 0)
        self.assertEqual(len(json.loads(self.output.read_text())["prices"]), 4)

    def test_cli_database_journals_symlinks_and_hardlinks_protected(self):
        original = self.db.read_bytes()
        protected = [self.db] + [Path(str(self.db) + suffix) for suffix in ("-wal", "-shm", "-journal")]
        symlink, hardlink = self.root / "alias.db", self.root / "hard.db"
        symlink.symlink_to(self.db)
        os.link(self.db, hardlink)
        protected.extend([symlink, hardlink])
        for output in protected:
            for force in ((), ("--force",)):
                with self.subTest(output=output, force=force):
                    code, _, err = self.cli_fetch(*force, output=output)
                    self.assertEqual(code, 1)
                    self.assertIn("registry", err)
        self.assertEqual(self.db.read_bytes(), original)
        self.client.get_json.assert_not_called()

    def test_cli_dangling_symlink_no_overwrite(self):
        self.output.symlink_to(self.root / "absent")
        self.assertEqual(self.cli_fetch()[0], 1)
        self.assertTrue(self.output.is_symlink())
        self.client.get_json.assert_not_called()

    def test_cli_race_and_atomic_failure_preserve_output(self):
        real_link = os.link

        def race(src, dst):
            Path(dst).write_bytes(b"racing writer")
            return real_link(src, dst)

        with patch("lele.cli.main.os.link", side_effect=race):
            self.assertEqual(self.cli_fetch()[0], 1)
        self.assertEqual(self.output.read_bytes(), b"racing writer")
        with patch("lele.cli.main.os.replace", side_effect=OSError("failed")):
            self.assertEqual(self.cli_fetch("--force")[0], 1)
        self.assertEqual(self.output.read_bytes(), b"racing writer")
        self.assertEqual(list(self.root.glob(".lele-*")), [])

    def test_cli_invalid_response_keeps_existing_file(self):
        self.output.write_bytes(b"original")
        self.client.get_json.return_value = [kline(0, "NaN"), kline(1)]
        self.assertEqual(self.cli_fetch("--force")[0], 1)
        self.assertEqual(self.output.read_bytes(), b"original")
        self.assertEqual(list(self.root.glob(".lele-*")), [])

    def test_sources_catalog_still_offline_and_separate(self):
        code, out, err = self.invoke("sources")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual([item["id"] for item in json.loads(out)], ["gleif", "fdic", "worldbank", "osfi", "sec"])
        self.client.get_json.assert_not_called()


class PriceHTTPTests(unittest.TestCase):
    def test_exact_endpoint_allowlist(self):
        for url in PRICE_ENDPOINTS.values():
            http.validate_url(url + "?interval=5m")
            for bad in (url + "/extra", url + "evil", url.replace("https:", "http:"),
                        url.replace(".com", ".com.evil.test"), url + "#fragment"):
                with self.subTest(url=bad), self.assertRaises(http.SourceError):
                    http.validate_url(bad)
        for url in ("https://api.binance.com/api/v3/order", "https://query1.finance.yahoo.com/v8/finance/chart/MSFT",
                    "https://query2.finance.yahoo.com/v8/finance/chart/AAPL",
                    "https://user@api.binance.com/api/v3/klines", "https://127.0.0.1/api/v3/klines"):
            with self.assertRaises(http.SourceError):
                http.validate_url(url)

    def test_price_http_byte_bound_no_cache_even_environment_override(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "LELE_CACHE_DIR": directory, "LELE_CACHE_TTL": "99999",
        }), patch.object(http, "build_opener") as opener, patch.object(http.time, "sleep"):
            response = Mock(status=200, headers={"Content-Length": str(prices.MAX_BYTES + 1)})
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            opener.return_value.open.return_value = response
            with registry.get_conn(str(Path(directory) / "registry.db")) as conn:
                eid = registry.upsert_entity(conn, "instrument", "Synthetic", key="test:price")
                with self.assertRaisesRegex(http.SourceError, "byte limit"):
                    prices.fetch_prices(conn, eid, "binance", "BTCUSDT")
            opener.return_value.open.assert_called_once()
            self.assertEqual(list(Path(directory).glob("public-api-*")), [])

    def test_denied_price_access_not_bypassed(self):
        for status in (401, 403, 451):
            with patch.object(http, "build_opener") as opener, patch.object(http.time, "sleep"):
                opener.return_value.open.side_effect = HTTPError(PRICE_ENDPOINTS["AAPL"], status, "denied", {}, io.BytesIO())
                client = http.HTTPClient(cache_dir=None, ttl=0)
                with self.assertRaises(http.SourceError):
                    client.get_json(PRICE_ENDPOINTS["AAPL"])
                opener.return_value.open.assert_called_once()


if __name__ == "__main__":
    unittest.main()
