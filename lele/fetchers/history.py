"""Multi-interval OHLC price history for one instrument, stored in the registry.

Unlike `prices.fetch_prices`, which exports a bounded five-minute window for
world-state analysis, this fetcher walks Binance klines backwards so that years
of daily or hourly bars are available for move detection. Bars are idempotent
on (instrument, interval, open time) so re-running refines rather than
duplicates.
"""
import hashlib
import json
import math
from ..core import clock
from datetime import datetime, UTC
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from ..core import importer, registry
from ..core.constants import PRICE_ENDPOINTS
from .http import HTTPClient

MAX_BYTES = 2 * 1024 * 1024
MAX_BARS = 5000
MAX_PAGE = 1000
MAX_PAGES = 10
SOURCE = "binance"
SYMBOLS = ("BTCUSDT", "PAXGUSDT")
INTERVALS = {"1d": 86400, "4h": 14400, "1h": 3600}


def _now():
    return clock.now()


def _iso(epoch_seconds):
    return datetime.fromtimestamp(epoch_seconds, UTC).isoformat()


def validate_request(symbol, interval, limit, pages, end, now):
    if symbol not in SYMBOLS:
        raise ValueError(f"history symbol must be one of {', '.join(SYMBOLS)}")
    if interval not in INTERVALS:
        raise ValueError(f"history interval must be one of {', '.join(INTERVALS)}")
    if type(limit) is not int or not 2 <= limit <= MAX_BARS:
        raise ValueError(f"history limit must be an integer from 2 to {MAX_BARS}")
    if type(pages) is not int or not 1 <= pages <= MAX_PAGES:
        raise ValueError(f"history pages must be an integer from 1 to {MAX_PAGES}")
    if end is None:
        return None
    from ..analysis import projection
    finish = projection._timestamp(end)
    if finish > now:
        raise ValueError("history end must not be in the future")
    return int(finish.timestamp())


def _number(value, label):
    """Accept a Binance numeric field sent as JSON text or number, return plain decimal text."""
    if isinstance(value, bool):
        raise ValueError(f"Binance {label} must be numeric")
    if isinstance(value, str):
        importer._text(value, f"Binance {label}", 64, True)
        try:
            number = Decimal(value.strip())
        except InvalidOperation as exc:
            raise ValueError(f"Binance {label} must be a finite decimal string") from exc
    elif isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"Binance {label} must be finite")
        number = Decimal(repr(value))
    else:
        raise ValueError(f"Binance {label} must be numeric")
    if not number.is_finite():
        raise ValueError(f"Binance {label} must be finite")
    return format(number.normalize(), "f")


def _bar(row, interval_seconds):
    if not isinstance(row, list) or len(row) != 12:
        raise ValueError("Binance kline requires exactly 12 fields")
    opening, closing = row[0], row[6]
    if type(opening) is not int or opening % 1000:
        raise ValueError("Binance opening must be integer epoch milliseconds")
    opening_seconds = opening // 1000
    if opening_seconds % interval_seconds:
        raise ValueError("Binance opening must sit on the requested interval grid")
    if type(closing) is not int or closing != (opening_seconds + interval_seconds) * 1000 - 1:
        raise ValueError("Binance close time does not describe one requested candle")
    prices = [_number(row[index], label) for index, label in
              ((1, "open"), (2, "high"), (3, "low"), (4, "close"))]
    volume = _number(row[5], "volume")
    quote_volume = _number(row[7], "quote volume")
    trades = row[8]
    if type(trades) is not int or trades < 0:
        raise ValueError("Binance trade count must be a nonnegative integer")
    return {
        "open_time": _iso(opening_seconds),
        "close_time": _iso(opening_seconds + interval_seconds),
        "open": prices[0], "high": prices[1], "low": prices[2], "close": prices[3],
        "volume": volume, "quote_volume": quote_volume, "trades": trades,
    }


def _page(client, symbol, interval, finish, page_size):
    params = {"symbol": symbol, "interval": interval, "limit": page_size}
    if finish is not None:
        params["endTime"] = (finish - 1) * 1000
    url = PRICE_ENDPOINTS[symbol] + "?" + urlencode(params)
    payload = client.get_json(url)
    rows = importer._array(payload, page_size, "Binance klines")
    return url, rows


def fetch_price_history(conn, entity_id, symbol, interval="1d", limit=1000, pages=1,
                        end=None):
    """Fetch bounded OHLC history and store it in `price_bars`.

    Pages walk backwards from `end`. Stored bars are idempotent, so a repeated
    run updates the same (instrument, interval, open time) rows.
    """
    if type(entity_id) is not int or not 1 <= entity_id <= 2**63 - 1:
        raise ValueError("entity id must be a positive SQLite integer")
    entity = registry.get_entity(conn, entity_id)
    if entity is None:
        raise ValueError(f"entity {entity_id} not found")
    importer._text(entity["key"], "entity key", 512, True)
    started = _now()
    registry.record_failures(conn, source=f'{SOURCE}-history', started=started, query=symbol, indicator=interval)
    finish = validate_request(symbol, interval, limit, pages, end, started)
    interval_seconds = INTERVALS[interval]
    page_size = min(MAX_PAGE, limit)
    pages = min(pages, -(-limit // page_size))
    client = HTTPClient(max_bytes=MAX_BYTES)
    stored = 0
    skipped = {"malformed": 0, "duplicate": 0, "unclosed": 0}
    pages_detail = []
    warnings = list(client.warnings)
    floor = None
    seen_open_times: set[str] = set()
    for page in range(1, pages + 1):
        url, rows = _page(client, symbol, interval, finish, page_size)
        if not rows:
            warnings.append(f"page {page} returned no rows; history start reached")
            break
        accepted = 0
        oldest_epoch = None
        for row in sorted(rows, key=lambda item: item[0] if isinstance(item, list) and item else 0):
            try:
                bar = _bar(row, interval_seconds)
            except (ValueError, TypeError):
                skipped["malformed"] += 1
                continue
            open_epoch = int(datetime.fromisoformat(bar["open_time"]).timestamp())
            if floor is not None and open_epoch >= floor:
                skipped["already_covered"] = skipped.get("already_covered", 0) + 1
                continue
            if bar["open_time"] in seen_open_times:
                skipped["duplicate"] += 1
                continue
            if bar["close_time"] > _now().isoformat():
                skipped["unclosed"] += 1
                continue
            seen_open_times.add(bar["open_time"])
            oldest_epoch = open_epoch if oldest_epoch is None else min(oldest_epoch, open_epoch)
            registry.add_price_bar(
                conn, instrument_key=entity["key"], interval_seconds=interval_seconds,
                open_time=bar["open_time"], close_time=bar["close_time"], open=bar["open"],
                high=bar["high"], low=bar["low"], close=bar["close"], volume=bar["volume"],
                quote_volume=bar["quote_volume"], trades=bar["trades"], source=SOURCE,
                retrieved_at=client.retrieved_at, source_url=url,
                evidence=json.dumps({"symbol": symbol, "interval": interval,
                                     "unadjusted": True}, separators=(",", ":")))
            stored += 1
            accepted += 1
        pages_detail.append({"page": page, "source_url": url, "rows": len(rows),
                             "stored": accepted,
                             "oldest_open_time": _iso(oldest_epoch) if oldest_epoch else None})
        if accepted == 0 or len(rows) < page_size or stored >= limit:
            break
        floor = oldest_epoch if oldest_epoch is not None else floor
        finish = oldest_epoch if oldest_epoch is not None else finish
    retained = registry.list_price_bars(conn, entity["key"], interval_seconds,
                                         limit=MAX_BARS, order="desc")
    retained_count = len(retained)
    newest_open_time = retained[0]["open_time"] if retained else None
    oldest_open_time = retained[-1]["open_time"] if retained else None
    retrieved = client.retrieved_at or started.isoformat()
    payload_hash = hashlib.sha256(
        json.dumps(pages_detail, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    registry.clear_failure_recorder(conn)
    run_id = registry.record_ingest_run(
        conn, source=f"{SOURCE}-history", query=symbol, indicator=interval,
        started_at=started.isoformat(), finished_at=_now().isoformat(),
        fetched=sum(detail["rows"] for detail in pages_detail), stored=stored,
        skipped=sum(skipped.values()), pages=len(pages_detail), total=retained_count,
        truncated=bool(pages_detail) and len(pages_detail) == pages
        and pages_detail[-1]["rows"] == page_size,
        records_sha256=payload_hash, warnings=warnings,
        coverage=f"{interval} bars from Binance spot klines, unadjusted",
        pages_detail=pages_detail)
    return {
        "method": "binance_ohlc_history_v1",
        "instrument": {"entity_id": entity_id, "entity_key": entity["key"], "symbol": symbol,
                       "interval": interval, "interval_seconds": interval_seconds,
                       "source": SOURCE, "venue": SOURCE,
                       "quote_currency": "USDT",
                       "quote_note": "every supported symbol is quoted in USDT on Binance spot, "
                                     "so the quote currency is stated once rather than "
                                     "implying a per-symbol distinction that does not exist"},
        "parameters": {"limit": limit, "pages": pages, "end": end,
                       "direction": "backward from end"},
        "coverage": {"stored_this_run": stored, "retained_bars": retained_count,
                     "pages": pages_detail, "oldest_open_time": oldest_open_time,
                     "newest_open_time": newest_open_time,
                     "adjusted": False, "complete": False,
                     "completeness_note": "a bounded page walk cannot prove it reached the "
                                          "provider's first bar, and a run can be resumed with a "
                                          "different --end, so the stored series may have holes; "
                                          "consumers must check bar continuity before measuring "
                                          "a window"},
        "skipped": skipped,
        "provenance": {"retrieved_at": retrieved, "ingest_run_id": run_id,
                       "pages_sha256": payload_hash, "truth_verified": False},
        "limitations": [
            "Spot klines only; the symbol is Binance spot quoted in USDT, not USD, and carries no "
            "contract, funding or basis information.",
            "Bars are unadjusted provider values; no split, distribution or roll repair applies to "
            "spot crypto, and no independent market-truth check was run.",
            "Binance lists a bounded number of historical klines per interval, so the earliest "
            "available bar depends on the symbol and interval; a short result means the provider "
            "history start, not a complete record.",
            "The final candle is dropped when it has not elapsed on the local UTC clock, so the "
            "newest stored bar may lag the provider by one interval.",
            "HTTP responses may be served from the local cache, in which case retrieved_at is the "
            "cached fetch time rather than the moment of this run.",
            "Trading is 24/7 for spot crypto, so no session calendar, holiday or corporate action "
            "needs repair, but venue outages and flash crashes remain uncorrected.",
            "No accuracy, calibration or trading claim is made from stored history.",
        ],
    }
