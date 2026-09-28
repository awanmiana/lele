import hashlib
import json
import os
import re
import tempfile
from ..core import clock
from datetime import date, datetime, timedelta, UTC
from decimal import Context, Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from urllib.parse import urlencode

from ..analysis import events
from ..core import importer, registry
from ..core.constants import EVIDENCE_ENDPOINTS
from . import prices
from .http import HTTPClient

INTERVAL = 300
MAX_SLOTS = 300
MAX_OBSERVATIONS = 1000
FUNDING_LIMIT = 100
RATIO_LIMIT = 120
DEPTH_LEVELS = 500
RATIO_PLACES = Decimal("0.000000000001")
MAX_BYTES = 2 * 1024 * 1024
SOURCES = {"binance-futures": ("BTCUSDT", "PAXGUSDT")}
PLATFORM = "binance-futures"
COT_MARKETS = {
    "BITCOIN-CME": "BITCOIN - CHICAGO MERCANTILE EXCHANGE",
    "GOLD-COMEX": "GOLD - COMMODITY EXCHANGE INC.",
    "WTI-NYMEX": "CRUDE OIL, LIGHT SWEET - NEW YORK MERCANTILE EXCHANGE",
}
COT_MAX_REPORTS = 52
COT_DEFAULT_REPORTS = 12
COT_PLATFORM = "cftc-cot"
SHORT_SYMBOL_RE = re.compile(r"[A-Z][A-Z0-9.\-]{0,9}")
SHORT_MAX_MONTHS = 36
SHORT_DEFAULT_MONTHS = 12
SHORT_MAX_RECORDS = 1000
SHORT_PLATFORM = "finra"


def _now():
    return clock.now()


def request_window(limit, end, now, symbol="BTCUSDT"):
    if type(limit) is not int or not 2 <= limit <= MAX_SLOTS:
        raise ValueError(f"evidence limit must be an integer from 2 to {MAX_SLOTS} five-minute slots")
    if symbol not in SOURCES["binance-futures"]:
        raise ValueError("unsupported evidence symbol")
    return prices.request_window("binance", symbol, limit, end, now)


def _iso(seconds):
    return datetime.fromtimestamp(seconds, UTC).isoformat()


def _decimal(value, label):
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{label} must be a finite JSON number or numeric string")
    text = value if isinstance(value, str) else str(value)
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"{label} must be a finite decimal") from exc
    if not number.is_finite():
        raise ValueError(f"{label} must be finite")
    return number


def _observation(identifier, entity_key, seconds, kind, source_url, description,
                 value, unit, currency):
    return {
        "id": identifier, "entity_key": entity_key, "observed_at": _iso(seconds),
        "available_at": _iso(seconds), "kind": kind, "source_url": source_url,
        "description": description, "platform": PLATFORM, "industry": None, "location": None,
        "measurement": {"value": str(value), "unit": unit, "currency": currency,
                        "basis": "observed"},
        "mapping": None,
    }


def _order_flow(rows, entity_key, url, start, finish, cutoff):
    observations = []
    skipped = {"unclosed": 0, "outside_window": 0}
    for row in rows:
        if not isinstance(row, list) or len(row) != 12:
            raise ValueError("Binance futures kline requires 12 fields")
        opening, closing = row[0], row[6]
        if type(opening) is not int or opening % 1000:
            raise ValueError("Binance futures opening must be integer epoch milliseconds")
        seconds = opening // 1000
        if seconds % INTERVAL:
            raise ValueError("Binance futures candle opening must be on a five-minute boundary")
        if type(closing) is not int or closing != (seconds + INTERVAL) * 1000 - 1:
            raise ValueError("Binance futures close time does not describe a five-minute candle")
        end = seconds + INTERVAL
        if end > cutoff:
            skipped["unclosed"] += 1
            continue
        if seconds < start or seconds >= finish:
            skipped["outside_window"] += 1
            continue
        quote = _decimal(row[7], "quote asset volume")
        taker = _decimal(row[10], "taker buy quote volume")
        if quote < 0 or taker < 0 or taker > quote:
            raise ValueError("Binance futures taker buy volume must lie within total volume")
        net = (2 * taker - quote)
        observations.append(_observation(
            f"{PLATFORM}:order_flow:{end}", entity_key, end, "order_flow", url,
            "Net taker quote flow over a closed five-minute futures candle",
            net, "currency", "USDT"))
    return observations, skipped


def _open_interest(rows, entity_key, url, cutoff):
    parsed = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("open interest history requires objects")
        timestamp = row.get("timestamp")
        if type(timestamp) is not int or not 0 <= timestamp <= 253402300499000:
            raise ValueError("open interest timestamp must be integer epoch milliseconds")
        value = _decimal(row.get("sumOpenInterestValue"), "sumOpenInterestValue")
        if value < 0:
            raise ValueError("open interest value must be nonnegative")
        parsed.append((timestamp // 1000, value))
    parsed.sort()
    observations = []
    for (_, previous), (seconds, current) in zip(parsed, parsed[1:]):
        if seconds > cutoff:
            continue
        observations.append(_observation(
            f"{PLATFORM}:open_interest:{seconds}", entity_key, seconds, "open_interest", url,
            "Change in open interest notional between consecutive five-minute bars",
            current - previous, "currency", "USDT"))
    return observations


def _funding(rows, entity_key, url, cutoff):
    observations = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("funding history requires objects")
        timestamp = row.get("fundingTime")
        if type(timestamp) is not int or not 0 <= timestamp <= 253402300499000:
            raise ValueError("funding time must be integer epoch milliseconds")
        seconds = timestamp // 1000
        if seconds > cutoff:
            continue
        rate = _decimal(row.get("fundingRate"), "fundingRate")
        observations.append(_observation(
            f"{PLATFORM}:funding_rate:{seconds}", entity_key, seconds, "funding_rate", url,
            "Settled perpetual futures funding rate", rate, "rate", None))
    return observations


def _snapshot_seconds(payload, retrieved):
    for field in ("T", "E"):
        value = payload.get(field)
        if value is None:
            continue
        if type(value) is not int or not 0 <= value <= 253402300499000:
            raise ValueError("order book timestamp must be integer epoch milliseconds")
        return value // 1000
    return int(retrieved.timestamp())


def _order_book_imbalance(payload, entity_key, url, retrieved):
    if not isinstance(payload, dict):
        raise ValueError("order book depth requires an object")
    totals = []
    for side in ("bids", "asks"):
        levels = payload.get(side)
        if not isinstance(levels, list) or not levels:
            raise ValueError("order book depth requires nonempty bids and asks")
        total = Decimal(0)
        for level in levels:
            if not isinstance(level, list) or len(level) < 2:
                raise ValueError("order book level must be a price and quantity pair")
            price = _decimal(level[0], "order book price")
            quantity = _decimal(level[1], "order book quantity")
            if price <= 0 or quantity < 0:
                raise ValueError("order book levels require positive prices and nonnegative quantities")
            total += price * quantity
        totals.append(total)
    bids, asks = totals
    combined = bids + asks
    imbalance = (bids - asks) / combined if combined > 0 else Decimal(0)
    imbalance = imbalance.quantize(RATIO_PLACES, rounding=ROUND_HALF_EVEN)
    seconds = _snapshot_seconds(payload, retrieved)
    return [_observation(
        f"{PLATFORM}:order_book_imbalance:{seconds}", entity_key, seconds,
        "order_book_imbalance", url,
        "Resting order-book depth imbalance (bid minus ask notional over bid plus ask) at retrieval",
        imbalance, "ratio", None)]


def _long_short_ratio(rows, entity_key, url, start, finish, cutoff, kind, description):
    observations = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("long/short ratio history requires objects")
        timestamp = row.get("timestamp")
        if type(timestamp) is not int or not 0 <= timestamp <= 253402300499000:
            raise ValueError("long/short ratio timestamp must be integer epoch milliseconds")
        seconds = timestamp // 1000
        if seconds < start or seconds >= finish or seconds > cutoff:
            continue
        value = _decimal(row.get("longShortRatio"), "longShortRatio")
        if value <= 0:
            raise ValueError("long/short ratio must be positive")
        observations.append(_observation(
            f"{PLATFORM}:{kind}:{seconds}", entity_key, seconds, kind, url,
            description, value, "ratio", None))
    return observations


def _validate(observations, entity_key):
    if not observations:
        raise ValueError("no closed evidence observations were produced")
    if len(observations) > MAX_OBSERVATIONS:
        raise ValueError("evidence exceeds the 1000-observation limit")
    raw = json.dumps({"observations": observations}, ensure_ascii=True,
                     allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise ValueError("evidence response exceeds 2 MiB")
    descriptor, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
        events._evidence(path, entity_key)
    finally:
        os.unlink(path)
    return raw


def fetch_evidence(conn, entity_id, source, symbol, limit=288, end=None):
    if source not in SOURCES or symbol not in SOURCES[source]:
        raise ValueError("unsupported evidence source/symbol pair")
    if type(entity_id) is not int or not 1 <= entity_id <= 2**63 - 1:
        raise ValueError("entity id must be a positive SQLite integer")
    entity = registry.get_entity(conn, entity_id)
    if entity is None:
        raise ValueError(f"entity {entity_id} not found")
    importer._text(entity["key"], "entity key", 512, True)
    started = _now()
    start, finish = request_window(limit, end, started, symbol)
    kline_url = EVIDENCE_ENDPOINTS["KLINE"] + "?" + urlencode({
        "symbol": symbol, "interval": "5m", "startTime": start * 1000,
        "endTime": finish * 1000 - 1, "limit": limit + 1})
    oi_url = EVIDENCE_ENDPOINTS["OPEN_INTEREST"] + "?" + urlencode({
        "symbol": symbol, "period": "5m", "limit": min(limit + 1, 500),
        "startTime": start * 1000, "endTime": finish * 1000})
    funding_url = EVIDENCE_ENDPOINTS["FUNDING"] + "?" + urlencode({
        "symbol": symbol, "limit": FUNDING_LIMIT,
        "startTime": start * 1000, "endTime": finish * 1000})
    ratio_limit = min(limit, RATIO_LIMIT)
    global_url = EVIDENCE_ENDPOINTS["GLOBAL_LONG_SHORT"] + "?" + urlencode({
        "symbol": symbol, "period": "5m", "limit": ratio_limit,
        "startTime": start * 1000, "endTime": finish * 1000})
    top_url = EVIDENCE_ENDPOINTS["TOP_LONG_SHORT"] + "?" + urlencode({
        "symbol": symbol, "period": "5m", "limit": ratio_limit,
        "startTime": start * 1000, "endTime": finish * 1000})
    depth_url = EVIDENCE_ENDPOINTS["DEPTH"] + "?" + urlencode({
        "symbol": symbol, "limit": DEPTH_LEVELS})
    client = HTTPClient(ttl=0, max_bytes=MAX_BYTES)
    client.cache_dir = None
    client.ttl = 0
    klines = client.get_json(kline_url)
    open_interest = client.get_json(oi_url)
    funding = client.get_json(funding_url)
    global_ratio = client.get_json(global_url)
    top_ratio = client.get_json(top_url)
    depth = client.get_json(depth_url)
    retrieved = prices._retrieved(client.retrieved_at)
    canonical = json.dumps([klines, open_interest, funding, global_ratio, top_ratio, depth],
                           sort_keys=True, ensure_ascii=True, separators=(",", ":"),
                           allow_nan=False).encode("utf-8")
    if len(canonical) > MAX_BYTES:
        raise ValueError("decoded evidence response exceeds 2 MiB")
    cutoff = min(started.timestamp(), retrieved.timestamp(), finish)
    entity_key = entity["key"]
    with localcontext(Context(prec=128, rounding=ROUND_HALF_EVEN)):
        observations, skipped = _order_flow(
            importer._array(klines, limit + 1, "Binance futures klines"),
            entity_key, kline_url, start, finish, cutoff)
        observations += _open_interest(
            importer._array(open_interest, 500, "open interest history"),
            entity_key, oi_url, cutoff)
        observations += _funding(
            importer._array(funding, FUNDING_LIMIT, "funding history"),
            entity_key, funding_url, cutoff)
        observations += _long_short_ratio(
            importer._array(global_ratio, ratio_limit, "global long/short account ratio"),
            entity_key, global_url, start, finish, cutoff, "long_short_account_ratio",
            "Global retail long/short account ratio over a closed five-minute bar")
        observations += _long_short_ratio(
            importer._array(top_ratio, ratio_limit, "top long/short position ratio"),
            entity_key, top_url, start, finish, cutoff, "top_long_short_position_ratio",
            "Top-trader long/short position ratio over a closed five-minute bar")
        depth_observations = _order_book_imbalance(depth, entity_key, depth_url, retrieved)
        observations += depth_observations
    observations.sort(key=lambda row: (row["observed_at"], row["id"]))
    observations = observations[:MAX_OBSERVATIONS]
    raw = _validate(observations, entity_key)
    counts: dict[str, int] = {}
    for row in observations:
        counts[row["kind"]] = counts.get(row["kind"], 0) + 1
    output = {"observations": observations}
    report = {
        "source": source, "symbol": symbol, "entity_id": entity_id, "entity_key": entity_key,
        "source_urls": {"klines": kline_url, "open_interest": oi_url, "funding": funding_url,
                        "global_long_short": global_url, "top_long_short": top_url,
                        "depth": depth_url},
        "retrieved_at": retrieved.isoformat(), "request_started_at": started.isoformat(),
        "window": {"opening_start_inclusive": _iso(start), "end_boundary_inclusive": _iso(finish)},
        "timestamp_semantics": "candle end boundary for order flow; provider bar/funding time "
                               "for open interest, funding and long/short ratios; exchange "
                               "transaction time (or retrieval when absent) for the depth snapshot",
        "snapshot": {
            "order_book_levels": DEPTH_LEVELS,
            "observed_at": depth_observations[0]["observed_at"],
            "note": "single retrieval-time depth snapshot, not a historical series; it may fall "
                    "after the requested window end",
        },
        "observations": len(observations), "by_kind": counts, "skipped": skipped,
        "output_bytes": len(raw), "output_sha256": hashlib.sha256(raw).hexdigest(),
        "response_sha256": hashlib.sha256(canonical).hexdigest(),
        "response_hash_encoding": "parsed JSON, sorted keys, ASCII, compact separators; not wire bytes",
        "submitted": True, "truth_verified": False, "warnings": list(client.warnings),
        "limitations": [
            "One bounded request per endpoint; no paging and no registry writes.",
            "Liquidations are not collected: Binance has no public historical liquidation REST "
            "endpoint (fapi/v1/allForceOrders returns 404); forceOrder is WebSocket-only.",
            "The order-book imbalance is one retrieval-time snapshot, not a historical series; it "
            "reflects resting depth that can change or be spoofed within seconds and is not executed flow.",
            "Long/short ratios are measured-only features; the crowded-positioning direction is "
            "ambiguous, so no sign is applied to the direction score.",
            "available_at is set equal to observed_at because provider publication lag is not verified; "
            "this may be optimistic for point-in-time use.",
            "Order flow is net taker quote volume (2*takerBuyQuote - totalQuote), a proxy for aggressive "
            "buying, not identified counterparties or net investment.",
            "Open interest is a notional change between consecutive bars, not a directional price signal; "
            "funding rate sign interpretation is not applied to the direction score.",
            "Binance futures USDT is not USD and access is jurisdiction-dependent; no license or "
            "redistribution right is asserted. No accuracy or objective claim.",
        ],
    }
    return output, report


def request_cot_window(reports, end: str | None, now):
    if type(reports) is not int or not 1 <= reports <= COT_MAX_REPORTS:
        raise ValueError(f"cot report limit must be an integer from 1 to {COT_MAX_REPORTS} weeks")
    if end is None:
        finish = now.date()
    else:
        importer._text(end, "cot end", 32, True)
        try:
            finish = date.fromisoformat(end)
        except ValueError as exc:
            raise ValueError("cot end must be an ISO date (YYYY-MM-DD)") from exc
    if finish > now.date():
        raise ValueError("cot end must not be in the future")
    return finish - timedelta(days=7 * reports), finish


def _cot_observations(rows, entity_key, url, retrieved, market):
    observations = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("COT report requires objects")
        name = importer._text(row.get("market_and_exchange_names"), "COT market", 256, True)
        if name != market:
            raise ValueError("COT report market does not match the request")
        report_date = importer._text(row.get("report_date_as_yyyy_mm_dd"), "COT report date", 64, True)
        try:
            observed = datetime.fromisoformat(report_date).replace(tzinfo=UTC)
        except ValueError as exc:
            raise ValueError("COT report date must be ISO8601") from exc
        if observed.hour or observed.minute or observed.second or observed.microsecond:
            raise ValueError("COT report date must be a whole UTC day")
        open_interest = _decimal(row.get("open_interest_all"), "open_interest_all")
        long = _decimal(row.get("noncomm_positions_long_all"), "noncomm_positions_long_all")
        short = _decimal(row.get("noncomm_positions_short_all"), "noncomm_positions_short_all")
        if open_interest < 0:
            raise ValueError("COT open interest must be nonnegative")
        stamp = observed.date().isoformat()
        observations.append({
            "id": f"{COT_PLATFORM}:cot_open_interest:{stamp}", "entity_key": entity_key,
            "observed_at": observed.isoformat(), "available_at": retrieved.isoformat(),
            "kind": "cot_open_interest", "source_url": url,
            "description": "CFTC Commitments of Traders legacy futures-only total open interest",
            "platform": COT_PLATFORM, "industry": None, "location": None,
            "measurement": {"value": str(open_interest), "unit": "contract", "currency": None,
                            "basis": "observed"},
            "mapping": None,
        })
        observations.append({
            "id": f"{COT_PLATFORM}:cot_positioning:{stamp}", "entity_key": entity_key,
            "observed_at": observed.isoformat(), "available_at": retrieved.isoformat(),
            "kind": "cot_positioning", "source_url": url,
            "description": "CFTC Commitments of Traders legacy futures-only net non-commercial "
                           "position (long minus short)",
            "platform": COT_PLATFORM, "industry": None, "location": None,
            "measurement": {"value": str(long - short), "unit": "contract", "currency": None,
                            "basis": "observed"},
            "mapping": None,
        })
    return observations


def fetch_cot(conn, entity_id, source, symbol, reports=COT_DEFAULT_REPORTS, end=None):
    if source != "cftc" or symbol not in COT_MARKETS:
        raise ValueError("unsupported COT source/symbol pair")
    if type(entity_id) is not int or not 1 <= entity_id <= 2**63 - 1:
        raise ValueError("entity id must be a positive SQLite integer")
    entity = registry.get_entity(conn, entity_id)
    if entity is None:
        raise ValueError(f"entity {entity_id} not found")
    importer._text(entity["key"], "entity key", 512, True)
    started = _now()
    start, finish = request_cot_window(reports, end, started)
    market = COT_MARKETS[symbol]
    where = (f"market_and_exchange_names='{market}' AND "
             f"report_date_as_yyyy_mm_dd >= '{start.isoformat()}T00:00:00.000' AND "
             f"report_date_as_yyyy_mm_dd <= '{finish.isoformat()}T00:00:00.000'")
    url = EVIDENCE_ENDPOINTS["COT"] + "?" + urlencode({
        "$where": where, "$order": "report_date_as_yyyy_mm_dd DESC", "$limit": reports})
    client = HTTPClient(ttl=0, max_bytes=MAX_BYTES)
    client.cache_dir = None
    client.ttl = 0
    rows = client.get_json(url)
    retrieved = prices._retrieved(client.retrieved_at).replace(microsecond=0)
    canonical = json.dumps(rows, sort_keys=True, ensure_ascii=True, separators=(",", ":"),
                           allow_nan=False).encode("utf-8")
    if len(canonical) > MAX_BYTES:
        raise ValueError("decoded evidence response exceeds 2 MiB")
    entity_key = entity["key"]
    with localcontext(Context(prec=128, rounding=ROUND_HALF_EVEN)):
        observations = _cot_observations(importer._array(rows, reports, "COT reports"),
                                         entity_key, url, retrieved, market)
    if not observations:
        raise ValueError("no COT reports were returned for the requested window")
    observations.sort(key=lambda row: (row["observed_at"], row["id"]))
    raw = _validate(observations, entity_key)
    counts: dict[str, int] = {}
    for row in observations:
        counts[row["kind"]] = counts.get(row["kind"], 0) + 1
    report = {
        "source": source, "symbol": symbol, "market": market, "entity_id": entity_id,
        "entity_key": entity_key, "source_url": url,
        "retrieved_at": retrieved.isoformat(), "request_started_at": started.isoformat(),
        "window": {"start_inclusive": start.isoformat(), "end_inclusive": finish.isoformat()},
        "cadence": "weekly; CFTC Commitments of Traders legacy futures-only report",
        "timestamp_semantics": "observed_at is the CFTC report date; available_at is the local "
                               "retrieval time because this endpoint does not expose the exact "
                               "publication time",
        "observations": len(observations), "by_kind": counts,
        "output_bytes": len(raw), "output_sha256": hashlib.sha256(raw).hexdigest(),
        "response_sha256": hashlib.sha256(canonical).hexdigest(),
        "response_hash_encoding": "parsed JSON, sorted keys, ASCII, compact separators; not wire bytes",
        "submitted": True, "truth_verified": False, "warnings": list(client.warnings),
        "limitations": [
            "One bounded Socrata query; no paging and no registry writes.",
            "Weekly report dates, not five-minute observations; a report stays current for days.",
            "available_at is the retrieval time, not the actual publication time, so point-in-time "
            "use is conservative and may be later than the true release.",
            "Net non-commercial positioning is measured-only; the crowded-positioning direction is "
            "ambiguous, so no sign is applied to the direction score.",
            "COT aggregates reportable traders in one contract market; it is not identified "
            "counterparties, fund flows or investment advice. No accuracy or licensing claim.",
            "The market name is pinned in this project; it is one CME bitcoin contract market, not "
            "the whole bitcoin market.",
        ],
    }
    return {"observations": observations}, report


def _months_before(value, months):
    index = value.year * 12 + (value.month - 1) - months
    return date(index // 12, index % 12 + 1, 1)


def request_short_window(months, end: str | None, now):
    if type(months) is not int or not 1 <= months <= SHORT_MAX_MONTHS:
        raise ValueError(f"short interest months must be an integer from 1 to {SHORT_MAX_MONTHS}")
    if end is None:
        finish = now.date()
    else:
        importer._text(end, "short interest end", 32, True)
        try:
            finish = date.fromisoformat(end)
        except ValueError as exc:
            raise ValueError("short interest end must be an ISO date (YYYY-MM-DD)") from exc
    if finish > now.date():
        raise ValueError("short interest end must not be in the future")
    return _months_before(finish, months), finish


def _short_observations(rows, entity_key, url, retrieved, symbol):
    observations = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("short interest records require objects")
        code = importer._text(row.get("symbolCode"), "symbol code", 16, True)
        if code.upper() != symbol:
            raise ValueError("short interest response symbol does not match the request")
        settlement = importer._text(row.get("settlementDate"), "settlement date", 32, True)
        try:
            observed_day = date.fromisoformat(settlement)
        except ValueError as exc:
            raise ValueError("short interest settlement date must be ISO8601") from exc
        current = _decimal(row.get("currentShortPositionQuantity"), "currentShortPositionQuantity")
        if current < 0:
            raise ValueError("short interest quantity must be nonnegative")
        issue = row.get("issueName")
        if issue is not None:
            importer._text(issue, "issue name", 256, True)
        observed = datetime(observed_day.year, observed_day.month, observed_day.day,
                            tzinfo=UTC)
        observations.append({
            "id": f"{SHORT_PLATFORM}:short_interest:{symbol}:{observed_day.isoformat()}",
            "entity_key": entity_key, "observed_at": observed.isoformat(),
            "available_at": retrieved.isoformat(), "kind": "short_interest", "source_url": url,
            "description": f"FINRA consolidated short interest for {symbol} "
                           f"({issue if issue else 'unknown issue'})",
            "platform": SHORT_PLATFORM, "industry": None, "location": None,
            "measurement": {"value": str(current), "unit": "share", "currency": None,
                            "basis": "observed"},
            "mapping": None,
        })
    return observations


def fetch_short(conn, entity_id, source, symbol, months=SHORT_DEFAULT_MONTHS, end=None):
    if source != "finra" or not isinstance(symbol, str) or not SHORT_SYMBOL_RE.fullmatch(symbol):
        raise ValueError("unsupported short interest source/symbol")
    if type(entity_id) is not int or not 1 <= entity_id <= 2**63 - 1:
        raise ValueError("entity id must be a positive SQLite integer")
    entity = registry.get_entity(conn, entity_id)
    if entity is None:
        raise ValueError(f"entity {entity_id} not found")
    importer._text(entity["key"], "entity key", 512, True)
    started = _now()
    start, finish = request_short_window(months, end, started)
    url = EVIDENCE_ENDPOINTS["SHORT_INTEREST"]
    payload = {
        "limit": SHORT_MAX_RECORDS,
        "compareFilters": [{"fieldName": "symbolCode", "fieldValue": symbol,
                            "compareType": "EQUAL"}],
        "dateRangeFilters": [{"fieldName": "settlementDate", "startDate": start.isoformat(),
                              "endDate": finish.isoformat()}],
    }
    client = HTTPClient(ttl=0, max_bytes=MAX_BYTES)
    client.cache_dir = None
    client.ttl = 0
    rows = client.post_json(url, payload)
    retrieved = prices._retrieved(client.retrieved_at).replace(microsecond=0)
    canonical = json.dumps(rows, sort_keys=True, ensure_ascii=True, separators=(",", ":"),
                           allow_nan=False).encode("utf-8")
    if len(canonical) > MAX_BYTES:
        raise ValueError("decoded evidence response exceeds 2 MiB")
    entity_key = entity["key"]
    with localcontext(Context(prec=128, rounding=ROUND_HALF_EVEN)):
        observations = _short_observations(
            importer._array(rows, SHORT_MAX_RECORDS, "short interest records"),
            entity_key, url, retrieved, symbol)
    if not observations:
        raise ValueError("no short interest records were returned for the requested window")
    observations.sort(key=lambda row: (row["observed_at"], row["id"]))
    raw = _validate(observations, entity_key)
    counts: dict[str, int] = {}
    for row in observations:
        counts[row["kind"]] = counts.get(row["kind"], 0) + 1
    report = {
        "source": source, "symbol": symbol, "entity_id": entity_id, "entity_key": entity_key,
        "source_url": url, "retrieved_at": retrieved.isoformat(),
        "request_started_at": started.isoformat(),
        "window": {"start_inclusive": start.isoformat(), "end_inclusive": finish.isoformat()},
        "cadence": "twice monthly; FINRA consolidated short interest settlement dates",
        "timestamp_semantics": "observed_at is the FINRA settlement date; available_at is the "
                               "local retrieval time because this endpoint does not expose the "
                               "publication time",
        "observations": len(observations), "by_kind": counts,
        "output_bytes": len(raw), "output_sha256": hashlib.sha256(raw).hexdigest(),
        "response_sha256": hashlib.sha256(canonical).hexdigest(),
        "response_hash_encoding": "parsed JSON, sorted keys, ASCII, compact separators; not wire bytes",
        "submitted": True, "truth_verified": False, "warnings": list(client.warnings),
        "limitations": [
            "One bounded POST filter; no paging and no registry writes.",
            "Settlement dates are twice monthly, not five-minute observations.",
            "available_at is the retrieval time, not the actual publication time, so point-in-time "
            "use is conservative and may be later than the true release.",
            "Short interest is measured-only; it is a stock of positions, not a directional flow, "
            "so no sign is applied to the direction score.",
            "One equity symbol at a time; FINRA consolidates exchange-reported positions and may "
            "revise them. No accuracy, investment or licensing claim.",
        ],
    }
    return {"observations": observations}, report
