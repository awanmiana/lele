"""Detect what was happening in the world during a window before a price move.

One detector, four channels, one schema. For a window that ends at a move start
the detector returns every signal it can see, each tagged with the cause family
it belongs to, so a later step can count reasons per move, compare them with
quiet periods, and read the same structure off the present.

Channels
- ``stored``      observations already in the registry for the instrument:
                  money flows, transfers, ETF and stablecoin flows, liquidations,
                  policy and regulatory events, macro releases, filings.
- ``news``        bounded GDELT headlines, each classified into a reason
                  category and scored for sentiment.
- ``structure``   Binance futures leverage state: funding, open interest,
                  long/short skew, order-book imbalance, taker order flow.
- ``market``      price and volume behaviour read from stored bars, including a
                  volume z-score against the trailing baseline.

A signal is an observation or a keyword match inside a time window. A family is
a grouping of signal kinds. Neither is a cause, and nothing here forecasts.
"""
import json
import math
import time
from ..core import clock
from datetime import datetime, timedelta, UTC
from decimal import Decimal, InvalidOperation, localcontext
from collections.abc import Callable

from ..core import registry
from ..fetchers import evidence, news, news_rss
from . import causes

METHOD = "pre_move_signal_scan_v1"
CHANNELS = ("stored", "news", "structure", "market")
DEFAULT_HORIZONS = (24, 48, 72)
MAX_HORIZONS = 4
MAX_HORIZON_HOURS = 24 * 90
TILE_HOURS = 24
MAX_SIGNALS = 2000
NEWS_ATTEMPTS = 3
NEWS_PAUSE_SECONDS = 8.0
NEWS_PROVIDERS = ("gdelt", "google_news")

FAMILIES = {
    "money_movement": "money entering or leaving, funds pulled for investment, "
                      "transfers, liquidations and leverage",
    "policy_decision": "a government, regulator, court or corporate actor changing "
                       "rules, rates or stated policy",
    "market_structure": "leverage, positioning, order-book and funding conditions that "
                        "can amplify or fake a move",
    "sentiment_emotion": "reported emotion, mood, crowding or hype in coverage",
    "security_incident": "hacks, thefts, exploits, outages or insolvency",
    "macro_environment": "rates, inflation, employment, trade and energy conditions",
    "corporate_fundamental": "earnings, guidance, valuation and corporate actions",
}

OBSERVATION_FAMILY = {
    "fund_flow": "money_movement",
    "exchange_transfer": "money_movement",
    "onchain_transfer": "money_movement",
    "etf_flow": "money_movement",
    "stablecoin_mint_burn": "money_movement",
    "liquidation": "money_movement",
    "liquidation_level": "market_structure",
    "open_interest": "market_structure",
    "funding_rate": "market_structure",
    "order_book_imbalance": "market_structure",
    "long_short_account_ratio": "market_structure",
    "top_long_short_position_ratio": "market_structure",
    "margin_debt": "money_movement",
    "short_interest": "money_movement",
    "options_positioning": "market_structure",
    "cot_positioning": "market_structure",
    "cot_open_interest": "market_structure",
    "order_flow": "market_structure",
    "trade_print": "market_structure",
    "block_trade": "money_movement",
    "trade_flow": "money_movement",
    "energy_flow": "money_movement",
    "social_sentiment": "sentiment_emotion",
    "news_event": "sentiment_emotion",
    "market_context": "sentiment_emotion",
    "capitulation_indicator": "sentiment_emotion",
    "political_event": "policy_decision",
    "geopolitical_event": "policy_decision",
    "regulatory_action": "policy_decision",
    "macro_release": "macro_environment",
    "labor_metric": "macro_environment",
    "calendar_event": "macro_environment",
    "filing_event": "corporate_fundamental",
    "insider_trade": "corporate_fundamental",
    "holding": "corporate_fundamental",
    "investment_adviser": "corporate_fundamental",
    "market_shock": "security_incident",
    "decision": "policy_decision",
    "stablecoin_supply": "money_movement",
    "market_activity": "money_movement",
}
GLOBAL_CONTEXT_KINDS = frozenset({
    "stablecoin_supply", "social_sentiment", "market_context", "capitulation_indicator",
    "macro_release", "labor_metric", "calendar_event", "political_event",
    "geopolitical_event", "regulatory_action", "market_shock",
})
CATEGORY_FAMILY = {
    "macro_rates": "macro_environment",
    "regulation_legal": "policy_decision",
    "etf_institutional": "money_movement",
    "security_incident": "security_incident",
    "exchange_incident": "security_incident",
    "whale_onchain": "money_movement",
    "listing_trading": "market_structure",
    "mining_energy": "macro_environment",
    "corporate_earnings": "corporate_fundamental",
    "geopolitics": "macro_environment",
    "market_sentiment": "sentiment_emotion",
}
MARKET_FAMILY = "market_structure"


def families():
    return dict(FAMILIES)


def validate(horizons, channels, topic, language, articles, include_structure,
             cache_seconds=0, provider=NEWS_PROVIDERS[0]):
    if not isinstance(horizons, (tuple, list)) or not horizons:
        raise ValueError("horizons must be a nonempty sequence of hour counts")
    if len(horizons) > MAX_HORIZONS:
        raise ValueError(f"at most {MAX_HORIZONS} horizons are allowed")
    for value in horizons:
        if type(value) is not int or not 1 <= value <= MAX_HORIZON_HOURS:
            raise ValueError(f"each horizon must be an integer from 1 to {MAX_HORIZON_HOURS}")
    if len(set(horizons)) != len(horizons):
        raise ValueError("horizons must be distinct")
    if not isinstance(channels, (tuple, list)) or not channels:
        raise ValueError("channels must be a nonempty sequence")
    for name in channels:
        if name not in CHANNELS:
            raise ValueError(f"channel must be one of {', '.join(CHANNELS)}")
    if not isinstance(topic, str) or not news.TOPIC_RE.fullmatch(topic):
        raise ValueError("topic must be 1..64 letters, digits, spaces, hyphens or underscores")
    if language is not None and language not in news.LANGUAGES:
        raise ValueError(f"news language must be one of {', '.join(news.LANGUAGES)}")
    if type(articles) is not int or not 1 <= articles <= news.MAX_ARTICLES:
        raise ValueError(f"articles must be an integer from 1 to {news.MAX_ARTICLES}")
    if type(include_structure) is not bool:
        raise ValueError("include structure must be a boolean")
    if not isinstance(cache_seconds, (int, float)) or cache_seconds < 0:
        raise ValueError("news cache seconds must be a nonnegative number")
    if provider not in NEWS_PROVIDERS:
        raise ValueError(f"news provider must be one of {', '.join(NEWS_PROVIDERS)}")


def _signal(channel, family, kind, label, observed_at, *, magnitude=None, unit="",
            direction="unknown", source="", source_url="", evidence_text="", confidence="",
            detail=None, covers_hours=None):
    return {
        "channel": channel, "family": family, "kind": kind, "label": label,
        "observed_at": observed_at, "magnitude": magnitude, "unit": unit,
        "direction": direction, "source": source, "source_url": source_url,
        "evidence": evidence_text, "confidence": confidence,
        "covers_hours": covers_hours, "detail": detail or {},
    }


def _time(value):
    return datetime.fromisoformat(value).astimezone(UTC)


def stored(conn, instrument_key, start, end):
    """Observations already recorded inside the window, for this instrument and
    for the market as a whole.

    Two scopes are read. Rows bound to the instrument answer what happened to
    this asset, including any flow a wired source recorded for it. Rows with no
    instrument key are market-wide context that applies to any instrument, such
    as a central bank release, a regulatory action, a sentiment index or
    aggregate stablecoin supply, and they are restricted to a fixed kind
    whitelist: a routed flow about two named organisations is never presented as
    context for an unrelated instrument.

    Only rows available before the window closes are returned, so a later
    restatement or retrieval cannot enter an earlier window. Rows that exist but
    were not yet available are counted, never silently dropped, because their
    absence is not evidence that nothing happened.
    """
    columns = ("source, external_id, kind, description, actor_key, counterparty_key, action,"
               " reason, reason_basis, amount, unit, currency, basis, occurred_at,"
               " observed_at, available_at, source_url, evidence")
    rows = conn.execute(
        f"""SELECT {columns}, instrument_key AS scope_key FROM observations
            WHERE (instrument_key=? OR instrument_key='') AND occurred_at>=? AND occurred_at<?
            ORDER BY occurred_at""",
        (instrument_key, start.isoformat(), end.isoformat()),
    ).fetchall()
    signals = []
    excluded_late: dict[str, int] = {}
    unmapped: dict[str, int] = {}
    for row in rows:
        if not row["scope_key"] and row["kind"] not in GLOBAL_CONTEXT_KINDS:
            unmapped[row["kind"]] = unmapped.get(row["kind"], 0) + 1
            continue
        family = OBSERVATION_FAMILY.get(row["kind"])
        if family is None:
            unmapped[row["kind"]] = unmapped.get(row["kind"], 0) + 1
            continue
        if row["available_at"] and _time(row["available_at"]) > end:
            excluded_late[row["kind"]] = excluded_late.get(row["kind"], 0) + 1
            continue
        signals.append(_signal(
            "stored", family, row["kind"], row["description"] or row["kind"],
            row["occurred_at"], magnitude=row["amount"], unit=row["unit"],
            direction=row["action"] or "unknown", source=row["source"],
            source_url=row["source_url"], evidence_text=row["evidence"],
            confidence=row["basis"], detail={
                "actor": row["actor_key"], "counterparty": row["counterparty_key"],
                "action": row["action"], "reason": row["reason"],
                "reason_basis": row["reason_basis"], "currency": row["currency"],
                "external_id": row["external_id"],
                "scope": "instrument" if row["scope_key"] else "market_wide"}))
    scopes = {"instrument": 0, "market_wide": 0}
    for item in signals:
        scopes[item["detail"]["scope"]] = scopes.get(item["detail"]["scope"], 0) + 1
    return signals, {
        "status": "ok", "rows_in_window": len(rows), "signals": len(signals),
        "instrument_signals": scopes["instrument"],
        "market_wide_signals": scopes["market_wide"],
        "excluded_available_after_window": sum(excluded_late.values()),
        "excluded_available_after_window_by_kind": dict(sorted(excluded_late.items())),
        "excluded_available_after_window_note":
            "these rows fall inside the window by occurrence but were recorded as available "
            "after it, so they are newer than this reference point; read them as current "
            "context rather than as context the window could have contained",
        "excluded_unmapped_kind": sum(unmapped.values()),
        "excluded_unmapped_kind_by_kind": dict(sorted(unmapped.items())),
        "market_wide_kinds": sorted(GLOBAL_CONTEXT_KINDS),
        "window": {"start_inclusive": start.isoformat(), "end_exclusive": end.isoformat()}}


def news_signals(conn, entity_id, topic, start, end, language, articles, cache_seconds=0,
                 attempts=NEWS_ATTEMPTS, pause=NEWS_PAUSE_SECONDS,
                 provider=NEWS_PROVIDERS[0], now=None):
    """Bounded headline sweep for one window, classified and scored for sentiment.

    The public news index throttles and occasionally answers a valid request
    with an empty list, so a tile is retried and an unreachable tile is reported
    as such. One failing tile must not hide the other channels.
    """
    moment = now if now is not None else clock.now()
    floor = moment - timedelta(hours=MAX_HORIZON_HOURS)
    if start < floor:
        return [], {"status": "outside_provider_reach", "window": {
            "start_inclusive": start.isoformat(), "end_exclusive": end.isoformat()},
            "provider_reach_days": MAX_HORIZON_HOURS // 24}
    hours = max(1, int((end - start).total_seconds() // 3600) + 1)
    payload: dict | None = None
    report: dict | None = None
    errors: list[str] = []
    reader: Callable[..., tuple[dict, dict]] = (news.fetch_news if provider == "gdelt"
                                                else news_rss.fetch_news_feed)
    for attempt in range(1, attempts + 1):
        try:
            payload, report = reader(conn, entity_id, topic, hours=hours, limit=articles,
                                     end=end.isoformat(), language=language,
                                     cache_seconds=cache_seconds)
            break
        except Exception as error:
            errors.append(f"attempt {attempt}: {type(error).__name__}: {error}")
            if attempt < attempts:
                time.sleep(pause * attempt)
    if payload is None or report is None:
        return [], {"status": "failed", "attempts": attempts, "errors": errors,
                    "window": {"start_inclusive": start.isoformat(),
                               "end_exclusive": end.isoformat()}}
    rows = [row for row in payload["observations"]
            if start <= _time(row["observed_at"]) < end]
    signals = []
    for row in rows:
        found, matches = causes.classify(row.get("description", ""))
        headline = row.get("description", "")
        scored = causes.headline_sentiment(headline)
        primary = found[0] if found else causes.UNCATEGORIZED
        family = CATEGORY_FAMILY.get(primary, "sentiment_emotion")
        if "market_sentiment" in found:
            family = "sentiment_emotion"
        signals.append(_signal(
            "news", family, primary, headline, row["observed_at"],
            magnitude=scored.get("score"), unit="sentiment",
            direction=scored.get("direction") or "unknown", source=news.PLATFORM,
            source_url=row.get("source_url", ""), evidence_text=json.dumps(
                {"categories": found, "matched_terms": matches, "sentiment": scored},
                sort_keys=True, separators=(",", ":")),
            confidence=scored.get("confidence") or "unknown",
            detail={"categories": found, "domain": row.get("platform", ""),
                    "sentiment_status": scored.get("status"),
                    "sentiment_method": scored.get("method"),
                    "sentiment_reason": scored.get("reason")}))
    observations = payload["observations"]
    recent = end > moment - timedelta(hours=24)
    if not rows and recent:
        status, note = "empty_recent_window", (
            "the window ends within a day of now and returned no article; a public news index "
            "lags its own live coverage, so read this as unknown rather than as no news")
    elif not rows:
        status, note = "empty", ""
    else:
        status, note = "ok", ""
    coverage = {"status": status, "status_note": note, "provider": provider,
                "articles_returned": len(observations),
                "articles_in_window": len(rows),
                "truncated_at_cap": len(observations) >= articles,
                "source_url": report["source_url"], "retrieved_at": report["retrieved_at"],
                "window": {"start_inclusive": start.isoformat(), "end_exclusive": end.isoformat()},
                "uncategorized": sum(1 for item in signals
                                     if item["kind"] == causes.UNCATEGORIZED),
                "sentiment_scored": sum(1 for item in signals if item["magnitude"] is not None),
                "sentiment_unknown": sum(1 for item in signals
                                         if item["magnitude"] is None)}
    return signals, coverage


def structure(conn, entity_id, symbol, start, end):
    """Binance futures leverage state for the window ending at `end`."""
    span = int((end - start).total_seconds() // 300)
    if span < 2:
        return [], {"status": "window_too_short", "minutes": span * 5}
    limit = min(span, evidence.MAX_SLOTS)
    covered_hours = limit * 5 // 60
    if covered_hours < 1:
        return [], {"status": "window_below_one_hour", "minutes": span * 5}
    try:
        payload, report = evidence.fetch_evidence(conn, entity_id, "binance-futures", symbol,
                                                  limit=limit, end=end.isoformat())
    except Exception as error:
        return [], {"status": "failed", "error": f"{type(error).__name__}: {error}"}
    rows = [row for row in payload["observations"]
            if start <= _time(row["observed_at"]) < end]
    by_kind: dict[str, list] = {}
    for row in rows:
        by_kind.setdefault(row["kind"], []).append(row)
    signals = []
    for kind, items in sorted(by_kind.items()):
        family = OBSERVATION_FAMILY.get(kind, MARKET_FAMILY)
        values = []
        for item in items:
            try:
                values.append(Decimal(str(item.get("measurement", {}).get("value", "0"))))
            except (InvalidOperation, ValueError, TypeError, AttributeError):
                continue
        if not values:
            continue
        with localcontext() as context:
            context.prec = 60
            last = values[-1]
            first = values[0]
            mean = sum(values, Decimal(0)) / len(values)
            ordered = sorted(values)
            middle = len(ordered) // 2
            median = (ordered[middle] if len(ordered) % 2
                      else (ordered[middle - 1] + ordered[middle]) / 2)
            change = ((last - median) / abs(median) * 100) if median != 0 else Decimal(0)
        signals.append(_signal(
            "structure", family, kind, f"{kind} over the pre-move window",
            items[-1]["observed_at"], magnitude=str(last), unit=items[0].get("measurement", {})
            .get("unit", ""), direction="up" if change > 0 else "down" if change < 0 else "flat",
            source="binance-futures",
            source_url=json.dumps(report.get("source_urls", {}), sort_keys=True,
                                  separators=(",", ":")),
            evidence_text=json.dumps({"points": len(values), "first": str(first),
                                      "last": str(last), "mean": str(mean),
                                      "median": str(median), "min": str(min(values)),
                                      "max": str(max(values)),
                                      "change_vs_median_percent": str(change)},
                                     sort_keys=True, separators=(",", ":")),
            confidence="observed",
            detail={"points": len(values), "change_vs_median_percent": str(change),
                    "mean": str(mean), "median": str(median), "min": str(min(values)),
                    "max": str(max(values)), "last": str(last)},
            covers_hours=covered_hours))
    return signals, {"status": "ok", "records": len(rows), "kinds": sorted(by_kind),
                     "requested_minutes": span, "used_minutes": limit * 5,
                     "covers_hours": covered_hours, "truncated": span > evidence.MAX_SLOTS,
                     "source_urls": report.get("source_urls", {}),
                     "retrieved_at": report["retrieved_at"]}


def market(conn, instrument_key, interval_seconds, start, end, baseline_bars=90):
    """Price and volume behaviour over the window, scored against trailing bars."""
    times: list[datetime] = []
    closes: list[Decimal] = []
    volumes: list[Decimal] = []
    for row in registry.list_price_bars(conn, instrument_key, interval_seconds, limit=20000):
        try:
            close = Decimal(row["close"])
            volume = Decimal(row["volume"] or 0)
            moment = _time(row["open_time"])
        except (InvalidOperation, ValueError, TypeError, ArithmeticError):
            continue
        times.append(moment)
        closes.append(close)
        volumes.append(volume)
    if len(closes) < baseline_bars + 2:
        return [], {"status": "insufficient_bars", "bars": len(closes),
                    "required": baseline_bars + 2}
    inside = [index for index, moment in enumerate(times) if start <= moment < end]
    if not inside:
        return [], {"status": "no_bars_in_window",
                    "window": {"start_inclusive": start.isoformat(),
                               "end_exclusive": end.isoformat()}}
    first, last = inside[0], inside[-1]
    signals = []
    with localcontext() as context:
        context.prec = 80
        change = ((closes[last] - closes[first]) / closes[first] * 100
                  if closes[first] else Decimal(0))
        span: list[Decimal] = [
            abs((closes[index] - closes[index - 1]) / closes[index - 1] * 100)
            for index in inside[1:] if closes[index - 1]]
        realized = (sum(span, Decimal(0)) / len(span)) if span else Decimal(0)
        prior = volumes[max(0, first - baseline_bars):first]
        window_volume = sum(volumes[first:last + 1], Decimal(0))
        mean_volume: Decimal
        deviation: Decimal
        z: Decimal | None
        if prior:
            mean_volume = sum(prior, Decimal(0)) / len(prior)
            variance = sum(((value - mean_volume) ** 2 for value in prior), Decimal(0)) / len(prior)
            deviation = variance.sqrt()
            z = ((window_volume / len(inside) - mean_volume) / deviation) if deviation > 0 else None
        else:
            mean_volume, deviation, z = Decimal(0), Decimal(0), None
        if realized > 0:
            signals.append(_signal(
                "market", MARKET_FAMILY, "realized_volatility", "mean absolute bar move in window",
                times[last].isoformat(), magnitude=str(realized), unit="percent",
                direction="up", source="price_bars", confidence="derived"))
        if z is not None and abs(z) > 0:
            signals.append(_signal(
                "market", MARKET_FAMILY, "volume_zscore", "window volume against trailing bars",
                times[last].isoformat(), magnitude=str(z), unit="sigma",
                direction="up" if z > 0 else "down", source="price_bars",
                confidence="derived",
                evidence_text=json.dumps({"baseline_bars": len(prior),
                                          "baseline_mean_volume": str(mean_volume)},
                                         sort_keys=True, separators=(",", ":")),
                detail={"baseline_bars": len(prior), "baseline_mean_volume": str(mean_volume),
                        "window_bars": len(inside), "window_volume": str(window_volume)}))
    return signals, {"status": "ok", "window_bars": len(inside),
                     "window": {"start_inclusive": start.isoformat(),
                                "end_exclusive": end.isoformat(),
                                "first_bar": times[first].isoformat(),
                                "last_bar": times[last].isoformat()},
                     "baseline_bars": len(prior) if prior else 0,
                     "volume_zscore_status": ("ok" if z is not None else
                                             ("zero_variance_baseline" if prior
                                              else "no_baseline")),
                     "change_percent": str(change),
                     "mean_abs_bar_move_percent": str(realized)}


def _tiles(start, end, tile_hours=TILE_HOURS):
    step = timedelta(hours=tile_hours)
    cursor = start
    out = []
    while cursor < end:
        upper = min(cursor + step, end)
        out.append((cursor, upper))
        cursor = upper
    return out


def _bucket(signals, horizons, end):
    """Group pooled signals into each requested lookback horizon.

    A signal that summarises a shorter span than the horizon, such as a futures
    leverage state covering 25 hours, is excluded from wider horizons rather
    than being counted as if it described them.
    """
    grouped = {}
    for hours in horizons:
        lower = end - timedelta(hours=hours)
        inside = [item for item in signals
                  if lower <= _time(item["observed_at"]) < end
                  and (item["covers_hours"] is None or hours <= item["covers_hours"])]
        counts: dict[str, int] = {}
        kinds: dict[str, int] = {}
        for item in inside:
            counts[item["family"]] = counts.get(item["family"], 0) + 1
            kinds[item["kind"]] = kinds.get(item["kind"], 0) + 1
        grouped[str(hours)] = {
            "hours": hours, "signals": len(inside), "families": dict(sorted(counts.items())),
            "kinds": dict(sorted(kinds.items())), "distinct_families": len(counts),
            "distinct_kinds": len(kinds), "items": inside}
    return grouped


def detect(conn, entity_id, instrument_key, interval_seconds, *, end, horizons=DEFAULT_HORIZONS,
           channels=CHANNELS, topic="bitcoin", language=None, articles=None,
           include_structure=True, baseline_bars=90, symbol="BTCUSDT", store=False,
           role="move", control_key="", news_cache_seconds=21600,
           news_provider=NEWS_PROVIDERS[0]):
    """Detect every visible signal in each lookback horizon before `end`.

    `end` is the move start: signals at or after it are excluded, so a move can
    never be explained by an article published after the price already moved.
    """
    horizons = tuple(sorted(horizons))
    articles = news.MAX_ARTICLES if articles is None else articles
    validate(horizons, channels, topic, language, articles, include_structure,
             news_cache_seconds, news_provider)
    if not isinstance(end, str) or not end:
        raise ValueError("end must be an aware ISO8601 timestamp")
    boundary = _time(end)
    widest = max(horizons)
    earliest = boundary - timedelta(hours=widest)
    report = {
        "method": METHOD, "instrument_key": instrument_key,
        "interval_seconds": interval_seconds, "role": role, "control_key": control_key or None,
        "parameters": {"horizons": list(horizons), "channels": list(channels), "topic": topic,
                       "language_filter": language, "articles_per_request": articles,
                       "include_structure": include_structure, "baseline_bars": baseline_bars,
                       "symbol": symbol, "tile_hours": TILE_HOURS,
                       "news_cache_seconds": news_cache_seconds,
                       "news_provider": news_provider},
        "window": {"end_exclusive": boundary.isoformat(),
                   "earliest": earliest.isoformat(), "widest_horizon_hours": widest},
        "horizons": {}, "coverage": {"channels": {}, "truth_verified": False},
        "family_definitions": families(), "limitations": [
            "A signal is an observation or a keyword match inside a lookback window. It records "
            "what was published or reported before the move, not what caused it.",
            "Sentiment is a fixed English word list applied to a headline, and its confidence is "
            "matched mentions over word tokens; it is not a reading of the article.",
            "The news provider caps articles per request, so a busy window is truncated and the "
            "cap is reported per request; a truncated window undercounts reasons.",
            "One keyword phrase against one news provider per run. Providers throttle, lag and "
            "return differently, so a reason count is a property of this provider, not of the "
            "world.",
            "Without an explicit language filter the provider returns every language it indexes, "
            "and headlines the English lexicon cannot read are counted as uncategorized.",
            "Structure signals describe futures leverage state, which is consistent with "
            "manipulation but does not identify a manipulator or prove intent.",
            "Money-movement signals exist only where a wired source recorded a flow for this "
            "instrument; a missing flow means unknown coverage, not no money moved.",
            "Windows are nested, so a 72-hour family count contains its 24-hour count. Counts are "
            "not additive across horizons.",
            "No forecast of direction, magnitude or timing is produced or implied.",
        ],
    }
    pooled: list[dict] = []
    if "stored" in channels:
        found, coverage = stored(conn, instrument_key, earliest, boundary)
        pooled.extend(found)
        coverage["kinds"] = dict(sorted(_tally(found).items()))
        report["coverage"]["channels"]["stored"] = coverage
    if "news" in channels:
        requests = []
        for tile_start, tile_end in _tiles(earliest, boundary):
            found, coverage = news_signals(conn, entity_id, topic, tile_start, tile_end,
                                           language, articles, news_cache_seconds,
                                           provider=news_provider)
            pooled.extend(found)
            requests.append(coverage)
        report["coverage"]["channels"]["news"] = {
            "status": "ok" if all(item["status"] == "ok" for item in requests) else "partial",
            "signals": sum(item.get("articles_in_window", 0) for item in requests),
            "requests": requests,
            "truncated_requests": sum(1 for item in requests if item.get("truncated_at_cap"))}
    if "structure" in channels and include_structure:
        found, coverage = structure(conn, entity_id, symbol,
                                    boundary - timedelta(hours=min(widest, 25)), boundary)
        pooled.extend(found)
        report["coverage"]["channels"]["structure"] = coverage
    if "market" in channels:
        found, coverage = market(conn, instrument_key, interval_seconds, earliest, boundary,
                                 baseline_bars)
        pooled.extend(found)
        report["coverage"]["channels"]["market"] = coverage
    if len(pooled) > MAX_SIGNALS:
        pooled.sort(key=lambda item: item["observed_at"], reverse=True)
        report["coverage"]["truncated_signals"] = len(pooled) - MAX_SIGNALS
        pooled = pooled[:MAX_SIGNALS]
    grouped = _bucket(pooled, horizons, boundary)
    for hours, bucket in grouped.items():
        bucket.pop("items")
        report["horizons"][hours] = bucket
    if store and role == "move":
        now = clock.now().replace(microsecond=0).isoformat()
        for bucket in grouped.values():
            summary = {key: value for key, value in bucket.items() if key != "items"}
            registry.add_cause_scan(
                conn, instrument_key=instrument_key, interval_seconds=interval_seconds,
                move_hours=widest, as_of=boundary.isoformat(),
                change_percent=str(summary.get("change_percent", "0")),
                observed_at=now, available_at=now,
                tier=None, direction=None, historical_moves=0,
                current_categories=json.dumps(sorted(summary["families"]),
                                              separators=(",", ":")),
                matched_categories=json.dumps(
                    {"horizons": {key: value["families"] for key, value in
                                  ((item, grouped[item]) for item in grouped)},
                     "signals": summary["signals"]}, sort_keys=True, separators=(",", ":")),
                news_articles=report["coverage"]["channels"].get("news", {}).get("signals", 0),
                score=str(summary["distinct_families"]),
                evidence=json.dumps({"scan": summary,
                                     "coverage": {key: value.get("status")
                                                  for key, value
                                                  in report["coverage"]["channels"].items()}},
                                    sort_keys=True, separators=(",", ":")))
    report["signals"] = sorted(pooled, key=lambda item: item["observed_at"])
    report["summary"] = {
        "signals": len(pooled),
        "families": dict(sorted(_tally(pooled, "family").items())),
        "channels": dict(sorted(_tally(pooled, "channel").items())),
        "widest_horizon": str(widest),
        "distinct_families": len({item["family"] for item in pooled}),
    }
    return report


def _tally(signals, key="kind"):
    counts: dict[str, int] = {}
    for item in signals:
        counts[item[key]] = counts.get(item[key], 0) + 1
    return counts


def family_of(kind):
    return OBSERVATION_FAMILY.get(kind) or CATEGORY_FAMILY.get(kind)


def spread(values):
    """Absolute dispersion of a list of numbers, or None when undefined."""
    numbers = [float(value) for value in values if value is not None]
    if len(numbers) < 2:
        return None
    mean = sum(numbers) / len(numbers)
    variance = sum((value - mean) ** 2 for value in numbers) / len(numbers)
    return math.sqrt(variance)
