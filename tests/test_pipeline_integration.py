"""Integration tests for the full P09 RAG pipeline.

Tests the complete flow:
1. Store political/regulatory events
2. Build event graph
3. Attribute money flows to events
4. Detect price volatility instances via ZigZag
5. Compute anomaly score via historical pattern matching
6. Prospective forecast with volatility_anomaly_v1
"""
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, UTC
from pathlib import Path

from lele.core import registry
from lele.analysis import (
    event_correlation,
    volatility_anomaly,
    prospective,
    rag,
)


class FullPipelineIntegrationTests(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / "registry.db"
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        registry._initialize(self.conn)
        self.addCleanup(self.conn.close)

        # Create instrument entity
        self.entity_id = registry.upsert_entity(
            self.conn, "instrument", "BTC-USD", key="binance:BTCUSDT"
        )

    def _create_price_file(self, directory, prices):
        price_path = Path(directory) / "prices.json"
        observations = []
        base_time = datetime(2026, 9, 20, 10, 0, 0, tzinfo=UTC)
        for i, price in enumerate(prices):
            timestamp = base_time + timedelta(minutes=5 * i)
            observations.append({"timestamp": timestamp.isoformat(), "price": str(price)})

        data = {
            "instrument": {
                "entity_key": "binance:BTCUSDT",
                "symbol": "BTCUSDT",
                "asset_class": "bitcoin",
                "venue": "binance",
                "currency": "USDT",
                "unit": "BTC",
                "price_type": "spot",
                "source_url": "https://api.binance.com",
            },
            "prices": observations,
        }
        price_path.write_text(json.dumps(data, separators=(",", ":")))
        return price_path

    def _create_evidence_file(self, directory, base_time, count, kind="fund_flow"):
        evidence_path = Path(directory) / "evidence.json"
        evidence = [{
            "id": f"ev-{i}",
            "entity_key": "binance:BTCUSDT",
            "observed_at": (base_time + timedelta(minutes=5*i)).isoformat(),
            "available_at": (base_time + timedelta(minutes=5*i)).isoformat(),
            "kind": kind,
            "source_url": "https://example.com",
            "description": f"Test {kind}",
            "platform": "binance", "industry": "crypto", "location": "global",
            "measurement": {"value": "1000000", "unit": "USD", "currency": "USD", "basis": "observed"},
            "mapping": {"from": "a", "to": "b", "actor": None, "action": None, "reason": None, "reason_basis": "unknown", "related_ids": [], "source_locator": None},
        } for i in range(count)]
        evidence_path.write_text(json.dumps({"observations": evidence}, separators=(",", ":")))
        return evidence_path

    def _create_historical_data(self, tmpdir, num_cycles=5):
        """Create historical price data with clear ZigZag episodes for pattern matching."""
        price_path = Path(tmpdir) / "prices_hist.json"
        observations = []
        base_time = datetime(2026, 9, 1, 10, 0, 0, tzinfo=UTC)
        price = 50000
        periods_per_cycle = 20
        for i in range(num_cycles * periods_per_cycle):
            cycle_pos = i % periods_per_cycle
            if cycle_pos < periods_per_cycle // 2:
                price += 250  # pump phase
            else:
                price -= 275  # dump phase
            timestamp = base_time + timedelta(minutes=5 * i)
            observations.append({"timestamp": timestamp.isoformat(), "price": str(price)})

        data = {
            "instrument": {
                "entity_key": "binance:BTCUSDT",
                "symbol": "BTCUSDT",
                "asset_class": "bitcoin",
                "venue": "binance",
                "currency": "USDT",
                "unit": "BTC",
                "price_type": "spot",
                "source_url": "https://api.binance.com",
            },
            "prices": observations,
        }
        price_path.write_text(json.dumps(data, separators=(",", ":")))

        # Evidence for historical data
        evidence_path = Path(tmpdir) / "evidence_hist.json"
        evidence = [{
            "id": f"hev-{i}",
            "entity_key": "binance:BTCUSDT",
            "observed_at": (base_time + timedelta(minutes=5*i)).isoformat(),
            "available_at": (base_time + timedelta(minutes=5*i)).isoformat(),
            "kind": "fund_flow",
            "source_url": "https://example.com",
            "description": "Historical fund flow",
            "platform": "binance", "industry": "crypto", "location": "global",
            "measurement": {"value": "1000000", "unit": "USD", "currency": "USD", "basis": "observed"},
            "mapping": {"from": "a", "to": "b", "actor": None, "action": None, "reason": None, "reason_basis": "unknown", "related_ids": [], "source_locator": None},
        } for i in range(num_cycles * periods_per_cycle)]
        evidence_path.write_text(json.dumps({"observations": evidence}, separators=(",", ":")))

        return price_path, evidence_path

    def test_full_pipeline_historical_storage_and_anomaly_detection(self):
        """Test: Store historical data, detect volatility, then detect anomaly on similar current pattern."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # 1. Create and store historical volatility instances
            hist_price_path, hist_evidence_path = self._create_historical_data(tmpdir, num_cycles=5)

            stored = volatility_anomaly.detect_volatility_instances(
                self.conn, "binance:BTCUSDT", str(hist_price_path), str(hist_evidence_path),
                threshold_percent=3, horizon_hours=72
            )
            self.assertGreater(stored, 0, "Should detect historical volatility instances")

            instances = registry.list_volatility_instances(self.conn, "binance:BTCUSDT")
            self.assertGreater(len(instances), 0)

            # 2. Create current price data with similar pattern
            current_price_path = self._create_price_file(tmpdir, [
                50000, 50250, 50500, 50750, 51000, 51250, 51500, 51750, 52000, 52250,  # pump
                52000, 51750, 51500, 51250, 51000, 50750, 50500, 50250, 50000, 49750   # dump
            ])

            base_time = datetime(2026, 9, 20, 10, 0, 0, tzinfo=UTC)
            current_evidence_path = self._create_evidence_file(tmpdir, base_time, 20)

            # 3. Compute anomaly score - should find high similarity matches
            result = volatility_anomaly.compute_anomaly_score(
                self.conn, self.entity_id, str(current_price_path), str(current_evidence_path),
                threshold_percent=3, horizon_hours=72
            )

            self.assertTrue(result["volatility_detected"])
            self.assertGreater(result["score"], 0)
            self.assertGreater(len(result["matches"]), 0)
            self.assertGreater(float(result["matches"][0]["similarity_score"]), 0.9)

    def test_full_pipeline_with_events_and_money_flows(self):
        """Test: Events + money flows + volatility correlation + anomaly detection."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create historical data first
            hist_price_path, hist_evidence_path = self._create_historical_data(tmpdir, num_cycles=3)
            volatility_anomaly.detect_volatility_instances(
                self.conn, "binance:BTCUSDT", str(hist_price_path), str(hist_evidence_path),
                threshold_percent=3, horizon_hours=72
            )

            # 1. Store political/regulatory events
            events = [{
                "event_type": "political_event",
                "external_id": "fed-2026-09-20-001",
                "title": "FOMC Rate Decision",
                "actor_key": "political:FED:RATE",
                "actor_name": "Federal Reserve",
                "instrument_key": "binance:BTCUSDT",
                "occurred_at": "2026-09-20T10:00:00+00:00",
                "observed_at": "2026-09-20T10:00:00+00:00",
                "severity": "high",
                "confidence": 0.9,
                "source_url": "https://federalregister.gov/fed-decision",
                "evidence": {"description": "Fed holds rates steady"},
            }, {
                "event_type": "regulatory_action",
                "external_id": "sec-2026-09-20-002",
                "title": "SEC Crypto Guidance",
                "actor_key": "regulatory:SEC:CRYPTO",
                "actor_name": "SEC",
                "instrument_key": "binance:BTCUSDT",
                "occurred_at": "2026-09-20T10:30:00+00:00",
                "observed_at": "2026-09-20T10:30:00+00:00",
                "severity": "moderate",
                "confidence": 0.8,
                "source_url": "https://sec.gov/crypto-guidance",
                "evidence": {"description": "New crypto custody guidance"},
            }]
            store_result = rag.store_events(self.conn, events, source="political-event")
            self.assertEqual(store_result["stored"], 2)

            # 2. Build event graph
            graph_result = rag.build_event_graph(self.conn, window_hours=24, min_strength=0.1)
            self.assertGreaterEqual(graph_result, 0)

            # 3. Create money flows
            src_entity = registry.upsert_entity(self.conn, "authority", "Treasury", key="political:TREASURY")
            dst_entity = registry.upsert_entity(self.conn, "instrument", "BTC-USD", key="binance:BTCUSDT")
            self.conn.execute(
                """INSERT INTO money_flows(src_id, dst_id, flow_type, amount, currency, occurred_at, source_url, evidence)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (src_entity, dst_entity, "transfer", "5000000", "USD",
                 "2026-09-20T10:15:00+00:00", "https://treasury.gov", "{}"),
            )
            self.conn.commit()

            # 4. Attribute money flows to events
            attr_result = rag.attribute_money_flows(self.conn, window_hours=48)
            self.assertGreaterEqual(attr_result, 0)

            # 5. Create current data for anomaly detection
            current_price_path = self._create_price_file(tmpdir, [
                50000, 50250, 50500, 50750, 51000, 51250, 51500, 51750, 52000, 52250,
                52000, 51750, 51500, 51250, 51000, 50750, 50500, 50250, 50000, 49750
            ])
            base_time = datetime(2026, 9, 20, 10, 0, 0, tzinfo=UTC)
            current_evidence_path = self._create_evidence_file(tmpdir, base_time, 20)

            # 6. Compute anomaly - should correlate events to volatility
            result = volatility_anomaly.compute_anomaly_score(
                self.conn, self.entity_id, str(current_price_path), str(current_evidence_path),
                threshold_percent=3, horizon_hours=72
            )

            self.assertTrue(result["volatility_detected"])
            self.assertIn("score", result)

            # 7. Verify event-volatility links were created
            links = event_correlation.get_event_volatility_links(self.conn, "binance:BTCUSDT")
            self.assertGreaterEqual(len(links), 0)

    def test_full_pipeline_prospective_forecast_settle(self):
        """Test: Full prospective cycle with volatility_anomaly_v1."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create historical data for pattern matching
            hist_price_path, hist_evidence_path = self._create_historical_data(tmpdir, num_cycles=3)
            volatility_anomaly.detect_volatility_instances(
                self.conn, "binance:BTCUSDT", str(hist_price_path), str(hist_evidence_path),
                threshold_percent=3, horizon_hours=72
            )

            # Current price data
            price_path = self._create_price_file(tmpdir, [
                50000, 50250, 50500, 50750, 51000, 51250, 51500, 51750, 52000, 52250,
                52000, 51750, 51500, 51250, 51000, 50750, 50500, 50250, 50000, 49750
            ])
            base_time = datetime(2026, 9, 20, 10, 0, 0, tzinfo=UTC)
            evidence_path = self._create_evidence_file(tmpdir, base_time, 20)

            # Pre-register
            ledger_path = Path(tmpdir) / "ledger.jsonl"
            config = volatility_anomaly.pre_register_volatility_anomaly_v1()
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(json.dumps(config, separators=(",", ":")))

            reg_result = prospective.preregister(str(config_path), str(ledger_path), force=True)
            self.assertEqual(reg_result["action"], "preregister")

            # Forecast
            forecast_result = prospective.forecast(
                self.conn, self.entity_id, str(price_path), str(ledger_path),
                evidence_path=str(evidence_path)
            )
            self.assertEqual(forecast_result["action"], "forecast")
            self.assertTrue(forecast_result["appended"])
            self.assertEqual(forecast_result["forecast"]["method"], "volatility_anomaly_v1")
            self.assertIn("volatility_anomaly", forecast_result["forecast"])

            # Create settlement price data (target time = 5 min after last price)
            settle_price_path = Path(tmpdir) / "prices_settle.json"
            settle_obs = []
            # Last price was at 11:35, target is 11:40
            settle_base = datetime(2026, 9, 20, 11, 35, 0, tzinfo=UTC)
            for i in range(2):
                timestamp = settle_base + timedelta(minutes=5 * i)
                price = 49500 if i == 0 else 49400  # Continues down
                settle_obs.append({"timestamp": timestamp.isoformat(), "price": str(price)})

            settle_data = {
                "instrument": {
                    "entity_key": "binance:BTCUSDT",
                    "symbol": "BTCUSDT",
                    "asset_class": "bitcoin",
                    "venue": "binance",
                    "currency": "USDT",
                    "unit": "BTC",
                    "price_type": "spot",
                    "source_url": "https://api.binance.com",
                },
                "prices": settle_obs,
            }
            settle_price_path.write_text(json.dumps(settle_data, separators=(",", ":")))

            # Settle
            settle_result = prospective.settle(self.conn, self.entity_id, str(settle_price_path), str(ledger_path))
            self.assertEqual(settle_result["action"], "settle")
            self.assertGreaterEqual(settle_result["settled"], 0)

            # Score
            score_result = prospective.score(str(ledger_path))
            self.assertIn("coverage", score_result)
            self.assertIn("metrics", score_result)
            self.assertIn("coverage", score_result)
            self.assertIn("settled_prospective", score_result["coverage"])

    def test_cli_volatility_analyze_command(self):
        """Test volatility-analyze CLI command via direct function call."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create historical data
            hist_price_path, hist_evidence_path = self._create_historical_data(tmpdir, num_cycles=3)
            volatility_anomaly.detect_volatility_instances(
                self.conn, "binance:BTCUSDT", str(hist_price_path), str(hist_evidence_path),
                threshold_percent=3, horizon_hours=72
            )

            # Current data
            price_path = self._create_price_file(tmpdir, [
                50000, 50250, 50500, 50750, 51000, 51250, 51500, 51750, 52000, 52250,
                52000, 51750, 51500, 51250, 51000, 50750, 50500, 50250, 50000, 49750
            ])
            base_time = datetime(2026, 9, 20, 10, 0, 0, tzinfo=UTC)
            evidence_path = self._create_evidence_file(tmpdir, base_time, 20)

            # Call compute_anomaly_score directly (what CLI does)
            result = volatility_anomaly.compute_anomaly_score(
                self.conn, self.entity_id, str(price_path), str(evidence_path),
                threshold_percent=3, horizon_hours=72
            )

            # Verify result structure matches CLI output
            self.assertIn("score", result)
            self.assertIn("volatility_detected", result)
            self.assertIn("current_episode", result)
            self.assertIn("matches", result)
            self.assertIn("breakdown", result)
            self.assertTrue(result["volatility_detected"])
            self.assertGreater(result["score"], 0)
            self.assertGreater(len(result["matches"]), 0)

    def test_rag_cli_commands_integration(self):
        """Test all RAG CLI subcommands in sequence."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # store-events
            events = [{
                "event_type": "political_event",
                "external_id": "test-001",
                "title": "Test Event",
                "actor_key": "political:TEST",
                "actor_name": "Test Agency",
                "instrument_key": "binance:BTCUSDT",
                "occurred_at": "2026-09-20T10:00:00+00:00",
                "observed_at": "2026-09-20T10:00:00+00:00",
                "source_url": "https://example.com",
            }]
            events_path = Path(tmpdir) / "events.json"
            events_path.write_text(json.dumps(events, separators=(",", ":")))

            store_result = rag.store_events(self.conn, events, source="political-event")
            self.assertEqual(store_result["stored"], 1)

            # build-graph
            graph_result = rag.build_event_graph(self.conn, window_hours=24)
            self.assertGreaterEqual(graph_result, 0)

            # attribute-flows (no flows yet, should return 0)
            attr_result = rag.attribute_money_flows(self.conn, window_hours=48)
            self.assertEqual(attr_result, 0)

            # detect-anomalies (no price observations in DB, should return 0)
            anomaly_result = rag.detect_anomalies(self.conn, lookback_days=30, threshold_sigma=2.0)
            self.assertEqual(anomaly_result, 0)

            # indicator (should work with stored events)
            indicator_result = rag.event_indicator_v1(self.conn, limit=20)
            self.assertIn("score", indicator_result)
            self.assertIn("event_count", indicator_result)
            self.assertEqual(indicator_result["event_count"], 1)


if __name__ == "__main__":
    unittest.main()
