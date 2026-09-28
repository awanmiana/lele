import hashlib
import json
from ..core import clock
from datetime import datetime, timedelta, UTC
from urllib.parse import urlencode

from ..analysis import projection
from ..core import importer, registry
from ..core.constants import PRICE_ENDPOINTS
from .http import HTTPClient, SourceError

MAX_POINTS = 1000
MAX_BYTES = 2 * 1024 * 1024
INTERVAL = 300
INSTRUMENTS = {
    "BTCUSDT": ("binance", "bitcoin", "USDT", "BTC", "spot_5m_close", "SPOT"),
    "PAXGUSDT": ("binance", "gold", "USDT", "PAXG token", "spot_5m_close", "SPOT"),
    "GC=F": ("yahoo", "gold", "USD", "troy ounce", "futures_5m_close", "FUTURE"),
    "CL=F": ("yahoo", "oil", "USD", "barrel", "futures_5m_close", "FUTURE"),
    "RB=F": ("yahoo", "fuel", "USD", "US gallon", "futures_5m_close", "FUTURE"),
    "AAPL": ("yahoo", "stock", "USD", "share", "equity_5m_close", "EQUITY"),
}


def _now():
    return clock.now()


def _epoch(value):
    if type(value) is not int or not 0 <= value <= 253402300499:
        raise ValueError("price timestamp must be integer Unix seconds in the supported range")
    if value % INTERVAL:
        raise ValueError("candle opening must be on a UTC five-minute boundary")
    return value


def _iso(value):
    return datetime.fromtimestamp(value, UTC).isoformat()


def _retrieved(value):
    importer._text(value, "retrieved_at", 64, True)
    try:
        result = datetime.fromisoformat(value)
        if result.utcoffset() is None:
            raise ValueError
        result = result.astimezone(UTC)
        if result > _now():
            raise ValueError
    except (ValueError, OverflowError) as exc:
        raise ValueError("retrieval timestamp must be aware and not in the future") from exc
    return result


def request_window(source, symbol, limit, end, now):
    if symbol not in INSTRUMENTS or INSTRUMENTS[symbol][0] != source:
        raise ValueError("unsupported price source/symbol pair")
    if type(limit) is not int or not 2 <= limit <= MAX_POINTS:
        raise ValueError("price limit must be from 2 to 1000 five-minute slots")
    finish = projection._timestamp(end) if end is not None else now.replace(
        minute=now.minute - now.minute % 5, second=0, microsecond=0)
    if finish > now:
        raise ValueError("price end must not be in the future")
    start = finish - timedelta(seconds=limit * INTERVAL)
    if start.timestamp() < 0:
        raise ValueError("price window must start at or after the Unix epoch")
    if source == "yahoo" and start < now - timedelta(days=60):
        raise ValueError("Yahoo five-minute history is limited to the last 60 days")
    return int(start.timestamp()), int(finish.timestamp())


def _binance(payload, limit):
    rows = importer._array(payload, limit + 1, "Binance klines")
    result = []
    for row in rows:
        if not isinstance(row, list) or len(row) != 12:
            raise ValueError("Binance kline requires 12 fields")
        opening, closing = row[0], row[6]
        if type(opening) is not int or opening % 1000:
            raise ValueError("Binance opening must be integer epoch milliseconds")
        opening = _epoch(opening // 1000)
        if type(closing) is not int or closing != (opening + INTERVAL) * 1000 - 1:
            raise ValueError("Binance close time does not describe a five-minute candle")
        if row[4] is not None:
            projection._price(row[4])
        result.append((opening, row[4], False))
    return result, "Binance", {}


def _yahoo(payload, symbol, limit):
    try:
        if not isinstance(payload, dict) or not isinstance(payload["chart"], dict):
            raise ValueError("invalid Yahoo chart")
        chart = payload["chart"]
        if chart["error"] is not None:
            raise SourceError("Yahoo chart unavailable; review source access and history limits")
        results = importer._array(chart["result"], 1, "Yahoo results")
        if len(results) != 1 or not isinstance(results[0], dict):
            raise ValueError("Yahoo requires one chart result")
        result = results[0]
        meta = result["meta"]
        if not isinstance(meta, dict):
            raise ValueError("invalid Yahoo metadata")
        if (meta.get("symbol") != symbol or meta.get("currency") != INSTRUMENTS[symbol][2]
                or meta.get("instrumentType") != INSTRUMENTS[symbol][5]
                or meta.get("dataGranularity") != "5m"):
            raise ValueError("Yahoo instrument or interval metadata mismatch")
        venue = meta.get("exchangeName")
        importer._text(venue, "Yahoo exchangeName", 256, True)
        timestamps = importer._array(result["timestamp"], limit + 1, "Yahoo timestamps")
        quotes = importer._array(result["indicators"]["quote"], 1, "Yahoo quotes")
        if len(quotes) != 1 or not isinstance(quotes[0], dict):
            raise ValueError("Yahoo requires one quote array")
        closes = importer._array(quotes[0]["close"], limit + 1, "Yahoo closes")
        if len(closes) != len(timestamps):
            raise ValueError("Yahoo timestamps and closes must have equal lengths")
        rows = []
        for index, (timestamp, close) in enumerate(zip(timestamps, closes)):
            if type(timestamp) is not int or not 0 <= timestamp <= 253402300499:
                raise ValueError("Yahoo candle timestamps must be integer Unix seconds in the supported range")
            if timestamp % INTERVAL == 0:
                opening = timestamp
            elif (index == len(timestamps) - 1 and type(meta.get("regularMarketTime")) is int
                  and timestamp == meta.get("regularMarketTime")
                  and type(close) in (int, float)):
                projection._price(str(close))
                rows.append((timestamp, close, True))
                continue
            else:
                raise ValueError("only the final timestamp may leave the five-minute grid and must match "
                                 "meta.regularMarketTime with a numeric close")
            if close is not None:
                if type(close) not in (int, float):
                    raise ValueError("Yahoo close must be a JSON number or null")
                close = str(close)
                projection._price(close)
            rows.append((opening, close, False))
    except (KeyError, TypeError, IndexError) as exc:
        raise ValueError("malformed Yahoo chart response") from exc
    return rows, venue, meta


def fetch_prices(conn, entity_id, source, symbol, limit=288, end=None):
    started = _now()
    start, finish = request_window(source, symbol, limit, end, started)
    if type(entity_id) is not int or not 1 <= entity_id <= 2**63 - 1:
        raise ValueError("entity id must be a positive SQLite integer")
    entity = registry.get_entity(conn, entity_id)
    if entity is None:
        raise ValueError(f"entity {entity_id} not found")
    importer._text(entity["key"], "entity key", 512, True)
    if source == "binance":
        params = {"symbol": symbol, "interval": "5m", "startTime": start * 1000,
                  "endTime": finish * 1000 - 1, "limit": limit}
    else:
        params = {"interval": "5m", "period1": start, "period2": finish,
                  "includePrePost": "false"}
    url = PRICE_ENDPOINTS[symbol] + "?" + urlencode(params)
    client = HTTPClient(ttl=0, max_bytes=MAX_BYTES)
    client.cache_dir = None
    client.ttl = 0
    payload = client.get_json(url)
    retrieved = _retrieved(client.retrieved_at)
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=True,
                           separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(canonical) > MAX_BYTES:
        raise ValueError("decoded price response exceeds 2 MiB")
    rows, venue, metadata = (_binance(payload, limit) if source == "binance"
                             else _yahoo(payload, symbol, limit))
    cutoff = min(started.timestamp(), retrieved.timestamp(), finish)
    seen = set()
    observations = []
    counts = {"missing_closes": 0, "outside_window": 0, "unclosed": 0, "trailing_snapshots": 0}
    for opening, close, trailing in rows:
        if trailing:
            counts["trailing_snapshots"] += 1
            continue
        if opening in seen:
            raise ValueError("duplicate candle opening timestamps")
        seen.add(opening)
        if close is None:
            counts["missing_closes"] += 1
        if opening + INTERVAL > cutoff:
            counts["unclosed"] += 1
            continue
        if opening < start or opening >= finish:
            counts["outside_window"] += 1
            continue
        if close is not None:
            observations.append({"timestamp": _iso(opening + INTERVAL), "price": close})
    observations.sort(key=lambda row: row["timestamp"])
    if not 2 <= len(observations) <= limit:
        raise ValueError("price window must contain at least two closed, valid observations")
    _, asset, currency, unit, price_type, _ = INSTRUMENTS[symbol]
    output = {"instrument": {"entity_key": entity["key"], "symbol": symbol,
                             "asset_class": asset, "venue": venue, "currency": currency,
                             "unit": unit, "price_type": price_type, "source_url": url},
              "prices": observations}
    report = {
        "source": source, "symbol": symbol, "entity_id": entity_id, "entity_key": entity["key"],
        "source_url": url, "retrieved_at": retrieved.isoformat(),
        "request_started_at": started.isoformat(), "interval_seconds": INTERVAL,
        "window": {"opening_start_inclusive": _iso(start), "end_boundary_inclusive": _iso(finish)},
        "timestamp_semantics": "candle end boundary (opening + 300 seconds), not trade/publication time",
        "received": len(rows), "exported": len(observations), "skipped": counts,
        "missing_slots": limit - len(observations), "requested_slots": limit,
        "response_sha256": hashlib.sha256(canonical).hexdigest(),
        "response_hash_encoding": "parsed JSON, sorted keys, ASCII, compact separators; not wire bytes",
        "provider_metadata": metadata, "truth_verified": False,
        "warnings": list(client.warnings),
        "limitations": [
            "One bounded request; no paging, gap filling, currency conversion or registry writes.",
            "Entity association is selected by the user, not independently identity-verified.",
            "Closure uses the local UTC clock; provider delay, later revisions and stale data remain possible.",
            "No prospective accuracy validation or guarantee; objective remains not_achieved.",
        ],
    }
    if source == "yahoo":
        report["limitations"].extend([
            "Yahoo chart is an unsupported public endpoint, not an assured permissive data license; "
            "review Yahoo terms for personal research use and redistribution rights. No access-block bypass.",
            "Uses quote.close, not adjclose; no adjustment, split, distribution or roll repair. "
            "Yahoo JSON numbers pass through Python float and are not exact exchange tick evidence.",
            "GC=F, CL=F and RB=F are Yahoo futures-series symbols, not fixed-expiry contracts, "
            "spot gold/oil or retail pump prices; venue is the provider exchangeName code.",
            "Five-minute history limited to recent 60 days; regular session requested, no calendar verification.",
            "A final provider row off the five-minute grid is a trailing snapshot, not a candle; "
            "it is excluded from exports and counted as skipped.trailing_snapshots.",
        ])
    else:
        report["limitations"].append("BTCUSDT is Binance spot quoted in USDT, not USD; access is jurisdiction-dependent.")
    return output, report
