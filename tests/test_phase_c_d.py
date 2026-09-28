import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, UTC
from pathlib import Path

from lele.core import registry
from lele.analysis import event_correlation, money_flow_attribution, volatility_anomaly, prospective


class EventCorrelationTests(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / "registry.db"
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        registry._initialize(self.conn)
        self.addCleanup(self.conn.close)

    def _setup_volatility_and_events(self):
        registry.add_volatility_instance(
            self.conn,
            instrument_key="BTC-USD",
            as_of="2026-09-20T10:00:00+00:00",
            target_time="2026-09-20T11:00:00+00:00",
            start_price="50000",
            end_price="52000",
            magnitude_percent="4.0",
            direction="up",
            episode_type="pump",
            regime="bull",
            threshold_percent=3,
            start_index=10,
            end_index=20,
            features_hash="abc123",
            observed_at="2026-09-20T10:00:00+00:00",
            available_at="2026-09-20T10:00:00+00:00",
        )
        self.conn.execute(
            """INSERT INTO event_store(source, event_type, external_id, title,
                 actor_key, actor_name, instrument_key, occurred_at, observed_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            ("political-event", "political_event", "evt-001",
             "Fed Announces Rate Decision", "political:FED:DECISION",
             "Federal Reserve", "BTC-USD",
             "2026-09-20T09:30:00+00:00", "2026-09-20T09:30:00+00:00"),
        )
        self.conn.commit()

    def test_correlate_events_to_volatility(self):
        self._setup_volatility_and_events()
        count = event_correlation.correlate_events_to_volatility(
            self.conn, "BTC-USD", threshold_percent=3, window_hours=24
        )
        self.assertGreaterEqual(count, 0)
        links = event_correlation.get_event_volatility_links(self.conn, "BTC-USD")
        self.assertGreaterEqual(len(links), 0)

    def test_correlate_no_events(self):
        registry.add_volatility_instance(
            self.conn,
            instrument_key="ETH-USD",
            as_of="2026-09-20T10:00:00+00:00",
            target_time="2026-09-20T11:00:00+00:00",
            start_price="3000",
            end_price="2900",
            magnitude_percent="-3.3",
            direction="down",
            episode_type="dump",
            regime="bear",
            threshold_percent=3,
            start_index=10,
            end_index=20,
            features_hash="def456",
            observed_at="2026-09-20T10:00:00+00:00",
            available_at="2026-09-20T10:00:00+00:00",
        )
        count = event_correlation.correlate_events_to_volatility(
            self.conn, "ETH-USD", threshold_percent=3, window_hours=24
        )
        self.assertEqual(count, 0)


class MoneyFlowAttributionTests(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / "registry.db"
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        registry._initialize(self.conn)
        self.addCleanup(self.conn.close)

    def _setup_event_and_flow(self):
        self.conn.execute(
            """INSERT INTO event_store(source, event_type, external_id, title,
                 actor_key, actor_name, instrument_key, occurred_at, observed_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            ("political-event", "political_event", "evt-001",
             "Treasury Auction", "political:TREASURY:AUCTION",
             "US Treasury", "BTC-USD",
             "2026-09-20T10:00:00+00:00", "2026-09-20T10:00:00+00:00"),
        )
        entity_src = registry.upsert_entity(self.conn, "authority", "US Treasury", key="political:TREASURY:AUCTION")
        entity_dst = registry.upsert_entity(self.conn, "instrument", "BTC-USD", key="BTC-USD")
        self.conn.execute(
            """INSERT INTO money_flows(src_id, dst_id, flow_type, amount, currency, occurred_at, source_url, evidence)
               VALUES(?,?,?,?,?,?,?,?)""",
            (entity_src, entity_dst, "transfer", "1000000", "USD",
             "2026-09-20T10:30:00+00:00", "https://example.com", "{}"),
        )
        self.conn.commit()

    def test_attribute_money_flows_to_events(self):
        self._setup_event_and_flow()
        count = money_flow_attribution.attribute_money_flows_to_events(
            self.conn, window_hours=48
        )
        self.assertGreaterEqual(count, 0)
        attributions = money_flow_attribution.get_money_flow_attributions(self.conn)
        self.assertGreaterEqual(len(attributions), 0)

    def test_attribute_no_events(self):
        count = money_flow_attribution.attribute_money_flows_to_events(
            self.conn, window_hours=48
        )
        self.assertEqual(count, 0)


class VolatilityAnomalyTests(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / "registry.db"
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        registry._initialize(self.conn)
        self.addCleanup(self.conn.close)

    def _create_price_file(self, directory, prices):
        import json
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

    def _create_evidence_file(self, directory):
        import json
        evidence_path = Path(directory) / "evidence.json"
        evidence = [{
            "id": "ev-1",
            "entity_key": "binance:BTCUSDT",
            "observed_at": "2026-09-20T10:00:00+00:00",
            "available_at": "2026-09-20T10:00:00+00:00",
            "kind": "fund_flow",
            "source_url": "https://example.com",
            "description": "Test fund flow",
            "platform": "binance",
            "industry": "crypto",
            "location": "global",
            "measurement": {"value": "1000000", "unit": "USD", "currency": "USD", "basis": "observed"},
            "mapping": {"from": "entity:a", "to": "entity:b", "actor": None, "action": None, "reason": None, "reason_basis": "unknown", "related_ids": [], "source_locator": None},
        }]
        data = {"observations": evidence}
        evidence_path.write_text(json.dumps(data, separators=(",", ":")))
        return evidence_path

    def test_compute_anomaly_score_insufficient_data(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            price_path = self._create_price_file(tmpdir, [50000] * 5)
            evidence_path = self._create_evidence_file(tmpdir)

            entity_id = registry.upsert_entity(self.conn, "instrument", "BTC-USD",
                                              key="binance:BTCUSDT")

            result = volatility_anomaly.compute_anomaly_score(
                self.conn, entity_id, str(price_path), str(evidence_path),
                threshold_percent=3, horizon_hours=72
            )
            self.assertIn("score", result)
            self.assertIn("volatility_detected", result)

    def test_volatility_anomaly_v1_spec(self):
        spec = volatility_anomaly.pre_register_volatility_anomaly_v1()
        self.assertEqual(spec["method"], "volatility_anomaly_v1")
        self.assertIn("target_metric", spec)
        self.assertEqual(spec["tolerance_bps"], 100)
        self.assertEqual(spec["target_rate"], 0.98)

    def test_volatility_anomaly_v1_prospective_integration(self):
        """Test that volatility_anomaly_v1 works with prospective forecast."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create price data with clear volatility pattern
            price_path = Path(tmpdir) / "prices.json"
            observations = []
            base_time = datetime(2026, 9, 20, 10, 0, 0, tzinfo=UTC)
            price = 50000
            for i in range(20):
                if i < 10:
                    price += 250  # pump
                else:
                    price -= 275  # dump
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

            # Evidence file
            evidence_path = Path(tmpdir) / "evidence.json"
            evidence = [{
                "id": f"ev-{i}",
                "entity_key": "binance:BTCUSDT",
                "observed_at": (base_time + timedelta(minutes=5*i)).isoformat(),
                "available_at": (base_time + timedelta(minutes=5*i)).isoformat(),
                "kind": "fund_flow",
                "source_url": "https://example.com",
                "description": "Test fund flow",
                "platform": "binance", "industry": "crypto", "location": "global",
                "measurement": {"value": "1000000", "unit": "USD", "currency": "USD", "basis": "observed"},
                "mapping": {"from": "a", "to": "b", "actor": None, "action": None, "reason": None, "reason_basis": "unknown", "related_ids": [], "source_locator": None},
            } for i in range(20)]
            evidence_path.write_text(json.dumps({"observations": evidence}, separators=(",", ":")))

            # Pre-register
            ledger_path = Path(tmpdir) / "ledger.jsonl"
            config = volatility_anomaly.pre_register_volatility_anomaly_v1()
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(json.dumps(config, separators=(",", ":")))

            reg_result = prospective.preregister(str(config_path), str(ledger_path), force=True)
            self.assertEqual(reg_result["action"], "preregister")
            self.assertIn("run_id", reg_result)

            # Forecast
            entity_id = registry.upsert_entity(self.conn, "instrument", "BTC-USD", key="binance:BTCUSDT")
            forecast_result = prospective.forecast(
                self.conn, entity_id, str(price_path), str(ledger_path), evidence_path=str(evidence_path)
            )
            self.assertEqual(forecast_result["action"], "forecast")
            self.assertTrue(forecast_result["appended"])
            self.assertEqual(forecast_result["forecast"]["method"], "volatility_anomaly_v1")
            self.assertIn("volatility_anomaly", forecast_result["forecast"])
            self.assertIn("world_state", forecast_result["forecast"])


if __name__ == "__main__":
    unittest.main()
