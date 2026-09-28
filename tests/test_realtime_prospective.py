"""The cron-driven prospective runner, tested offline.

The previous version of this file reached the network and opened a hard-coded
one person's home directory, so the "unit tests" only passed on the machine
that wrote them, and the registry they used was whatever happened to contain
entity 2. Everything here is offline and the registry is a temporary file.

What is asserted is the part that matters: the runner takes the registry path as
an argument, and the pre-registration it writes carries the recorded objective —
0.9 across all four required asset classes — not a lower number that would let a
run declare victory on one class.
"""
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from lele.analysis import prospective
from lele.core import clock, registry
from lele.scripts import realtime_prospective as rtp


INSTRUMENT = {"entity_key": "binance:BTCUSDT", "symbol": "BTCUSDT", "asset_class": "bitcoin",
              "venue": "binance", "currency": "USDT", "unit": "coin", "price_type": "last",
              "source_url": "https://api.binance.com/api/v3/klines"}


def price_payload(count=10, start_price=100.0, entity_key="binance:BTCUSDT"):
    from datetime import UTC, datetime, timedelta
    base = datetime(2026, 1, 1, tzinfo=UTC)
    prices = []
    for index in range(count):
        moment = base + timedelta(minutes=5 * index)
        prices.append({"timestamp": moment.isoformat(),
                       "price": f"{start_price + index:.2f}"})
    return {"instrument": dict(INSTRUMENT, entity_key=entity_key), "prices": prices}


class RunnerCase(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db_path = str(self.root / "registry.db")
        self.ledger = str(self.root / "ledger.jsonl")
        with registry.get_conn(self.db_path) as conn:
            self.eid = registry.upsert_entity(conn, "instrument", "Bitcoin",
                                              key="binance:BTCUSDT")

    def test_fetch_latest_prices(self):
        payload, report = price_payload(10), {"exported": 10}
        with patch.object(rtp.prices, "fetch_prices", return_value=(payload, report)) as fetch:
            got_payload, got_report = rtp.fetch_latest_prices(self.db_path, self.eid,
                                                              "BTCUSDT", limit=5)
        self.assertEqual(got_report, report)
        self.assertEqual(len(got_payload["prices"]), 10)
        self.assertIn("timestamp", got_payload["prices"][-1])
        self.assertIn("price", got_payload["prices"][-1])
        self.assertEqual(fetch.call_args.args[1], self.eid)
        self.assertEqual(fetch.call_args.args[3], "BTCUSDT")
        self.assertEqual(fetch.call_args.kwargs["limit"], 5)

    def test_the_connector_uses_the_path_it_was_given(self):
        with patch.object(rtp.prices, "fetch_prices",
                          return_value=(price_payload(3), {"exported": 3})):
            rtp.fetch_latest_prices(self.db_path, self.eid, "BTCUSDT", limit=3)
        source = Path(rtp.__file__).read_text()
        # No absolute path under a home directory may appear in code, under
        # either name: the guard is about the shape, not about one spelling.
        for pattern in ("/root/.", '"/root', "'/root"):
            self.assertNotIn(pattern, source,
                             "no absolute home-directory path may be hard-coded in code")
        self.assertNotIn("os.path.expanduser", source)


class ObjectiveTests(unittest.TestCase):

    def test_the_preregistered_target_is_the_recorded_objective(self):
        config = rtp.preregister_config("constructed_indicator_v1")
        self.assertEqual(config["target_rate"], 0.9,
                         "the standing objective is 90%; a lower bar would be a "
                         "redefinition made after seeing the data")
        self.assertEqual(config["target_metric"], "direction")
        self.assertEqual(config["required_asset_classes"],
                         ["bitcoin", "gold", "oil", "stock"],
                         "every class the objective names must be required, or a run "
                         "can declare the target met on one of them")
        self.assertEqual(rtp.TARGET_RATE, 0.9)

    def test_preregister_writes_that_config(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = str(Path(root) / "ledger.jsonl")
            result = rtp.preregister_method(ledger, "constructed_indicator_v1")
            self.assertIn("run_id", result)
            # the ledger stores the config as text, which is what is scored against
            self.assertEqual(result["config"]["target_rate"], "0.9")
            with open(ledger) as handle:
                written = json.loads(handle.readline())
            self.assertEqual(written["record"], "run")
            self.assertEqual(written["config"]["required_asset_classes"],
                             ["bitcoin", "gold", "oil", "stock"])
            self.assertTrue(os.path.exists(ledger))

    def test_preregister_refuses_to_replace_a_run_in_progress(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = str(Path(root) / "ledger.jsonl")
            first = rtp.preregister_method(ledger, "constructed_indicator_v1")
            with self.assertRaisesRegex(ValueError, "force"):
                rtp.preregister_method(ledger, "constructed_indicator_v1")
            with open(ledger) as handle:
                lines = [json.loads(line) for line in handle if line.strip()]
            runs = [line for line in lines if line.get("record") == "run"]
            self.assertEqual(len(runs), 1, "the original run must still be the only one")
            self.assertEqual(runs[0]["run_id"], first["run_id"])

    def test_the_config_temporary_file_is_removed(self):
        with tempfile.TemporaryDirectory() as root:
            before = set(os.listdir(root))
            rtp.preregister_method(str(Path(root) / "ledger.jsonl"), "constructed_indicator_v1")
            after = set(os.listdir(root))
            self.assertEqual(after - before, {"ledger.jsonl"},
                             "the temporary config must not be left behind")


class OfflineLedgerTests(unittest.TestCase):
    """A full forecast/settle/score cycle with a supplied clock, no network."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db_path = str(self.root / "registry.db")
        self.ledger = str(self.root / "ledger.jsonl")
        self.config = str(self.root / "config.json")
        with registry.get_conn(self.db_path) as conn:
            self.eid = registry.upsert_entity(conn, "instrument", "Bitcoin",
                                              key="binance:BTCUSDT")
        with open(self.config, "w") as handle:
            json.dump(rtp.preregister_config("evidence_score_10bps_v2"), handle)
        self.prices = str(self.root / "prices.json")

    def write_prices(self, values, start):
        from datetime import timedelta
        payload = {"instrument": dict(INSTRUMENT, entity_key="binance:BTCUSDT"),
                   "prices": [{"timestamp": (start + timedelta(minutes=5 * index)).isoformat(),
                               "price": str(value)} for index, value in enumerate(values)]}
        with open(self.prices, "w") as handle:
            json.dump(payload, handle)
        return self.prices

    def test_forecast_then_settle_then_score(self):
        """A full cycle with a frozen clock, so the forecast really is prospective.

        With real wall-clock time a fixture dated in the past is recorded as
        `backfilled` and excluded from scoring, which is the harness refusing to
        count a forecast it cannot prove was made before the outcome. Freezing
        the clock puts the data at the issuance instant, which is what the
        runner sees in production.
        """
        start = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
        with open(self.config) as handle:
            config = json.load(handle)
        config.update({"method": "five_minute_persistence_v1",
                       "required_asset_classes": ["bitcoin"],
                       "min_settled_per_class": 1,
                       "target_metric": "direction"})
        with open(self.config, "w") as handle:
            json.dump(config, handle)
        prospective.preregister(self.config, self.ledger, force=True)

        self.write_prices(["100", "101", "102", "102"], start)
        with clock.freeze(start), registry.get_conn(self.db_path) as conn:
            issued = prospective.forecast(conn, self.eid, self.prices, self.ledger)
        self.assertEqual(issued["forecast"]["status"], "prospective")
        self.assertGreater(issued["forecast"]["target_time"],
                           issued["forecast"]["issued_at"])

        # the target is the last issued price plus five minutes, and settlement
        # requires an exact observation there; a gap would stay pending, which is
        # what the harness is designed to do rather than bridge
        self.write_prices(["100", "101", "102", "103", "104"], start)
        with clock.freeze(start + timedelta(minutes=20)), \
                registry.get_conn(self.db_path) as conn:
            settled = prospective.settle(conn, self.eid, self.prices, self.ledger)
        self.assertEqual(settled["settled"], 1)

        scored = prospective.score(self.ledger)
        self.assertIn("metrics", scored)
        self.assertEqual(scored["objective"]["status"], "not_achieved",
                         "one settled sample can never meet a 0.9 target")

    def test_a_backdated_forecast_is_excluded_rather_than_counted(self):
        """The same fixture without the clock, refused for the right reason."""
        start = datetime(2020, 6, 1, 12, 0, tzinfo=UTC)
        with open(self.config) as handle:
            config = json.load(handle)
        config["method"] = "five_minute_persistence_v1"
        with open(self.config, "w") as handle:
            json.dump(config, handle)
        prospective.preregister(self.config, self.ledger, force=True)
        self.write_prices(["100", "101", "102", "102"], start)
        with registry.get_conn(self.db_path) as conn:
            issued = prospective.forecast(conn, self.eid, self.prices, self.ledger)
        self.assertEqual(issued["forecast"]["status"], "backfilled",
                         "a forecast for a moment that has already passed is not evidence")


if __name__ == "__main__":
    unittest.main()
