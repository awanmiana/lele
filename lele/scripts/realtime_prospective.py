#!/usr/bin/env python3
"""Real-time prospective evaluation runner, for a cron-driven forecast record.

A forecast is only evidence if it was written down before the outcome was
known. This runner exists so that happens on a schedule instead of by hand:
`forecast` issues a genuine prospective record, `settle` resolves the ones whose
target time has passed, and `score` reports what the ledger says.

    forecast : every 5 minutes
    settle   : on the minute after each target time
    score    : hourly

Usage:

    python3 -m lele.scripts.realtime_prospective --action preregister \
        --ledger /var/lib/lele/ledger.jsonl
    python3 -m lele.scripts.realtime_prospective --action forecast \
        --entity-id 2 --symbol BTCUSDT --ledger /var/lib/lele/ledger.jsonl
    python3 -m lele.scripts.realtime_prospective --action settle \
        --entity-id 2 --symbol BTCUSDT --ledger /var/lib/lele/ledger.jsonl
    python3 -m lele.scripts.realtime_prospective --action score \
        --ledger /var/lib/lele/ledger.jsonl

Three things this module previously got wrong, and must not get wrong again.

* It opened a hard-coded absolute path to one person's registry, ignoring both
  `LELE_DB` and `--db`, and failed confusingly for anyone else. The path is now
  a parameter with the documented default.
* It pre-registered a `target_rate` of 0.5 while the recorded standing objective
  is 0.9. A run that pre-registers a lower bar and then reports it as met has
  redefined the goal after seeing the data, which is the one thing the whole
  prospective harness exists to prevent. The target is now 0.9, stated once, and
  every required asset class must be present.
* It rewrote the ledger on `preregister` with `force=True`, discarding a run in
  progress. Re-preregistering is refused unless it is asked for explicitly.
"""
import argparse
import json
import os
import sys
from contextlib import suppress
import tempfile

from lele.analysis import prospective
from lele.core import db
from lele.core.constants import DB_PATH
from lele.fetchers import prices

# The objective as recorded in DEVELOPMENT_PLAN, in one place, and not a value
# any code path may lower.
TARGET_RATE = 0.9
TARGET_METRIC = "direction"
TOLERANCE_BPS = 100
REQUIRED_ASSET_CLASSES = ["bitcoin", "gold", "oil", "stock"]
MIN_SETTLED_PER_CLASS = 30
OBSERVATION_WINDOW_SECONDS = 300
METHODS = ("constructed_indicator_v1", "constructed_indicator_weighted_v1")


def _write_temp(payload, suffix=".json"):
    """Write a payload to a temporary file and return its path.

    The file is closed before it is returned because the consumer opens it
    again; a handle left open would be flushed at an unpredictable moment.
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False) as handle:
        json.dump(payload, handle)
        return handle.name


def _with_temp(payload, action):
    path = _write_temp(payload)
    try:
        return action(path)
    finally:
        with suppress(OSError):
            os.unlink(path)


def fetch_latest_prices(db_path, entity_id, symbol, limit=10):
    """Fetch a bounded window of recent closes for the entity."""
    with db.get_conn(db_path) as conn:
        return prices.fetch_prices(conn, entity_id, "binance", symbol, limit=limit, end=None)


def issue_forecast(db_path, entity_id, price_payload, ledger_path, method):
    """Record a prospective forecast whose target time is still in the future."""
    return _with_temp(price_payload,
                      lambda path: _forecast(db_path, entity_id, path, ledger_path))


def _forecast(db_path, entity_id, price_path, ledger_path):
    with db.get_read_conn(db_path) as conn:
        return prospective.forecast(conn, entity_id, price_path, ledger_path)


def _settle(db_path, entity_id, price_path, ledger_path):
    with db.get_read_conn(db_path) as conn:
        return prospective.settle(conn, entity_id, price_path, ledger_path)


def settle_forecast(db_path, entity_id, price_payload, ledger_path):
    """Resolve every forecast whose target time has passed."""
    return _with_temp(price_payload, lambda path: _settle(db_path, entity_id, path, ledger_path))


def score_ledger(ledger_path, run_id=None):
    """Report what the recorded forecasts actually achieved."""
    return prospective.score(ledger_path, run_id)


def preregister_config(method):
    """The pre-registration, built from the recorded objective.

    `required_asset_classes` lists every class the objective names. Naming only
    the one that happens to be wired would let the harness declare the target
    met on a single class, so the full list is required and the run stays
    `not_achieved` until each is present.
    """
    return {
        "method": method,
        "required_asset_classes": list(REQUIRED_ASSET_CLASSES),
        "target_metric": TARGET_METRIC,
        "tolerance_bps": TOLERANCE_BPS,
        "target_rate": TARGET_RATE,
        "min_settled_per_class": MIN_SETTLED_PER_CLASS,
        "observation_window_seconds": OBSERVATION_WINDOW_SECONDS,
    }


def preregister_method(ledger_path, method="constructed_indicator_weighted_v1", force=False):
    """Pre-register the method before any outcome is known."""
    return _with_temp(preregister_config(method),
                      lambda path: prospective.preregister(path, ledger_path, force))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Real-time prospective evaluation for constructed indicators")
    parser.add_argument("--db", default=DB_PATH, help="registry path (default: LELE_DB)")
    parser.add_argument("--entity-id", type=int, help="entity id in the registry")
    parser.add_argument("--symbol", help="instrument symbol, e.g. BTCUSDT")
    parser.add_argument("--ledger", required=True, help="path to the prospective ledger")
    parser.add_argument("--method", default=METHODS[1], choices=list(METHODS))
    parser.add_argument("--action", choices=["preregister", "forecast", "settle", "score"],
                        default="forecast")
    parser.add_argument("--run-id", help="specific run id to score")
    parser.add_argument("--force", action="store_true",
                        help="allow preregister to replace an existing run")
    args = parser.parse_args(argv)

    if args.action == "preregister":
        result = preregister_method(args.ledger, args.method, args.force)
    elif args.action == "score":
        result = score_ledger(args.ledger, args.run_id)
    else:
        if args.entity_id is None or args.symbol is None:
            parser.error(f"--action {args.action} requires --entity-id and --symbol")
        if args.run_id or args.force:
            parser.error(f"--action {args.action} accepts neither --run-id nor --force")
        window = 10 if args.action == "forecast" else 100
        payload, report = fetch_latest_prices(args.db, args.entity_id, args.symbol, limit=window)
        print(f"fetched {report['exported']} closes, newest "
              f"{payload['prices'][-1]['timestamp'] if payload['prices'] else 'none'}",
              file=sys.stderr)
        if args.action == "forecast":
            result = _with_temp(
                payload, lambda path: _forecast(args.db, args.entity_id, path, args.ledger))
        else:
            result = _with_temp(
                payload, lambda path: _settle(args.db, args.entity_id, path, args.ledger))
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
