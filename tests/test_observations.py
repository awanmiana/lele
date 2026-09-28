import json
import tempfile
import unittest
from pathlib import Path

from lele.analysis import events, observations, worldstate
from lele.core import registry


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = str(self.root / "registry.db")
        self.path = self.root / "observations.json"

    def write(self, rows):
        self.path.write_text(json.dumps({"observations": rows}), encoding="utf-8")
        return str(self.path)

    def trade(self, **overrides):
        row = {
            "source": "sec-form4", "external_id": "trade-1", "kind": "insider_trade",
            "description": "Open-market sale", "instrument": "sec:AAPL", "actor": "person:jdoe",
            "amount": "1000", "unit": "shares", "currency": "USD", "basis": "observed",
            "observed_at": "2026-01-02T12:00:00+00:00",
            "available_at": "2026-01-02T12:05:00+00:00",
            "source_url": "https://www.sec.gov/Archives/aapl",
        }
        row.update(overrides)
        return row

    def test_import_projects_instrument_bound_observations(self):
        with registry.get_conn(self.db) as conn:
            instrument = registry.upsert_entity(conn, "instrument", "Apple Inc.", key="sec:AAPL")
            report = observations.import_observations(conn, self.write([self.trade()]))
            self.assertEqual((report["imported"], report["kinds"]), (1, ["insider_trade"]))
            rows = registry.list_observations(conn)
            self.assertEqual((rows[0]["instrument_key"], rows[0]["actor_key"]),
                             ("sec:AAPL", "person:jdoe"))
            self.assertEqual(len(registry.list_observations(conn, instrument)), 1)
            self.assertEqual(registry.list_observations(conn), registry.list_observations(conn, instrument))
            payload, meta = observations.evidence_payload(conn, instrument)
            self.assertEqual(meta["observations"], 1)
            evidence = payload["observations"][0]
            self.assertEqual((evidence["id"], evidence["kind"], evidence["entity_key"]),
                             ("sec-form4:trade-1", "insider_trade", "sec:AAPL"))
            self.assertEqual(evidence["measurement"],
                             {"value": "1000", "unit": "shares", "currency": "USD",
                              "basis": "observed"})
            self.assertIsNone(evidence["mapping"])

    def test_projection_feeds_worldstate(self):
        with registry.get_conn(self.db) as conn:
            instrument = registry.upsert_entity(conn, "instrument", "Apple Inc.", key="sec:AAPL")
            observations.import_observations(conn, self.write([self.trade()]))
            payload, _ = observations.evidence_payload(conn, instrument)
            projected = self.root / "evidence.json"
            projected.write_text(json.dumps(payload), encoding="utf-8")
            records, digest = events._evidence(str(projected), "sec:AAPL")
            self.assertEqual(len(records), 1)
            self.assertIsNotNone(digest)
            features = worldstate.build_features(records, records[0][1])
            self.assertEqual(features["kinds"], {"insider_trade": 1})
            self.assertEqual(features["direction_score"], 1)
            self.assertEqual(features["net_direction"], "up")

    def test_routed_flow_maps_actor_and_counterparty(self):
        with registry.get_conn(self.db) as conn:
            instrument = registry.upsert_entity(conn, "instrument", "Apple Inc.", key="sec:AAPL")
            registry.upsert_entity(conn, "company", "Counterparty", key="local:cp")
            row = self.trade(kind="fund_flow", external_id="flow-1", actor="sec:AAPL",
                             counterparty="local:cp", amount="250", unit="USD",
                             currency="USD", description="Documented transfer")
            observations.import_observations(conn, self.write([row]))
            payload, _ = observations.evidence_payload(conn, instrument)
            mapping = payload["observations"][0]["mapping"]
            self.assertEqual((mapping["from"], mapping["to"]), ("sec:AAPL", "local:cp"))
            self.assertIsNone(mapping["actor"])

    def test_decision_requires_action_and_reason_basis(self):
        with registry.get_conn(self.db) as conn:
            registry.upsert_entity(conn, "instrument", "Apple Inc.", key="sec:AAPL")
            with self.assertRaises(ValueError):
                observations.import_observations(conn, self.write(
                    [self.trade(kind="decision", action=None)]))
            with self.assertRaises(ValueError):
                observations.import_observations(conn, self.write(
                    [self.trade(kind="decision", action="buy", reason="a plan",
                                reason_basis="unknown")]))
            good = self.trade(kind="decision", action="buy", actor="person:jdoe",
                              reason="rule 10b5-1 plan", reason_basis="documented_mandate")
            self.assertEqual(observations.import_observations(conn, self.write([good]))["imported"], 1)

    def test_unknowns_and_rejections(self):
        with registry.get_conn(self.db) as conn:
            registry.upsert_entity(conn, "instrument", "Apple Inc.", key="sec:AAPL")
            base = self.trade()
            for change in (
                {"kind": "not_a_kind"},
                {"source_url": "http://insecure.example/x"},
                {"observed_at": "2026-01-02 12:00:00"},
                {"available_at": "2026-01-02T11:59:00+00:00"},
                {"amount": "abc"},
                {"unit": ""},
                {"actor": "x", "action": "buy"},
                {"reason": "because", "reason_basis": "unknown"},
            ):
                row = dict(base, external_id="bad-" + str(len(change)))
                row.update(change)
                with self.subTest(change=change), self.assertRaises(ValueError):
                    observations.import_observations(conn, self.write([row]))
            missing = dict(base)
            del missing["source_url"]
            with self.assertRaises(ValueError):
                observations.import_observations(conn, self.write([missing]))
            with self.assertRaises(ValueError):
                observations.import_observations(conn, self.write(
                    [dict(base, instrument="sec:MISSING")]))
            self.assertEqual(registry.list_observations(conn), [])

    def test_reimport_is_idempotent(self):
        with registry.get_conn(self.db) as conn:
            registry.upsert_entity(conn, "instrument", "Apple Inc.", key="sec:AAPL")
            path = self.write([self.trade()])
            observations.import_observations(conn, path)
            observations.import_observations(conn, path)
            self.assertEqual(len(registry.list_observations(conn)), 1)

    def test_registry_add_observation_validation(self):
        with registry.get_conn(self.db) as conn:
            registry.upsert_entity(conn, "instrument", "Apple Inc.", key="sec:AAPL")
            oid = registry.add_observation(
                conn, source="sec-form4", external_id="a", kind="insider_trade",
                observed_at="2026-01-02T12:00:00+00:00",
                available_at="2026-01-02T12:05:00+00:00", instrument_key="sec:AAPL",
                amount="5", unit="shares")
            self.assertIs(type(oid), int)
            with self.assertRaises(ValueError):
                registry.add_observation(conn, source="s", external_id="b", kind="k",
                                         observed_at="t", available_at="t", reason_basis="guess")
            with self.assertRaises(ValueError):
                registry.add_observation(conn, source="s", external_id="c", kind="k",
                                         observed_at="t", available_at="t", amount="1e9")
            with self.assertRaises(ValueError):
                registry.add_observation(conn, source="s", external_id="d", kind="k",
                                         observed_at="t", available_at="t", amount="1", unit="")
            with self.assertRaises(ValueError):
                registry.add_observation(conn, source="s", external_id="e", kind="decision",
                                         observed_at="t", available_at="t", action="buy",
                                         reason="text", reason_basis="unknown")
            with self.assertRaises(ValueError):
                registry.list_observations(conn, 999)


if __name__ == "__main__":
    unittest.main()
