import hashlib
import json
import re
from datetime import datetime, timedelta, UTC
from decimal import Context, Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext

from ..core import importer, registry
from . import engine

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_POINTS = 10_000
HORIZON_SECONDS = 300
METHOD = "five_minute_persistence_v1"
INSTRUMENT_FIELDS = {
    "entity_key": 512, "symbol": 128, "asset_class": 128, "venue": 256,
    "currency": 128, "unit": 128, "price_type": 128, "source_url": 4096,
}
ASSET_CLASSES = frozenset({"bitcoin", "crypto", "gold", "silver", "oil", "fuel", "commodity",
                          "stock", "etf", "index", "bond", "fx", "volatility"})
_PRICE_RE = re.compile(r"[+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_TIMESTAMP_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.0+)?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)


def _exact_object(value, fields, label):
    importer._object(value, fields, label)
    if value.keys() != fields:
        raise ValueError(f"{label} requires exactly {', '.join(sorted(fields))}")


def _timestamp(value):
    importer._text(value, "timestamp", 64, True)
    if not _TIMESTAMP_RE.fullmatch(value):
        raise ValueError("timestamp requires an aware ISO8601 datetime with whole seconds")
    try:
        timestamp = datetime.fromisoformat(value).astimezone(UTC)
    except (ValueError, OverflowError) as exc:
        raise ValueError("timestamp is outside the supported datetime range") from exc
    if timestamp.microsecond or timestamp.second or timestamp.minute % 5:
        raise ValueError("timestamp must be on a UTC five-minute boundary")
    return timestamp


def _price(value):
    importer._text(value, "price", 64, True)
    if not _PRICE_RE.fullmatch(value):
        raise ValueError("price must be a positive finite decimal string")
    try:
        price = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("invalid decimal price") from exc
    if not price.is_finite() or price <= 0 or not -12 <= price.adjusted() <= 12:
        raise ValueError("price must be positive with an adjusted exponent from -12 to 12")
    return price


def _load(path):
    with open(path, "rb") as stream:
        raw = stream.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("projection JSON file exceeds 2 MiB")
    try:
        payload = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=importer._pairs,
                             parse_constant=importer._constant)
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("invalid UTF-8 JSON or excessive nesting") from exc
    _exact_object(payload, {"instrument", "prices"}, "projection")
    instrument = payload["instrument"]
    _exact_object(instrument, set(INSTRUMENT_FIELDS), "instrument")
    for field, limit in INSTRUMENT_FIELDS.items():
        importer._text(instrument[field], f"instrument.{field}", limit, True)
    if instrument["asset_class"] not in ASSET_CLASSES:
        raise ValueError("unsupported instrument.asset_class")
    importer._provenance(instrument["source_url"], "instrument.source_url")
    prices = importer._array(payload["prices"], MAX_POINTS, "prices")
    if len(prices) < 2:
        raise ValueError("prices requires at least two observations")
    observations: list[tuple[datetime, Decimal]] = []
    for row in prices:
        _exact_object(row, {"timestamp", "price"}, "price observation")
        timestamp, price = _timestamp(row["timestamp"]), _price(row["price"])
        if observations and timestamp <= observations[-1][0]:
            raise ValueError("timestamps must be strictly increasing in UTC")
        observations.append((timestamp, price))
    try:
        target_time = observations[-1][0] + timedelta(seconds=HORIZON_SECONDS)
    except OverflowError as exc:
        raise ValueError("forecast target is outside the supported datetime range") from exc
    return instrument, prices[-1]["price"], observations, target_time, raw


def project(conn, entity_id: int, path) -> dict:
    if type(entity_id) is not int or not 1 <= entity_id <= 2**63 - 1:
        raise ValueError("entity id must be a positive SQLite integer")
    instrument, last_price, observations, target_time, raw = _load(path)
    entity = registry.get_entity(conn, entity_id)
    if entity is None:
        raise ValueError(f"entity {entity_id} not found")
    if entity["key"] != instrument["entity_key"]:
        raise ValueError("instrument.entity_key does not match the selected registry entity")
    eligible, skipped, hits = 0, 0, 0
    with localcontext(Context(prec=128, rounding=ROUND_HALF_EVEN)):
        total_error = Decimal(0)
        for previous, current in zip(observations, observations[1:]):
            if current[0] - previous[0] != timedelta(seconds=HORIZON_SECONDS):
                skipped += 1
                continue
            eligible += 1
            hits += previous[1] == current[1]
            total_error += abs(current[1] - previous[1])
        mae = str(total_error / eligible) if eligible else None
    rate = hits / eligible if eligible else None
    return {
        "method": METHOD,
        "instrument": instrument,
        "horizon_seconds": HORIZON_SECONDS,
        "forecast": {
            "as_of": observations[-1][0].isoformat(),
            "target_time": target_time.isoformat(),
            "price": last_price,
            "status": "baseline_unvalidated",
        },
        "evaluation": {
            "eligible_predictions": eligible,
            "skipped_gap_pairs": skipped,
            "exact_hits": hits,
            "exact_match_rate": rate,
            "mae": mae,
            "historical_target_met": eligible > 0 and hits * 10 >= eligible * 9,
            "target_rate": 0.9,
        },
        "objective": {
            "status": "not_achieved",
            "reason": "Historical, user-supplied fixtures cannot establish prospective 90% "
                      "exact-price accuracy across markets; no accuracy is guaranteed.",
        },
        "research": {
            "entity": dict(entity),
            "context": engine.finmap(conn, [entity_id]),
            "used_in_forecast": False,
            "note": "Current registry snapshot is excluded from historical prediction to avoid lookahead.",
        },
        "provenance": {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "input_bytes": len(raw),
            "validation": "syntax_only",
            "truth_verified": False,
        },
        "limitations": [
            "Historical as-of projection of the last supplied price, not a live or stale-free quote.",
            "Persistence only: every eligible historical prediction uses the immediately preceding price; "
            "no future observations or research context are forecast inputs.",
            "Only consecutive observations exactly 300 seconds apart are evaluated; gaps are never bridged.",
            "Exact decimal equality is not credible evidence of predictive skill; constant, stale or "
            "selected prices can inflate the historical hit rate.",
            "Input and provenance are syntactically validated, not independently truth verified; "
            "instrument identity beyond the registry key is user supplied.",
            "Decimal errors and sums are exact within input bounds; nonterminating MAE is rounded "
            "to 128 significant digits using half-even rounding, in the supplied price unit.",
            "No prospective validation, cross-market generalization, trading advice or trade execution.",
        ],
    }
