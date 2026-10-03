"""Store OHLC price history the user exported from a source they may use.

`fetchers.history` reaches years back for Binance spot pairs and nothing else, so a
listed equity or an exchange-traded commodity has no stored bars and therefore no
measured window: `explain` can report an issuer's filings, insider trades and
holdings, but there is no price to measure any of them against. That was the
largest remaining gap in the tool, and it is a data gap rather than a code one.

It cannot be closed by picking a provider here. Every free no-key daily OHLC
source either restricts automated use of its output or, in Stooq's case as
observed on 2026-09-29, answers every request -- including the plain CSV download
-- with a JavaScript proof-of-work browser check whose `/__verify` step exists to
keep non-browser clients out. Passing it would be defeating an access control, so
Stooq is recorded as blocked rather than integrated. Everything with clear terms
asks for an API key, which this project treats as blocked until a real one
exists. So the provider is the user.

This reads a bounded JSON file they exported from a source they have the right to
use, and stores it exactly as `fetchers.history` stores klines: idempotent on
(instrument, interval, open time), so re-running refines rather than duplicates.

What is preserved, because a bar without its provenance is not evidence: the
symbol, venue, currency and asset class; the adjustment basis, which is required
rather than guessed, since a split repairs nothing and silently corrupts every
return that crosses it; the source reference and retrieval time; and the cadence
and gap count measured from the imported bars rather than assumed from the
interval label.

Nothing here is verified. The provenance records what the file claims, and the
absence of verification is stated in the report rather than left to be inferred.
"""
import hashlib
import json
import math
import re
from datetime import datetime, timedelta, UTC
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from ..core import clock, importer, registry

METHOD = "user_supplied_ohlc_v1"
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_BARS = 20000
INTERVALS = {"1d": 86400, "4h": 14400, "1h": 3600}
ADJUSTMENTS = ("adjusted", "unadjusted", "unknown")
INSTRUMENT_FIELDS = {"symbol", "venue", "currency", "asset_class", "adjustment",
                     "rights_basis", "notes"}
BAR_FIELDS = {"open_time", "close_time", "open", "high", "low", "close", "volume",
              "quote_volume", "trades"}
REQUIRED_INSTRUMENT = ("symbol", "currency", "adjustment")
# Optional instrument fields and what their absence means. Reported as `unknown`
# rather than filled in, because a venue or an asset class that is guessed is
# indistinguishable from a measured one downstream. Free-text notes are accepted
# but not listed here: an absent note is not an unknown, it is simply absent.
OPTIONAL_INSTRUMENT = {"venue": "the trading venue or market",
                       "asset_class": "the asset class such as equity or commodity",
                       "rights_basis": "the basis on which the export may be used"}
INSTANT = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})"
)


def _now():
    return clock.now()


def _instant(value, label):
    """An aware ISO8601 instant, canonicalized to UTC.

    Canonicalized because `price_bars` is keyed on the open time as text: the same
    session written as `09:30-04:00` and as `13:30Z` would otherwise be two rows
    for one bar, and the idempotent re-import would silently duplicate instead of
    refine.
    """
    if not isinstance(value, str) or not INSTANT.fullmatch(value):
        raise ValueError(f"{label} must be an aware ISO8601 instant such as 2026-09-25T13:30:00Z")
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"{label} must carry a UTC offset")
    return moment.astimezone(UTC).isoformat()


def _decimal(value, label, required=True):
    """A finite decimal as text, accepting a JSON number or a numeric string."""
    if isinstance(value, bool):
        raise ValueError(f"{label} must be numeric")
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{label} must be finite")
        value = repr(value)
    if not isinstance(value, str) or not value.strip():
        if required:
            raise ValueError(f"{label} is required")
        return ""
    text = value.strip()
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"{label} must be a finite decimal string") from exc
    if not number.is_finite():
        raise ValueError(f"{label} must be finite")
    return text


def _bar(value, interval_seconds, newest_allowed):
    """Validate one bar, raising rather than dropping it.

    Every bar is checked before anything is written, so a file with one bad row
    stores nothing rather than storing a silently short series.
    """
    importer._object(value, BAR_FIELDS, "bar")
    for field in ("open", "high", "low", "close", "volume"):
        if field not in value:
            raise ValueError(f"bar requires {field}; volume has no unknown representation")
    open_time = _instant(value["open_time"], "bar open_time")
    opened = datetime.fromisoformat(open_time)
    derived = "close_time" not in value
    if derived:
        close_time = (opened + timedelta(seconds=interval_seconds)).isoformat()
    else:
        close_time = _instant(value["close_time"], "bar close_time")
        if datetime.fromisoformat(close_time) <= opened:
            raise ValueError("bar close_time must be after open_time")
    prices = {name: _decimal(value[name], f"bar {name}") for name in
              ("open", "high", "low", "close")}
    volume = _decimal(value["volume"], "bar volume")
    quote_volume = _decimal(value.get("quote_volume", ""), "bar quote_volume", required=False)
    trades = value.get("trades", 0)
    if type(trades) is not int or not 0 <= trades <= 2**63 - 1:
        raise ValueError("bar trades must be a nonnegative integer")
    # The same ordering rules `registry.add_price_bar` enforces, checked here so
    # the whole file is validated before the first write.
    numbers = {name: Decimal(text) for name, text in prices.items()}
    numbers["volume"] = Decimal(volume)
    if min(numbers.values()) <= 0:
        raise ValueError("bar open, high, low, close and volume must be positive")
    if numbers["high"] < numbers["low"]:
        raise ValueError("bar high must not be below low")
    if not numbers["low"] <= numbers["open"] <= numbers["high"]:
        raise ValueError("bar open must lie within the low..high range")
    if not numbers["low"] <= numbers["close"] <= numbers["high"]:
        raise ValueError("bar close must lie within the low..high range")
    if numbers["volume"] < 0 or (quote_volume and Decimal(quote_volume) < 0):
        raise ValueError("bar volume must not be negative")
    if close_time > newest_allowed:
        raise ValueError("bar close_time is in the future; a bar that has not closed is not "
                         "a measurement")
    return {"open_time": open_time, "close_time": close_time, **prices,
            "volume": volume, "quote_volume": quote_volume, "trades": trades,
            "close_time_derived": derived}


def validate_request(interval, now):
    if interval not in INTERVALS:
        raise ValueError(f"history interval must be one of {', '.join(INTERVALS)}")
    if not isinstance(now, datetime):
        raise ValueError("now must be a datetime")


def _instrument(value):
    importer._object(value, INSTRUMENT_FIELDS, "instrument")
    for field in REQUIRED_INSTRUMENT:
        if field not in value:
            raise ValueError(f"instrument requires {field}")
    result = {}
    for field, limit in (("symbol", 64), ("venue", 128), ("currency", 16),
                         ("asset_class", 64), ("adjustment", 16),
                         ("rights_basis", 256), ("notes", 4096)):
        if field not in value:
            result[field] = ""
            continue
        if field == "adjustment" and value[field] not in ADJUSTMENTS:
            raise ValueError(f"instrument adjustment must be one of {', '.join(ADJUSTMENTS)}")
        importer._text(value[field], f"instrument {field}", limit, required=field in REQUIRED_INSTRUMENT)
        result[field] = value[field]
    return result


def _load(path):
    with open(path, "rb") as stream:
        raw = stream.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("price history file exceeds 10 MiB")
    try:
        payload = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=importer._pairs,
                             parse_constant=importer._constant)
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("Invalid UTF-8 JSON or excessive nesting") from exc
    importer._object(payload, {"instrument", "source", "source_url", "retrieved_at", "bars"},
                     "payload")
    for field in ("instrument", "source", "source_url", "retrieved_at", "bars"):
        if field not in payload:
            raise ValueError(f"payload requires {field}")
    importer._text(payload["source"], "source", 64, True)
    importer._provenance(payload["source_url"], "source_url")
    _instant(payload["retrieved_at"], "retrieved_at")
    bars = importer._array(payload["bars"], MAX_BARS, "bars")
    if not bars:
        raise ValueError("bars must contain at least one record")
    return payload


def _cadence(times):
    """The most common spacing between consecutive bars, measured not assumed.

    An equity's daily bars are not one day apart: weekends and holidays make the
    real spacing alternate between one day and three, so a detector that trusted
    the interval label would call every weekend a hole and every window crossing
    one the wrong length.
    """
    counts: dict[int, int] = {}
    for index in range(1, len(times)):
        gap = int((times[index] - times[index - 1]).total_seconds())
        if gap > 0:
            counts[gap] = counts.get(gap, 0) + 1
    if not counts:
        return 0, 0
    cadence = max(sorted(counts), key=lambda gap: counts[gap])
    return cadence, sum(1 for gap in counts if gap > cadence * 1.5)


def import_price_history(conn, entity_id, path, interval="1d", now=None):
    """Store a user-supplied OHLC export in `price_bars` and report what it covers.

    No network access: the provider is whatever produced `path`, and this records
    its claims rather than checking them.
    """
    started = now if now is not None else _now()
    validate_request(interval, started)
    if type(entity_id) is not int or not 1 <= entity_id <= 2**63 - 1:
        raise ValueError("entity id must be a positive SQLite integer")
    entity = registry.get_entity(conn, entity_id)
    if entity is None:
        raise ValueError(f"entity {entity_id} not found")
    importer._text(entity["key"], "entity key", 512, True)
    payload = _load(path)
    instrument = _instrument(payload["instrument"])
    # Registered once the symbol is known, which is after the file is read and parsed
    # and before anything is stored: a refused argument or an unreadable file is not a
    # run, and a failure from here on is.
    registry.record_failures(conn, source="import-history", started=started,
                             query=instrument["symbol"], indicator=interval)
    interval_seconds = INTERVALS[interval]
    newest_allowed = started.astimezone(UTC).isoformat()
    bars = [_bar(row, interval_seconds, newest_allowed) for row in payload["bars"]]
    seen: set[str] = set()
    unique = []
    duplicates = 0
    for bar in sorted(bars, key=lambda item: item["open_time"]):
        if bar["open_time"] in seen:
            duplicates += 1
            continue
        seen.add(bar["open_time"])
        unique.append(bar)

    inherited = not conn.in_transaction
    if inherited:
        conn.execute("BEGIN")
    savepoint = "import_price_history_" + uuid4().hex
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        for bar in unique:
            registry.add_price_bar(
                conn, instrument_key=entity["key"], interval_seconds=interval_seconds,
                open_time=bar["open_time"], close_time=bar["close_time"], open=bar["open"],
                high=bar["high"], low=bar["low"], close=bar["close"], volume=bar["volume"],
                quote_volume=bar["quote_volume"], trades=bar["trades"],
                source=payload["source"],
                retrieved_at=payload["retrieved_at"], source_url=payload["source_url"],
                evidence=json.dumps({
                    "method": METHOD, "symbol": instrument["symbol"],
                    "venue": instrument["venue"] or "unknown",
                    "currency": instrument["currency"],
                    "asset_class": instrument["asset_class"] or "unknown",
                    "adjustment": instrument["adjustment"],
                    "rights_basis": instrument["rights_basis"] or "unknown",
                    "close_time_derived": bar["close_time_derived"],
                    "trades_applicable": False,
                }, sort_keys=True, separators=(",", ":")))
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except BaseException:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        if inherited:
            conn.rollback()
        raise

    retained = registry.list_price_bars(conn, entity["key"], interval_seconds,
                                        limit=MAX_BARS, order="desc")
    times = [datetime.fromisoformat(row["open_time"]) for row in reversed(retained)]
    cadence, holes = _cadence(times)
    records_hash = hashlib.sha256(
        json.dumps([[bar["open_time"], bar["close_time"], bar["open"], bar["high"],
                     bar["low"], bar["close"], bar["volume"]] for bar in unique],
                   separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    unknown = [f"{field} ({meaning})" for field, meaning in OPTIONAL_INSTRUMENT.items()
               if not instrument[field]]
    if instrument["adjustment"] == "unknown":
        unknown.insert(0, "adjustment (stated as unknown: any split, distribution or roll "
                          "across the imported span is unrepaired)")
    registry.set_payload_fingerprint(conn, records_hash)
    registry.clear_failure_recorder(conn)
    run_id = registry.record_ingest_run(
        conn, source="import-history", query=instrument["symbol"], indicator=interval,
        started_at=started.isoformat(), finished_at=(now or _now()).isoformat(),
        fetched=len(bars), stored=len(unique), skipped=duplicates, pages=1,
        total=len(retained), truncated=False, retrieval_sha256=records_hash,
        warnings=(["duplicated open times collapsed"] if duplicates else []),
        coverage=f"{interval} bars from a user-supplied export, {instrument['adjustment']}",
        pages_detail=[{"page": 1, "source_url": payload["source_url"], "rows": len(bars),
                       "stored": len(unique)}])
    return {
        "method": METHOD,
        "instrument": {"entity_id": entity_id, "entity_key": entity["key"],
                       "symbol": instrument["symbol"],
                       "venue": instrument["venue"] or "unknown",
                       "currency": instrument["currency"],
                       "asset_class": instrument["asset_class"] or "unknown",
                       "interval": interval, "interval_seconds": interval_seconds,
                       "adjustment": instrument["adjustment"]},
        "parameters": {"path": path, "source": "user-supplied file", "network_access": False},
        "coverage": {
            "stored_this_run": len(unique), "retained_bars": len(retained),
            "oldest_open_time": times[0].isoformat() if times else None,
            "newest_open_time": times[-1].isoformat() if times else None,
            "measured_cadence_seconds": cadence or None,
            "cadence_note": "measured from the imported bars, not from the interval label",
            "gaps_detected": holes, "complete": False,
            "completeness_note": "an export is whatever the provider chose to send, so the "
                                 "series may start late, end early or omit a session; "
                                 "consumers must check continuity before measuring a window",
        },
        "skipped": {"duplicate": duplicates},
        "unknowns": unknown,
        "provenance": {"source_url": payload["source_url"],
                       "retrieved_at": payload["retrieved_at"], "ingest_run_id": run_id,
                       "bars_sha256": records_hash, "rights_verified": False},
        "limitations": [
            "No network call is made and no field is checked against an independent source: "
            "this stores the export's own claims, and rights_verified is false whatever the "
            "file claims.",
            "adjustment states whether the export is split- and distribution-adjusted. An "
            "'unknown' adjustment means a return spanning a corporate action is not a "
            "measurement of price movement, and nothing here repairs it.",
            "venue, asset_class and rights_basis are copied from the file and left unknown "
            "when absent; they are never inferred from the symbol.",
            "trades is 0 because a trade count is a crypto spot venue field with no meaning "
            "for an exchange-traded instrument; it is not a claim that no trades occurred.",
            "close_time is derived from open_time plus the interval when the file omits it, "
            "and each stored row records that it was derived.",
            "Quote currency and venue identity are the exporter's statements. Nothing here "
            "establishes which exchange a symbol traded on or in what currency.",
            "No accuracy, calibration or trading claim is made from an imported series.",
        ],
    }
