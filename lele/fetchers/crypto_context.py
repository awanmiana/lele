"""Measured crypto market context: a sentiment index and stablecoin supply.

Both series are provider-aggregated and market-wide, so they are stored without
an instrument key and read by the pre-move scan for any instrument. Both are
*measurements* rather than keyword matches, which is the reason they exist: a
headline lexicon can only guess at emotion or at money moving, while an index
and a supply series record them.

Neither is a cause. A greed reading does not explain a move, and a rise in
stablecoin supply does not identify who supplied the capital.
"""
import hashlib
import json
import math
from ..core import clock
from datetime import datetime, timedelta, UTC
from decimal import Decimal
from urllib.parse import urlencode

from ..core import importer, registry
from ..core.constants import SOURCES
from .http import HTTPClient, SourceError

MAX_BYTES = 2 * 1024 * 1024
MAX_SENTIMENT_DAYS = 3650
MAX_STABLECOIN_DAYS = 4000
MAX_ACTIVITY_DAYS = 365
COINS = ("bitcoin", "ethereum", "tether", "gold", "pax-gold")
#: Run-history source labels. Each is used by both the failure registration and the
#: success row, so a failure of one of these fetches lands in the same series as its
#: successes rather than in a series of its own.
SENTIMENT_SOURCE = "crypto-fear-greed"
STABLECOIN_SOURCE = "crypto-stablecoin-supply"
ACTIVITY_SOURCE = "crypto-market-activity"
MARKET_ACTIVITY = "market_activity_v1"
DAILY_SECONDS = 86400
FEAR_GREED = "crypto_fear_greed_v1"
STABLECOIN = "stablecoin_supply_v1"
CLASSIFICATIONS = ("Extreme Fear", "Fear", "Neutral", "Greed", "Extreme Greed")
COMPOSITE_BASIS = "estimated"
AGGREGATE_BASIS = "observed"
ATTEMPTS = 3
PAUSE_SECONDS = 4.0


def _now():
    return clock.now()


def _day(timestamp):
    return datetime.fromtimestamp(int(timestamp), UTC).replace(
        hour=0, minute=0, second=0, microsecond=0)


def validate_sentiment(limit, end, now):
    if type(limit) is not int or not 1 <= limit <= MAX_SENTIMENT_DAYS:
        raise ValueError(f"sentiment limit must be an integer from 1 to {MAX_SENTIMENT_DAYS}")
    if end is None:
        return None
    from ..analysis import projection
    finish = projection._timestamp(end)
    if finish > now:
        raise ValueError("sentiment end must not be in the future")
    return finish


def validate_stablecoins(limit, end, now):
    if type(limit) is not int or not 1 <= limit <= MAX_STABLECOIN_DAYS:
        raise ValueError(f"stablecoin limit must be an integer from 1 to {MAX_STABLECOIN_DAYS}")
    if end is None:
        return None
    from ..analysis import projection
    finish = projection._timestamp(end)
    if finish > now:
        raise ValueError("stablecoin end must not be in the future")
    return finish


def _get_json(client, url, label):
    """Fetch once, retrying a bounded number of times.

    Both providers occasionally miss a read deadline on a long window. A refusal
    is retried rather than reported as an empty series, because an empty result
    here would read as "no market context", which is not what happened.
    """
    import time
    errors: list[str] = []
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return client.get_json(url), errors
        except SourceError as error:
            errors.append(f"attempt {attempt}: {error}")
            if attempt < ATTEMPTS:
                time.sleep(PAUSE_SECONDS * attempt)
    raise SourceError(f"{label} unavailable after {ATTEMPTS} attempts: {errors[-1]}")


def _int(value, label, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{label} must be numeric")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric") from exc
    if not low <= number <= high:
        raise ValueError(f"{label} must lie between {low} and {high}")
    return number


def _day_ceiling(moment):
    """Last UTC day boundary that is not in the future at `moment`."""
    return moment.astimezone(UTC).replace(
        hour=0, minute=0, second=0, microsecond=0)


def _sentiment_observation(row, retrieved, now=None):
    if not isinstance(row, dict):
        raise ValueError("sentiment entry must be an object")
    stamp = row.get("timestamp")
    if isinstance(stamp, str) and stamp.isdigit():
        stamp = int(stamp)
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        raise ValueError("sentiment timestamp must be Unix seconds")
    moment = _day(stamp)
    if moment > _day_ceiling(now if now is not None else clock.now()):
        raise ValueError("sentiment timestamp must not be in the future")
    value = _int(row.get("value"), "sentiment value", 0, 100)
    classification = row.get("value_classification")
    if not isinstance(classification, str) or classification not in CLASSIFICATIONS:
        raise ValueError(f"sentiment classification must be one of {', '.join(CLASSIFICATIONS)}")
    return {
        "id": f"alternative-me:social_sentiment:{moment.date().isoformat()}",
        "observed_at": moment.isoformat(), "kind": "social_sentiment",
        "value": str(value), "unit": "index_0_100", "currency": "",
        "classification": classification, "moment": moment,
    }


def fetch_sentiment(conn, limit=365, end=None):
    """Daily crypto fear and greed index as `social_sentiment` observations."""
    started = _now()
    finish = validate_sentiment(limit, end, started)
    registry.record_failures(conn, source=SENTIMENT_SOURCE, started=started,
                             query=f"limit:{limit} end:{finish.date() if finish else 'latest'}")
    query = {"limit": str(limit), "format": "json"}
    if finish is not None:
        query["start"] = str(int((finish - timedelta(days=limit + 2)).timestamp()))
    url = SOURCES["FEAR_GREED"] + "/fng/?" + urlencode(query)
    client = HTTPClient(ttl=0, max_bytes=MAX_BYTES)
    client.cache_dir = None
    client.ttl = 0
    payload, errors = _get_json(client, url, "sentiment index")
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("sentiment response must be an object with a data array")
    rows = payload["data"]
    if len(rows) > limit:
        rows = rows[:limit]
    retrieved = started.replace(microsecond=0).isoformat()
    observations, skipped = [], {"invalid": 0, "future": 0, "duplicate": 0}
    seen: set[str] = set()
    for row in rows:
        try:
            parsed = _sentiment_observation(row, retrieved, started)
        except (ValueError, TypeError):
            skipped["invalid"] += 1
            continue
        if parsed["id"] in seen:
            skipped["duplicate"] += 1
            continue
        seen.add(parsed["id"])
        moment = parsed["moment"]
        observations.append({
            "source": "alternative-me", "external_id": parsed["id"],
            "kind": "social_sentiment",
            "description": f"Crypto fear and greed {parsed['value']} {parsed['classification']}",
            "actor_key": "index:alternative-me: fear_greed", "counterparty_key": "",
            "instrument_key": "",
            "action": "", "reason": "", "reason_basis": "unknown",
            "amount": parsed["value"], "unit": "index_0_100", "currency": "",
            "basis": COMPOSITE_BASIS, "occurred_at": moment.isoformat(),
            "observed_at": moment.isoformat(),
            "available_at": (moment + timedelta(days=1)).isoformat(),
            "source_url": url,
            "evidence": json.dumps({
                "method": FEAR_GREED, "index": "crypto_fear_greed",
                "value": int(parsed["value"]), "classification": parsed["classification"],
                "scale": "0 extreme fear to 100 extreme greed",
                "retrieved_at": retrieved, "scope": "market_wide",
                "availability_basis": "a daily value is treated as available from the following "
                                      "UTC day, because a same-day read is not established"},
                sort_keys=True, separators=(",", ":"))})
    observations.sort(key=lambda row: (row["observed_at"], row["external_id"]))
    stored = _store(conn, observations)
    report = {
        "method": FEAR_GREED, "source": "alternative-me", "source_url": url,
        "retrieved_at": retrieved, "request_started_at": started.isoformat(),
        "window": {"limit_days": limit, "end": finish.isoformat() if finish else None},
        "received": len(rows), "stored": stored, "skipped": skipped,
        "scope": "market_wide; stored without an instrument key",
        "response_sha256": hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=True, allow_nan=False,
                       separators=(",", ":")).encode("utf-8")).hexdigest(),
        "truth_verified": False, "warnings": list(client.warnings) + errors,
        "limitations": [
            "The index is a provider aggregate of sentiment, volatility, volume and social "
            "activity. Its composition is not reproduced here and its inputs are not audited.",
            "A reading is a sentiment measurement, not a cause. High greed does not predict a "
            "fall, and no threshold here is claimed to be predictive.",
            "The value is treated as available from the following UTC day, which is a stated "
            "conservative assumption rather than a verified publication time.",
            "One provider and one index; the series begins in 2013 and is not backfilled here.",
            "No accuracy, calibration or trading claim is made.",
        ],
    }
    registry.set_payload_fingerprint(conn, client)
    registry.clear_failure_recorder(conn)
    registry.record_ingest_run(
        conn, SENTIMENT_SOURCE, started.isoformat(), _now().isoformat(),
        query=f"limit:{limit} end:{finish.date() if finish else 'latest'}",
        fetched=len(rows), stored=stored, skipped=sum(skipped.values()), pages=1,
        total=len(observations), truncated=False,
        request_sha256=hashlib.sha256(url.encode("utf-8")).hexdigest(),
        payload_sha256=report["response_sha256"],
        warnings=report["warnings"],
        coverage="daily crypto fear and greed index, market-wide, provider aggregate")
    return {"observations": observations}, report


def _number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    return repr(float(value)) if isinstance(value, float) else str(value)


def _peg_breakdown(value, label):
    """Sum a provider per-peg-currency object.

    The provider keys these fields by peg currency rather than returning a
    single figure, so the aggregate is summed here and the USD-pegged part is
    kept separately because the rest of the supply is not dollar denominated.
    """
    if not isinstance(value, dict) or not value:
        raise ValueError(f"{label} must be a nonempty object keyed by peg currency")
    total = Decimal(0)
    usd = Decimal(0)
    pegs = 0
    for currency, amount in sorted(value.items()):
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            continue
        pegs += 1
        total += Decimal(str(amount))
        if currency == "peggedUSD":
            usd = Decimal(str(amount))
    if not pegs:
        raise ValueError(f"{label} has no numeric peg entries")
    return str(total.quantize(Decimal("0.01"))), str(usd.quantize(Decimal("0.01"))), pegs


def _stablecoin_observation(row, now=None):
    if not isinstance(row, dict):
        raise ValueError("stablecoin entry must be an object")
    stamp = row.get("date")
    if isinstance(stamp, str) and stamp.isdigit():
        stamp = int(stamp)
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        raise ValueError("stablecoin date must be Unix seconds")
    moment = _day(stamp)
    if moment > _day_ceiling(now if now is not None else clock.now()):
        raise ValueError("stablecoin date must not be in the future")
    circulating, circulating_usd, pegs = _peg_breakdown(
        row.get("totalCirculatingUSD"), "total circulating")
    try:
        minted, _, _ = _peg_breakdown(row.get("totalMintedUSD") or {"total": 0},
                                       "total minted")
    except ValueError:
        minted = "0"
    try:
        bridged, _, _ = _peg_breakdown(row.get("totalBridgedToUSD") or {"total": 0},
                                       "total bridged")
    except ValueError:
        bridged = "0"
    return {
        "id": f"defillama:stablecoin_supply:{moment.date().isoformat()}",
        "observed_at": moment.isoformat(), "moment": moment,
        "circulating": circulating, "circulating_usd": circulating_usd, "pegs": pegs,
        "minted": minted, "bridged": bridged,
    }


def fetch_stablecoin_supply(conn, limit=1200, end=None):
    """Daily aggregate stablecoin supply as market-wide supply observations."""
    started = _now()
    finish = validate_stablecoins(limit, end, started)
    registry.record_failures(conn, source=STABLECOIN_SOURCE, started=started,
                             query=f"limit:{limit} end:{finish.date() if finish else 'latest'}")
    url = SOURCES["DEFILLAMA_STABLECOINS"] + "/stablecoincharts/all"
    client = HTTPClient(ttl=0, max_bytes=MAX_BYTES)
    client.cache_dir = None
    client.ttl = 0
    payload, errors = _get_json(client, url, "stablecoin supply")
    if not isinstance(payload, list):
        raise ValueError("stablecoin response must be an array of daily entries")
    rows = payload
    if finish is not None:
        ceiling = int(finish.timestamp())
        rows = [row for row in rows if isinstance(row, dict)
                and isinstance(row.get("date"), (int, float, str))
                and str(row.get("date")).lstrip("-").isdigit()
                and int(row["date"]) <= ceiling]
    rows = rows[-limit:]
    if not rows:
        raise ValueError("stablecoin response contains no usable daily entries")
    retrieved = started.replace(microsecond=0).isoformat()
    observations, skipped = [], {"invalid": 0, "duplicate": 0}
    seen: set[str] = set()
    for row in rows:
        try:
            parsed = _stablecoin_observation(row, started)
        except (ValueError, TypeError):
            skipped["invalid"] += 1
            continue
        if parsed["id"] in seen:
            skipped["duplicate"] += 1
            continue
        seen.add(parsed["id"])
        moment = parsed["moment"]
        observations.append({
            "source": "defillama", "external_id": parsed["id"],
            "kind": "stablecoin_supply",
            "description": "Aggregate stablecoin supply across tracked chains",
            "actor_key": "aggregate:defillama:stablecoins", "counterparty_key": "",
            "instrument_key": "", "action": "", "reason": "",
            "reason_basis": "unknown",
            "amount": parsed["circulating"], "unit": "usd", "currency": "USD",
            "basis": AGGREGATE_BASIS, "occurred_at": moment.isoformat(),
            "observed_at": moment.isoformat(),
            "available_at": (moment + timedelta(days=1)).isoformat(),
            "source_url": url,
            "evidence": json.dumps({
                "method": STABLECOIN, "scope": "market_wide",
                "total_circulating_usd": parsed["circulating"],
                "usd_pegged_circulating_usd": parsed["circulating_usd"],
                "peg_currencies_counted": parsed["pegs"],
                "total_minted_usd": parsed["minted"],
                "total_bridged_usd": parsed["bridged"],
                "note": "the provider keys these fields by peg currency; totals are summed "
                        "across pegs here and the USD-pegged part is recorded separately",
                "retrieved_at": retrieved,
                "availability_basis": "a daily snapshot is treated as available from the "
                                      "following UTC day, because a same-day read is not "
                                      "established"},
                sort_keys=True, separators=(",", ":"))})
    observations.sort(key=lambda row: (row["observed_at"], row["external_id"]))
    stored = _store(conn, observations)
    report = {
        "method": STABLECOIN, "source": "defillama", "source_url": url,
        "retrieved_at": retrieved, "request_started_at": started.isoformat(),
        "window": {"limit_days": limit, "end": finish.isoformat() if finish else None},
        "received": len(rows), "stored": stored, "skipped": skipped,
        "coverage": {"newest_observed_at": observations[-1]["observed_at"] if observations
                     else None,
                     "oldest_observed_at": observations[0]["observed_at"] if observations
                     else None},
        "scope": "market_wide; stored without an instrument key",
        "response_sha256": hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=True, allow_nan=False,
                       separators=(",", ":")).encode("utf-8")).hexdigest(),
        "truth_verified": False, "warnings": list(client.warnings) + errors,
        "limitations": [
            "Aggregate supply across the stablecoins the provider tracks. It is not a net "
            "inflow, not a purchase of any asset, and not attributable to any actor.",
            "Supply changes can reflect issuance, redemption, bridging or a provider coverage "
            "change; this series cannot separate them and no attribution is attempted.",
            "A supply rise is consistent with capital moving into the ecosystem and nothing "
            "more. It is not evidence that a specific asset was bought.",
            "The snapshot is treated as available from the following UTC day, a stated "
            "conservative assumption rather than a verified publication time.",
            "One provider; the tracked stablecoin set can change over time without notice.",
            "No accuracy, calibration or trading claim is made.",
        ],
    }
    registry.set_payload_fingerprint(conn, client)
    registry.clear_failure_recorder(conn)
    registry.record_ingest_run(
        conn, STABLECOIN_SOURCE, started.isoformat(), _now().isoformat(),
        query=f"limit:{limit} end:{finish.date() if finish else 'latest'}",
        fetched=len(rows), stored=stored, skipped=sum(skipped.values()), pages=1,
        total=len(observations), truncated=False,
        request_sha256=hashlib.sha256(url.encode("utf-8")).hexdigest(),
        payload_sha256=report["response_sha256"],
        warnings=report["warnings"],
        coverage="daily aggregate stablecoin supply, market-wide, provider aggregate")
    return {"observations": observations}, report


def validate_activity(coin, days, entity_id, instrument_key):
    if coin not in COINS:
        raise ValueError(f"coin must be one of {', '.join(COINS)}")
    if type(days) is not int or not 1 <= days <= MAX_ACTIVITY_DAYS:
        raise ValueError(f"days must be an integer from 1 to {MAX_ACTIVITY_DAYS}")
    if type(entity_id) is not int or not 1 <= entity_id <= 2**63 - 1:
        raise ValueError("entity id must be a positive SQLite integer")
    importer._text(instrument_key, "instrument_key", 512, True)


def _day_point(entry, label):
    if not isinstance(entry, list) or len(entry) != 2:
        raise ValueError(f"{label} point must be a millisecond timestamp and a value")
    stamp, value = entry
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        raise ValueError(f"{label} timestamp must be numeric")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} value must be numeric")
    moment = datetime.fromtimestamp(int(stamp) / 1000, UTC).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return moment, float(value)


def fetch_market_activity(conn, entity_id, instrument_key, coin="bitcoin", days=365):
    """Daily per-asset capital activity: market capitalisation, volume and close.

    The stored measurement is the day-on-day market capitalisation change, which
    is the closest public proxy for capital entering or leaving this specific
    asset. It is a proxy: a market cap rises with price as well as with new
    demand, and the series cannot separate a purchase from a sale, a new buyer
    from a rotation, or spot from derivative notional.
    """
    validate_activity(coin, days, entity_id, instrument_key)
    started = _now()
    registry.record_failures(conn, source=ACTIVITY_SOURCE, started=started,
                             query=instrument_key, indicator=coin)
    url = (f"{SOURCES['COINGECKO']}/coins/{coin}/market_chart?"
           + urlencode({"vs_currency": "usd", "days": str(days), "interval": "daily"}))
    client = HTTPClient(ttl=0, max_bytes=MAX_BYTES)
    client.cache_dir = None
    client.ttl = 0
    payload, errors = _get_json(client, url, "market activity")
    if not isinstance(payload, dict):
        raise ValueError("market activity response must be an object")
    series: dict[datetime, dict[str, float]] = {}
    for label, key in (("close", "prices"), ("market_cap", "market_caps"),
                       ("volume", "total_volumes")):
        points = payload.get(key)
        if not isinstance(points, list) or not points:
            raise ValueError(f"market activity response requires a nonempty {key} array")
        for entry in points:
            try:
                moment, value = _day_point(entry, label)
            except (ValueError, TypeError, OverflowError, OSError):
                continue
            if value <= 0 or not math.isfinite(value):
                continue
            series.setdefault(moment, {})[label] = value
    if not series:
        raise ValueError("market activity response contained no usable daily points")
    ceiling = _now().replace(hour=0, minute=0, second=0, microsecond=0)
    moments = sorted(series)
    retrieved = started.replace(microsecond=0).isoformat()
    observations, skipped = [], {"incomplete": 0, "duplicate": 0, "no_prior_day": 0}
    seen: set[str] = set()
    previous: tuple[datetime, float] | None = None
    for moment in moments:
        if moment > ceiling:
            skipped["incomplete"] = skipped.get("incomplete", 0) + 1
            continue
        point = series[moment]
        if "market_cap" not in point:
            skipped["incomplete"] = skipped.get("incomplete", 0) + 1
            continue
        identifier = f"coingecko:market_activity:{coin}:{moment.date().isoformat()}"
        if identifier in seen:
            skipped["duplicate"] = skipped.get("duplicate", 0) + 1
            continue
        seen.add(identifier)
        market_cap = point["market_cap"]
        if previous is None:
            skipped["no_prior_day"] = skipped.get("no_prior_day", 0) + 1
            previous = (moment, market_cap)
            continue
        prior_moment, prior_cap = previous
        change = market_cap - prior_cap
        change_percent = (change / prior_cap * 100) if prior_cap else 0.0
        observations.append({
            "source": "coingecko", "external_id": identifier, "kind": "market_activity",
            "description": f"{coin} market capitalisation change over the day",
            "actor_key": "aggregate:coingecko", "counterparty_key": "",
            "instrument_key": instrument_key, "action": "", "reason": "",
            "reason_basis": "unknown",
            "amount": repr(round(change, 2)), "unit": "usd_market_cap_change",
            "currency": "USD", "basis": "estimated",
            "occurred_at": moment.isoformat(), "observed_at": moment.isoformat(),
            "available_at": (moment + timedelta(days=1)).isoformat(),
            "source_url": url,
            "evidence": json.dumps({
                "method": MARKET_ACTIVITY, "coin": coin, "quote": "usd",
                "market_cap_usd": repr(round(market_cap, 2)),
                "market_cap_change_usd": repr(round(change, 2)),
                "market_cap_change_percent": repr(round(change_percent, 4)),
                "prior_market_cap_usd": repr(round(prior_cap, 2)),
                "prior_day": prior_moment.date().isoformat(),
                "close_usd": repr(round(point["close"], 8)) if "close" in point else None,
                "volume_usd": repr(round(point["volume"], 2)) if "volume" in point else None,
                "retrieved_at": retrieved,
                "semantics": "market capitalisation change is a capital-activity proxy; it "
                             "rises with price as well as demand and cannot separate buying "
                             "from selling or spot from derivative notional",
                "availability_basis": "a daily value is treated as available from the "
                                      "following UTC day"},
                sort_keys=True, separators=(",", ":"))})
        previous = (moment, market_cap)
    if not observations:
        raise ValueError("market activity produced no comparable daily pairs")
    observations.sort(key=lambda row: (row["observed_at"], row["external_id"]))
    stored = _store(conn, observations)
    report = {
        "method": MARKET_ACTIVITY, "source": "coingecko", "coin": coin,
        "instrument_key": instrument_key, "entity_id": entity_id, "source_url": url,
        "retrieved_at": retrieved, "request_started_at": started.isoformat(),
        "window": {"days": days, "oldest_observed_at": observations[0]["observed_at"],
                   "newest_observed_at": observations[-1]["observed_at"]},
        "received": len(moments), "stored": stored, "skipped": skipped,
        "scope": "instrument_bound",
        "response_sha256": hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=True, allow_nan=False,
                       separators=(",", ":")).encode("utf-8")).hexdigest(),
        "truth_verified": False, "warnings": list(client.warnings) + errors,
        "limitations": [
            "Market capitalisation is price times supply, so a change reflects price as well "
            "as demand. It is a capital-activity proxy, not a measured flow of money.",
            "Nothing here separates a purchase from a sale, a new buyer from capital rotating "
            "out of another asset, or spot volume from derivative notional.",
            "Volume is total reported volume across venues and aggregators and is not a "
            "verified consolidated figure.",
            "A second public price source, useful for cross-checking the exchange series, but "
            "it agrees with the exchange only within its own sampling and rounding.",
            "The daily value is treated as available from the following UTC day, a stated "
            "conservative assumption rather than a verified publication time.",
            "No accuracy, calibration or trading claim is made.",
        ],
    }
    registry.set_payload_fingerprint(conn, client)
    registry.clear_failure_recorder(conn)
    registry.record_ingest_run(
        conn, ACTIVITY_SOURCE, started.isoformat(), _now().isoformat(),
        query=instrument_key, indicator=coin,
        fetched=len(moments), stored=stored, skipped=sum(skipped.values()), pages=1,
        total=len(observations), truncated=False,
        request_sha256=hashlib.sha256(url.encode("utf-8")).hexdigest(),
        payload_sha256=report["response_sha256"],
        warnings=report["warnings"],
        coverage=f"daily {coin} market activity for {instrument_key}; a market-cap proxy, "
                 "not a net flow")
    return {"observations": observations}, report


def _store(conn, observations):
    stored = 0
    for row in observations:
        registry.add_observation(conn, **row)
        stored += 1
    return stored
