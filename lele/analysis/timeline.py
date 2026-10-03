"""Answer the question the tool could not: "what is stored in this window?"

Three questions, one window:

* what moved, measured from stored bars rather than asserted;
* what else is recorded inside that window and in the hours before it, by
  family, with provenance;
* what is *not* recorded, which is the half that decides whether an answer can
  be believed.

Two things this module refuses to do, because the distinction is the whole
value of the tool.

It does not call anything a cause. Every record it returns is a **temporal
coincidence**: something with a timestamp inside the window, or inside the
pre-window. Whether that something moved the price is a separate question, and
the one thing a keyword match, a leverage reading or a policy publication
cannot answer. The distinction is carried in the output as `relation`, which is
always `coincident` or `precursor`, never `cause`.

It does not present absence as evidence. A family with no records is reported
as `no_coverage` with the command that would create it, because "nothing was
recorded" and "nothing happened" are different claims and only the first is
knowable from a registry.
"""
from datetime import UTC, datetime, timedelta
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext

from ..core import registry
from . import moves, signals

METHOD = "window_explain_v1"
MIN_INTERVAL_SECONDS = 60
MAX_INTERVAL_SECONDS = 24 * 3600
MAX_LIMIT = 20_000
DEFAULT_LIMIT = 2_000

# Units for which a percent change is not a meaningful statement. A ratio, an
# index and a rate are not additive quantities, so "it rose 4%" implies a scale
# they do not have.
BOUNDED_UNITS = frozenset({"ratio", "rate", "index_0_100", "percent", "count", "accounts"})

# Kinds whose reading is a change *over its own interval* rather than a level.
# For these, the endpoints are two arbitrary intervals: a percent change between
# them is a ratio of two near-zero numbers and means nothing, while their sum
# over the window is the quantity of interest. `order_flow` is a net taker delta
# per closed candle, so the net over a week is real and its percent change is not.
# `open_interest` reads "change in open interest notional between consecutive
# five-minute bars" and `market_activity` is a day-on-day capitalisation change,
# so all three are deltas rather than levels.
DELTA_KINDS = frozenset({"order_flow", "market_activity", "open_interest"})

# What each family means, stated once, so the output cannot quietly imply that a
# leverage reading and a settled payment are the same kind of thing.
FAMILY_MEANING = {
    "money_movement": "a recorded measurement or document about capital, supply or obligation; "
                      "a balance or an aggregate is not a settled transfer",
    "market_structure": "positioning, leverage, open interest or order-flow state; it shows "
                        "what participants were positioned for, never who or why",
    "sentiment_emotion": "a provider aggregate of emotion, attention or activity; a composite "
                         "index whose composition is not audited here",
    "policy_decision": "a published rule, order, statement or filing; publication is a fact, "
                       "its effect on a price is not",
    "macro_environment": "an official statistical release; it describes an economy, not a market",
    "corporate_fundamental": "a filed or reported corporate event or position; a position "
                             "snapshot is not a trade",
    "security_incident": "a recorded incident affecting infrastructure or access",
    "other": "a record whose kind has no family mapping; treat its meaning as unestablished",
}

# Channels a user would expect, and the command that creates each. Reported as
# `no_coverage` with the remedy rather than silently absent.
CHANNEL_REMEDY = {
    "stored_bars": "lele fetch-history ID SYMBOL --interval {interval}",
    "detected_moves": "lele moves ID --thresholds 3 5 7 11",
    "volatility_instances": "lele volatility-analyze ID",
    "stored_observations": "lele fetch-evidence / fetch-cot / fetch-short / fetch-treasury "
                           "/ fetch-political / fetch-sentiment / fetch-stablecoins",
    "headlines": "lele fetch-news-feed ID TOPIC --output news.json",
    "documented_money_flows": "lele flows import flows.json",
    "price_anomalies": "lele rag detect-anomalies --instrument-key KEY --mode percentile "
                           "--level 95",
}


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def validate(entity_id, start, end, pre_hours, limit, threshold_percent, interval_seconds):
    if type(entity_id) is not int or not 1 <= entity_id <= 2 ** 63 - 1:
        raise ValueError("entity id must be a positive SQLite integer")
    if type(pre_hours) is not int or not 0 <= pre_hours <= 24 * 365:
        raise ValueError("pre hours must be an integer from 0 to 8760")
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be an integer from 1 to {MAX_LIMIT}")
    if type(threshold_percent) is not int or not 1 <= threshold_percent <= 1000:
        raise ValueError("threshold percent must be an integer from 1 to 1000")
    if type(interval_seconds) is not int or not MIN_INTERVAL_SECONDS <= interval_seconds \
            <= MAX_INTERVAL_SECONDS:
        raise ValueError("interval seconds must be an integer from 60 to 86400")
    begin, finish = _time(start), _time(end)
    if begin >= finish:
        raise ValueError("start must be before end")
    if finish - begin > timedelta(days=3650):
        raise ValueError("window must not exceed ten years")
    return begin, finish


def _relation(occurred_at: str, window_start: datetime, window_end: datetime) -> str:
    """Where the record sits relative to the window, stated as a fact."""
    moment = _time(occurred_at)
    if window_start <= moment < window_end:
        return "coincident"
    return "precursor"


def _round(value, digits="0.0001"):
    with localcontext(Context(prec=60, rounding=ROUND_HALF_EVEN)):
        return str(value.quantize(Decimal(digits)))


def _measure(row):
    """The number a record carries, or None when it carries none."""
    raw = (row.get("amount") or "").strip()
    if not raw:
        return None, "", ""
    unit = (row.get("unit") or "").strip()
    currency = (row.get("currency") or "").strip()
    try:
        return str(Decimal(raw)), unit, currency
    except ArithmeticError:
        # A stored value that will not parse is unknown, not zero and not absent.
        return None, unit, currency


def _window_behaviour(bars, window_start, window_end, threshold_percent):
    """What the stored bars themselves say happened, inside the window.

    Measured from closes, with the two traps handled explicitly: a bar missing
    inside the window, and a window shorter than one bar.
    """
    inside = [bar for bar in bars
              if window_start <= _time(bar["open_time"]) < window_end]
    result: dict = {
        "bars_in_window": len(inside),
        "measured": False,
        "open_price": None, "close_price": None, "change_percent": None,
        "high": None, "low": None, "volume": None,
        "largest_bar_move_percent": None, "largest_bar_move_at": None,
    }
    if len(inside) < 2:
        result["note"] = ("fewer than two stored bars inside the window, so no return can "
                          "be computed; widen the window or fetch more history")
        return result
    with localcontext(Context(prec=60, rounding=ROUND_HALF_EVEN)):
        closes = [Decimal(bar["close"]) for bar in inside]
        opens = [Decimal(bar["open"]) for bar in inside]
        highs = [Decimal(bar["high"]) for bar in inside]
        lows = [Decimal(bar["low"]) for bar in inside]
        first_open, last_close = opens[0], closes[-1]
        result["measured"] = True
        result["open_price"] = str(first_open)
        result["close_price"] = str(last_close)
        result["change_percent"] = _round((last_close - first_open) / first_open * 100) \
            if first_open != 0 else None
        result["high"] = str(max(highs))
        result["low"] = str(min(lows))
        volumes = []
        for bar in inside:
            try:
                volumes.append(Decimal(bar["volume"] or "0"))
            except ArithmeticError:
                continue
        if volumes:
            result["volume"] = str(sum(volumes))
            mean = sum(volumes) / Decimal(len(volumes))
            if len(volumes) > 1 and mean > 0:
                peak = max(volumes)
                result["volume_vs_window_mean"] = _round(
                    (peak - mean) / mean * Decimal(100))
        moves_found = []
        for index in range(1, len(inside)):
            start_close = closes[index - 1]
            if start_close == 0:
                continue
            change = (closes[index] - start_close) / start_close * 100
            moves_found.append((abs(change), index, change))
        if moves_found:
            magnitude, index, change = max(moves_found, key=lambda item: item[0])
            result["largest_bar_move_percent"] = _round(change)
            result["largest_bar_move_at"] = inside[index]["open_time"]
            result["largest_bar_move_direction"] = "up" if change > 0 else "down"
            if magnitude >= threshold_percent:
                result["exceeds_threshold"] = True
    return result


def _episodes(bars, threshold_percent, window_start, window_end):
    """Threshold ZigZag legs of the stored series that touch the window.

    Reused from `episodes` so the answer is the same segmentation `episodes`
    reports, not a second definition that could drift from it.
    """
    from . import episodes as episodes_module
    if len(bars) < 3:
        return []
    points = []
    for index, bar in enumerate(bars):
        try:
            points.append((_time(bar["open_time"]), Decimal(bar["close"]), index))
        except (ArithmeticError, ValueError):
            continue
    if len(points) < 3:
        return []
    legs = episodes_module._zigzag(points, Decimal(threshold_percent))
    inside = []
    for leg in legs:
        start = _time(leg["start"])
        end = _time(leg["end"])
        if end < window_start or start >= window_end:
            continue
        direction = leg["direction"]
        inside.append({
            "direction": direction, "relation": "coincident",
            "start": leg["start"], "end": leg["end"],
            "duration_hours": leg["duration_hours"],
            "magnitude_percent": _round(Decimal(leg["magnitude_percent"])),
            "start_price": leg["start_price"], "end_price": leg["end_price"],
            "start_bar": bars[leg["start_index"]]["open_time"],
            "end_bar": bars[leg["end_index"]]["open_time"],
            "label": f"{threshold_percent}% {'rally' if direction == 'up' else 'decline'} leg",
            "started_before_window": start < window_start,
        })
    return inside


def _records(window_rows, window_start, window_end, pre_start, limit):
    """Stored records as one timeline, split by where they sit.

    Two buckets because they are different claims. A record inside the window is
    coincident with it; a record before it is a precursor, which is the only
    ordering a timestamp can support and still not a cause.
    """
    inside, prior = [], []
    for row in window_rows:
        occurred = row.get("occurred_at") or ""
        if not occurred:
            continue
        moment = _time(occurred)
        amount, unit, currency = _measure(row)
        entry = {
            "kind": row["kind"],
            "family": signals.OBSERVATION_FAMILY.get(row["kind"], "other"),
            "source": row["source"],
            "source_url": row.get("source_url") or "",
            "occurred_at": occurred,
            "observed_at": row.get("observed_at") or "",
            "available_at": row.get("available_at") or "",
            "description": (row.get("description") or "")[:400],
            "amount": amount, "unit": unit, "currency": currency,
            "basis": row.get("basis") or "",
            "actor": row.get("actor_key") or "",
            "relation": _relation(occurred, window_start, window_end),
            "is_cause": False,
            "relation_note": "recorded at this time; whether it moved the price is a "
                             "separate question this tool does not answer",
        }
        if window_start <= moment < window_end:
            entry["relation"] = "coincident"
            inside.append(entry)
        elif moment >= pre_start:
            entry["relation"] = "precursor"
            prior.append(entry)
    inside.sort(key=lambda item: (item["occurred_at"], item["kind"]))
    prior.sort(key=lambda item: (item["occurred_at"], item["kind"]))
    return inside[:limit], prior[:limit], len(inside) + len(prior)


def explain(conn, entity_id: int, start: str, end: str, *, interval_seconds: int = 86400,
            pre_hours: int = 72, threshold_percent: int = 3,
            move_hours: int = 24, thresholds: tuple[int, ...] = (3, 5, 7, 11),
            limit: int = DEFAULT_LIMIT, include_market_wide: bool = True) -> dict:
    """Everything the registry holds for one instrument in one window.

    The window is an input, not a side effect: the same call with the same
    arguments returns the same answer, because nothing here reads a clock.
    """
    window_start, window_end = validate(entity_id, start, end, pre_hours, limit,
                                        threshold_percent, interval_seconds)
    pre_start = window_start - timedelta(hours=pre_hours)
    entity = registry.get_entity(conn, entity_id)
    if entity is None:
        raise ValueError(f"entity {entity_id} not found")
    key = entity["key"]
    read_start = (pre_start - timedelta(days=1)).isoformat()

    span = registry.stored_bar_span(conn, key, interval_seconds)
    all_bars = registry.list_price_bars(conn, key, interval_seconds, start=read_start,
                                        end=window_end.isoformat(), limit=MAX_LIMIT)
    behaviour = _window_behaviour(all_bars, window_start, window_end, threshold_percent)
    legs = _episodes(all_bars, threshold_percent, window_start, window_end)

    move_rows = registry.list_move_events(conn, key, interval_seconds, move_hours,
                                          start=read_start, end=window_end.isoformat(),
                                          limit=MAX_LIMIT)
    move_rows, unverified_moves = moves.verify_stored(conn, key, interval_seconds, move_rows)
    tiers: dict = {}
    for row in move_rows:
        if not (pre_start <= _time(row["end_time"]) < window_end):
            continue
        tier = row["tier"]
        entry = tiers.setdefault(tier, {"tier": tier, "threshold_percent":
                                        row["threshold_percent"], "count": 0,
                                        "up": 0, "down": 0, "largest": None,
                                        "examples": []})
        entry["count"] += 1
        entry["up" if row["direction"] == "up" else "down"] += 1
        magnitude = abs(Decimal(row["change_percent"]))
        if entry["largest"] is None or magnitude > abs(Decimal(entry["largest"])):
            entry["largest"] = row["change_percent"]
        if len(entry["examples"]) < 3:
            entry["examples"].append({
                "start_time": row["start_time"], "end_time": row["end_time"],
                "change_percent": row["change_percent"],
                "duration_hours": _round(
                    Decimal(int((_time(row["end_time"]) - _time(row["start_time"]))
                                .total_seconds())) / Decimal(3600)),
                "z_score": row["z_score"],
            })
    for entry in tiers.values():
        entry["examples"].sort(key=lambda item: item["start_time"])

    volatility = registry.volatility_in_window(conn, key, window_start.isoformat(),
                                               window_end.isoformat(), limit=limit)
    anomalies = registry.anomalies_in_window(conn, key, window_start.isoformat(),
                                             window_end.isoformat(), limit=limit)

    coverage_rows = registry.observation_coverage(
        conn, start=pre_start.isoformat(), end=window_end.isoformat(),
        instrument_key=key, include_market_wide=include_market_wide)
    window_result = registry.observations_in_window(
        conn, start=pre_start.isoformat(), end=window_end.isoformat(),
        instrument_key=key, include_market_wide=include_market_wide, limit=limit)
    inside, prior, total_records = _records(window_result["rows"], window_start,
                                            window_end, pre_start, limit)

    by_family: dict[str, int] = {}
    kinds: dict[str, int] = {}
    for row in coverage_rows:
        by_family[signals.OBSERVATION_FAMILY.get(row["kind"], "other")] = \
            by_family.get(signals.OBSERVATION_FAMILY.get(row["kind"], "other"), 0) + row["rows"]
        kinds[row["kind"]] = row["rows"]

    documented_flows = _documented_flows(conn, entity_id, pre_start, window_end, limit)

    channels = _channel_coverage(span, all_bars, behaviour, tiers, volatility, anomalies,
                                 inside, prior, documented_flows, window_result["truncated"])

    return {
        "method": METHOD,
        "instrument": {"entity_id": entity_id, "entity_key": key, "name": entity["name"],
                       "kind": entity["kind"]},
        "window": {"start": window_start.isoformat(), "end": window_end.isoformat(),
                   "pre_window_start": pre_start.isoformat(), "pre_hours": pre_hours,
                   "interval_seconds": interval_seconds,
                   "move_hours": move_hours, "thresholds_percent": list(thresholds),
                   "threshold_percent": threshold_percent},
        "price": behaviour,
        "episodes": legs,
        "moves": {"by_tier": [tiers[name] for name in sorted(tiers, key=_tier_sort)],
                  "total_rows": len(move_rows),
                  "unverified_rows": len(unverified_moves),
                  "unverified_sample": unverified_moves[:20],
                  "unverified_note": "a stored move whose window spans a missing bar or does "
                                     "not cover the recorded move length is not counted here; "
                                     "the detector that wrote it may predate the contiguity "
                                     "check, so its window is not the length it is labelled "
                                     "with",
                  "note": "tiers are cumulative: a move is recorded at every threshold it "
                          "clears, so the counts are nested, not independent"},
        "volatility_instances": volatility,
        "price_anomalies": anomalies,
        "records": {"inside_window": inside, "before_window": prior,
                    "total_in_scope": total_records,
                    "truncated": window_result["truncated"]},
        "coverage": {
            "bar_history": span,
            "stored_observations": {"kinds": kinds, "by_family": by_family,
                                   "rows": sorted(coverage_rows,
                                                  key=lambda row: row["kind"]),
                                   "in_scope_total": total_records,
                                   "truncated": window_result["truncated"]},
            "documented_money_flows": len(documented_flows),
            "channels": channels,
            "never_recorded": dict(
                sorted((kind, value) for kind, value
                       in registry.observation_kinds_ever(conn).items() if kind not in kinds)),
        },
        "family_meaning": {name: FAMILY_MEANING[name] for name in sorted(by_family)},
        "what_this_is": {
            "it_is": "a timestamped inventory of what this registry recorded for the "
                     "instrument in the window, measured from stored bars where a "
                     "measurement is possible",
            "it_is_not": "an explanation. Nothing here is marked as a cause, because a "
                         "temporal coincidence is not a mechanism, and no record in this "
                         "registry can establish one.",
            "how_to_read_it": "read the coverage block first. A family with no records is "
                              "'nothing was recorded', which is not 'nothing happened'; the "
                              "remedy for each empty channel is given with it.",
        },
        "limitations": [
            "Every record is a temporal coincidence or a precursor. Whether any of them "
            "moved the price is not established by this tool and is not claimed here.",
            "Records are included by their own occurrence time, so a record published long "
            "after the event it describes will appear in the window it happened in, not the "
            "window it was published in; available_at is reported per row for that reason.",
            "A missing family is missing coverage, not absence of the underlying event.",
            "Stored bars are unadjusted provider values; no split, distribution, roll or "
            "venue-outage repair is applied.",
            "The window cannot exceed ten years and each read is bounded; a truncated result "
            "says so rather than presenting a prefix as the whole window.",
        ],
    }


def _documented_flows(conn, entity_id, window_start, window_end, limit):
    """Documented cross-entity transfers inside the window.

    `list_money_flows` is bounded at a thousand rows, so the window filter is
    applied here rather than trusting a prefix of the table to be the answer.
    """
    inside = []
    for row in registry.list_money_flows(conn, entity_id, limit=1000):
        occurred = str(row.get("occurred_at") or "")
        if occurred and window_start.isoformat() <= occurred < window_end.isoformat():
            inside.append(row)
    inside.sort(key=lambda item: item.get("occurred_at") or "")
    return inside[:limit]


def _tier_sort(tier: str):
    try:
        return (0, int(str(tier).lstrip("p")))
    except (TypeError, ValueError):
        return (1, 0)


def _channel_coverage(span, bars, behaviour, tiers, volatility, anomalies, inside, prior,
                      documented_flows, truncated) -> list[dict]:
    """One honest line per channel: what is there, or what to run to get it."""
    def entry(name, present, count, note=""):
        return {"channel": name,
                "status": "present" if present else "no_coverage",
                "records": count,
                "remedy": "" if present else CHANNEL_REMEDY.get(name, ""),
                "note": note}

    span_note = ""
    if span.get("bars") and not span.get("usable"):
        span_note = (f"stored history has {span['gaps']} gap(s), largest "
                     f"{span['largest_gap_seconds']}s; a window spanning one is not measured")
    removed_before = span.get("history_removed_before")
    if removed_before:
        # "Nothing stored" and "nothing happened" are different claims, and a
        # recorded cut is the difference between them: before this instant the
        # history was removed on purpose, which is not the same as never fetched.
        span_note = (f"{span_note}; " if span_note else "") + (
            f"history before {removed_before} was removed by recorded prune run "
            f"{span['history_removed_run_id']} ({span['history_removed_reason']}), so a window "
            "before that instant cannot be measured from this registry")
    channels = [
        entry("stored_bars", bool(span.get("bars")), span.get("bars", 0), span_note),
        entry("detected_moves", bool(tiers), sum(item["count"] for item in tiers.values())),
        entry("volatility_instances", bool(volatility), len(volatility)),
        entry("stored_observations", bool(inside or prior), len(inside) + len(prior),
              "result truncated at the read limit" if truncated else ""),
        entry("documented_money_flows", bool(documented_flows), len(documented_flows),
              "a documented cross-entity flow requires a supplied JSON; nothing infers one"),
    ]
    channels.append({
        "channel": "headlines", "status": "not_stored", "records": 0,
        "remedy": CHANNEL_REMEDY["headlines"],
        "note": "headlines are not persisted by default; the public indexes that serve them "
                "reach back days, not years, so a historical window cannot be covered by them",
    })
    channels.append({
        "channel": "price_anomalies", "status": "present" if anomalies else "no_coverage",
        "records": len(anomalies), "remedy": CHANNEL_REMEDY["price_anomalies"],
        "note": "",
    })
    return channels


def capital(conn, entity_id: int, start: str, end: str, *, interval_seconds: int = 86400,
            limit: int = DEFAULT_LIMIT, include_market_wide: bool = True) -> dict:
    """Capital and positioning records for one instrument over a window.

    Named for what it can show rather than what a user hopes for. Nothing in
    this registry is a settled transfer into or out of an exchange for a crypto
    asset: the public sources report supply aggregates, market capitalisation
    change, leverage state and reported obligations. Each row is therefore
    labelled with what its number actually is, so a supply total is never read as
    a net inflow and an open-interest change is never read as money moving.
    """
    window_start, window_end = validate(entity_id, start, end, 0, limit, 3, interval_seconds)
    entity = registry.get_entity(conn, entity_id)
    if entity is None:
        raise ValueError(f"entity {entity_id} not found")
    key = entity["key"]
    result = registry.observations_in_window(
        conn, start=window_start.isoformat(), end=window_end.isoformat(),
        instrument_key=key, include_market_wide=include_market_wide, limit=limit)
    documented = _documented_flows(conn, entity_id, window_start, window_end, limit)

    series: dict[str, list[dict]] = {}
    labels: dict[str, dict] = {}
    for row in result["rows"]:
        kind = row["kind"]
        amount, unit, currency = _measure(row)
        if amount is None:
            continue
        series.setdefault(kind, []).append({
            "occurred_at": row["occurred_at"], "available_at": row.get("available_at") or "",
            "value": amount, "unit": unit, "currency": currency,
            "source": row["source"], "source_url": row.get("source_url") or "",
            "basis": row.get("basis") or "", "description": (row.get("description") or "")[:200],
            "scope": "instrument" if row.get("instrument_key") else "market_wide",
        })
        if kind not in labels:
            labels[kind] = _capital_label(kind, row)
    for kind in series:
        series[kind].sort(key=lambda item: item["occurred_at"])

    changes = {}
    for kind, points in series.items():
        if len(points) < 2:
            changes[kind] = {"points": len(points), "first": None, "last": None,
                             "absolute_change": None, "percent_change": None,
                             "note": "a single reading has no change"}
            continue
        with localcontext(Context(prec=60, rounding=ROUND_HALF_EVEN)):
            first = Decimal(points[0]["value"])
            last = Decimal(points[-1]["value"])
            delta = last - first
            percent = None
            is_delta = kind in DELTA_KINDS
            if points[0]["unit"] not in BOUNDED_UNITS and not is_delta:
                percent = _round(delta / abs(first) * 100) if first != 0 else None
            changes[kind] = {
                "points": len(points),
                "first": str(first), "last": str(last),
                "first_at": points[0]["occurred_at"], "last_at": points[-1]["occurred_at"],
                "absolute_change": str(delta.quantize(Decimal("0.000001"))),
                "percent_change": percent,
                "note": labels[kind]["is_net_flow"],
            }
            if is_delta:
                # A per-interval delta has no level to grow from. The meaningful
                # window statistic is the net, and the percent change of two
                # arbitrary intervals is a ratio of near-zero numbers.
                net = sum((Decimal(point["value"]) for point in points
                           if point["value"] not in ("", None)), Decimal(0))
                changes[kind]["net_over_window"] = str(
                    net.quantize(Decimal("0.000001")))
                changes[kind]["percent_change_note"] = (
                    "this is a change over each interval, not a level, so no percent "
                    "change is reported; net_over_window is the sum across the window, "
                    "which is the quantity of interest")
            elif percent is None:
                # A ratio or an index has no meaningful percent change: 74 to 75
                # on a 0-100 scale is not "1.4% more of anything", and printing it
                # that way invites exactly the reading the label forbids.
                changes[kind]["percent_change_note"] = (
                    f"no percent change is reported: {points[0]['unit']} is a bounded unit, "
                    "not an additive quantity, so the absolute change is shown instead")
    return {
        "method": "capital_positioning_view_v1",
        "instrument": {"entity_id": entity_id, "entity_key": key, "name": entity["name"]},
        "window": {"start": window_start.isoformat(), "end": window_end.isoformat()},
        "measured_series": dict(sorted(series.items())),
        "what_each_number_is": labels,
        "change_over_window": changes,
        "documented_money_flows": {
            "count": len(documented),
            "flows": [{"type": row.get("flow_type"), "amount": row.get("amount"),
                       "currency": row.get("currency"), "occurred_at": row.get("occurred_at"),
                       "source_url": row.get("source_url") or "",
                       "counterparty_entity_id": row.get("dst_id") or row.get("src_id")}
                      for row in documented],
            "note": "these are the only records in this registry that are documented "
                    "cross-entity transfers; they exist because they were supplied, never "
                    "because a balance, an ownership edge or a filing implied one",
        },
        "coverage": {"rows_in_scope": result["total"], "returned": len(result["rows"]),
                     "truncated": result["truncated"]},
        "not_available": [
            "net inflow to or from exchanges for a crypto asset: no public source in this "
            "build reports a settled net figure for an exchange",
            "fund flows into or out of a specific listed equity: the SEC extractors record "
            "insider transactions, manager holdings and filings, not investor-level flows",
            "order-level attribution: a print is not attributable to a participant without a "
            "venue account identifier, and none is recorded here",
        ],
        "limitations": [
            "A change in a supply or capitalisation series is not a net flow. It rises with "
            "price as well as with demand, and cannot separate buying from selling, new "
            "capital from rotation, or spot from derivative notional.",
            "An open-interest, funding or long/short change is a change in positions, not a "
            "transfer of funds, and identifies no participant.",
            "Market-wide series describe the whole market, not this instrument, and are "
            "labelled market_wide so they are not mistaken for instrument-level evidence.",
            "A composite index is provider-constructed; its composition is not audited here.",
        ],
    }


# What a number of each kind actually is. The wording is the safeguard: a reader
# who sees "aggregate supply" cannot mistake it for a net inflow.
_CAPITAL_LABELS = {
    "stablecoin_supply": ("aggregate stablecoin supply across the market",
                          "a supply total, not a net flow into this asset"),
    "market_activity": ("change in this asset's market capitalisation (a supply total)",
                        "rises with price as well as with demand, and cannot separate "
                        "buying from selling, new capital from rotation, or spot from "
                        "derivative notional"),
    "fund_flow": ("a documented or reported transfer of funds", "the strongest record here"),
    "exchange_transfer": ("a recorded transfer to or from an exchange",
                          "a reported transfer, not a settled net figure"),
    "onchain_transfer": ("a recorded on-chain transfer", "one transfer, not an aggregate"),
    "etf_flow": ("a reported fund creation or redemption", "a reported figure"),
    "stablecoin_mint_burn": ("a reported mint or burn", "a supply change, not a flow to an asset"),
    "liquidation": ("a reported liquidation", "a forced close, not a discretionary flow"),
    "block_trade": ("a reported block trade", "a print, not a settled net position"),
    "trade_flow": ("a reported trade flow", "depends entirely on the reporting source"),
    "energy_flow": ("a reported physical energy flow", "a quantity, not a financial transfer"),
    "margin_debt": ("reported margin debt", "a stock of credit, not a flow"),
    "short_interest": ("reported short interest", "a position snapshot, not a trade"),
    "open_interest": ("derivative open interest", "positioning state, not money transferred"),
    "funding_rate": ("the perpetual funding rate", "a price of leverage, not a transfer"),
    "order_flow": ("measured taker-side order flow", "aggressor side, not a settled transfer"),
    "cot_positioning": ("reported net non-commercial positioning",
                        "a weekly position snapshot, not a flow"),
    "cot_open_interest": ("reported total futures open interest", "a position snapshot"),
    "long_short_account_ratio": ("the share of accounts long versus short",
                                 "an account count, not size"),
    "top_long_short_position_ratio": ("long/short share among large positions",
                                      "a position ratio, not a transfer"),
    "options_positioning": ("reported options positioning", "a position snapshot"),
    "liquidation_level": ("an estimated liquidation level",
                          "an estimate, not a recorded close"),
    "order_book_imbalance": ("order book depth imbalance", "a momentary snapshot"),
    "social_sentiment": ("a provider sentiment aggregate", "not a flow and not a measurement "
                         "of money"),
    "market_context": ("a provider market aggregate", "meaning is provider-defined"),
    "capitulation_indicator": ("a capitulation indicator", "an indicator, not a record of "
                               "forced selling"),
    "macro_release": ("an official statistical release", "a macro aggregate"),
    "political_event": ("a published policy document or statement", "publication is a fact"),
    "regulatory_action": ("a published rule or enforcement action", "publication is a fact"),
    "geopolitical_event": ("a published geopolitical event record", "publication is a fact"),
    "market_shock": ("a recorded disruption to market access", "an incident record"),
    "insider_trade": ("a filed insider transaction", "one filed transaction"),
    "holding": ("a reported position snapshot", "a position, not a trade"),
    "filing_event": ("a filed corporate document", "a filing, not a transaction"),
    "investment_adviser": ("an investment adviser registration", "a registration"),
    "labor_metric": ("an official labour statistic", "a macro aggregate"),
    "calendar_event": ("a scheduled calendar entry", "a schedule, not an occurrence"),
    "flight_event": ("a recorded flight position", "an observation"),
    "energy_metric": ("an official energy statistic", "a quantity"),
}


def _capital_label(kind, row):
    meaning, is_flow = _CAPITAL_LABELS.get(
        kind, (f"a record of kind {kind}",
               "this kind has no documented meaning here, so treat the number as unestablished"))
    if row.get("basis") == "estimated":
        is_flow = f"{is_flow}; the value is provider-estimated rather than measured"
    scope = "instrument" if row.get("instrument_key") else "market_wide"
    return {"what_it_is": meaning, "is_net_flow": is_flow, "scope": scope,
            "family": signals.OBSERVATION_FAMILY.get(kind, "other")}


def capital_summary(report: dict) -> str:
    """One line naming what was found and, more importantly, what was not."""
    series = report.get("measured_series", {})
    changes = report.get("change_over_window", {})
    parts = []
    for kind, info in sorted(changes.items()):
        percent = info.get("percent_change")
        # The net comes first where there is one: for a per-interval delta it is
        # the quantity of interest, and the endpoint difference is not.
        if percent is not None:
            parts.append(f"{kind}={percent}%")
        elif info.get("net_over_window") is not None:
            parts.append(f"{kind}=net {info['net_over_window']} "
                         f"over {info['points']} intervals")
        elif info.get("absolute_change") is not None:
            parts.append(f"{kind}={info['absolute_change']} "
                         f"({info['points']} readings, no percent: not an additive level)")
        else:
            parts.append(f"{kind}={info['points']} reading(s), no change")
    missing = len(report.get("not_available", []))
    return (f"{report['window']['start'][:10]}..{report['window']['end'][:10]} "
            f"series={len(series)} documented_flows="
            f"{report['documented_money_flows']['count']} "
            f"unavailable_in_this_build={missing} | " + (" ".join(parts) or "no measured series")
            + " | a supply or capitalisation change is not a net flow")


def summary_line(report: dict) -> str:
    """One line for a human, so the tool is usable without reading the JSON."""
    price = report.get("price", {})
    coverage = report.get("coverage", {}).get("channels", [])
    present = [item["channel"] for item in coverage if item["status"] == "present"]
    absent = [item["channel"] for item in coverage if item["status"] != "present"]
    change = price.get("change_percent")
    return (f"{report['window']['start'][:10]}..{report['window']['end'][:10]} "
            f"change={change if change is not None else 'not measured'}% "
            f"episodes={len(report.get('episodes', []))} "
            f"records={report['records']['total_in_scope']} "
            f"channels_with_data={len(present)} channels_without={len(absent)} "
            f"| nothing here is marked as a cause")
