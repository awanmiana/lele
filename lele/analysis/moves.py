"""Detect tiered, non-overlapping price moves from stored OHLC history.

A move is a fixed-horizon return over stored bars. Candidates are ranked by
absolute change, then selected greedily so that no two selected windows share a
bar, which keeps one crash from being counted many times. Each selected move is
scored against a trailing baseline of earlier returns only, so no value at or
after the move start influences its own baseline.

Selection is statistical bookkeeping, not a claim about causes: a move is a
labelled price outcome, never an explanation.
"""
import json
from ..core import clock
from datetime import datetime, timedelta, UTC
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext

from ..core import registry

METHOD = "tiered_moves_v1"
DEFAULT_THRESHOLDS = (3, 5, 7, 11)
MAX_THRESHOLDS = 8
MIN_MOVE_HOURS = 1
MAX_MOVE_HOURS = 24 * 365
MIN_BASELINE_BARS = 20
MAX_BASELINE_BARS = 2000
MAX_BARS = 20000
MAX_MOVES = 5000


def _time(value):
    return datetime.fromisoformat(value).astimezone(UTC)


def validate(move_hours, thresholds, baseline_bars, limit, store, cooldown_bars=0):
    if type(move_hours) is not int or not MIN_MOVE_HOURS <= move_hours <= MAX_MOVE_HOURS:
        raise ValueError(f"move hours must be an integer from {MIN_MOVE_HOURS} to {MAX_MOVE_HOURS}")
    if (not isinstance(thresholds, (tuple, list)) or not thresholds
            or len(thresholds) > MAX_THRESHOLDS):
        raise ValueError(f"thresholds must be a nonempty sequence of at most {MAX_THRESHOLDS} "
                         "absolute percentages")
    for value in thresholds:
        if type(value) is not int or not 1 <= value <= 1000:
            raise ValueError("each threshold must be an integer from 1 to 1000")
    if len(set(thresholds)) != len(thresholds):
        raise ValueError("thresholds must be distinct")
    if list(thresholds) != sorted(thresholds):
        raise ValueError("thresholds must be given in ascending order")
    if type(baseline_bars) is not int or not MIN_BASELINE_BARS <= baseline_bars <= MAX_BASELINE_BARS:
        raise ValueError(f"baseline bars must be an integer from {MIN_BASELINE_BARS} to "
                         f"{MAX_BASELINE_BARS}")
    if type(limit) is not int or not 1 <= limit <= MAX_MOVES:
        raise ValueError(f"limit must be an integer from 1 to {MAX_MOVES}")
    if type(store) is not bool:
        raise ValueError("store must be a boolean")
    if type(cooldown_bars) is not int or not 0 <= cooldown_bars <= 10000:
        raise ValueError("cooldown bars must be an integer from 0 to 10000")


def _series(conn, instrument_key, interval_seconds):
    rows = registry.list_price_bars(conn, instrument_key, interval_seconds, limit=MAX_BARS)
    times, closes, opens, highs, lows = [], [], [], [], []
    for row in rows:
        try:
            times.append(_time(row["open_time"]))
            opens.append(Decimal(row["open"]))
            highs.append(Decimal(row["high"]))
            lows.append(Decimal(row["low"]))
            closes.append(Decimal(row["close"]))
        except (ValueError, TypeError, ArithmeticError):
            continue
    return times, opens, highs, lows, closes


def _cadence(times, interval_seconds):
    """The bar spacing the series actually has, in seconds.

    Taken as the most common gap between consecutive bars rather than from the
    `interval_seconds` label, because a fetch that stopped half way and was
    resumed can leave two blocks of different spacing under one label.
    """
    counts: dict[int, int] = {}
    for index in range(1, len(times)):
        gap = int((times[index] - times[index - 1]).total_seconds())
        if gap > 0:
            counts[gap] = counts.get(gap, 0) + 1
    if not counts:
        return int(interval_seconds)
    return max(sorted(counts), key=lambda gap: counts[gap])


def _gaps(times, cadence, tolerance=1.5):
    """Index positions where the series skips a bar.

    A window that spans one of these is not the move it claims to be: an
    eight-day "24 hour" return is a different measurement with a different
    baseline, and reporting it as a 24-hour move silently corrupts the sample.
    """
    limit = cadence * tolerance
    return {index for index in range(1, len(times))
            if (times[index] - times[index - 1]).total_seconds() > limit}


def _step(times, move_hours, cadence):
    """How many bars make up the requested horizon, or None if there are not enough.

    Derived from the measured cadence rather than from the first bars of the
    series, so a series whose opening block is irregular does not fix the window
    length for the whole run.
    """
    step = max(-(-int(move_hours * 3600) // int(cadence)), 1)
    return step if len(times) > step else None


def _returns(closes, step):
    with localcontext(Context(prec=80, rounding=ROUND_HALF_EVEN)):
        values = []
        for index in range(step, len(closes)):
            start = closes[index - step]
            if start == 0:
                continue
            values.append((closes[index] - start) / start * 100)
        return values


def _baseline(values, position, baseline_bars):
    """Mean and standard deviation of returns strictly before `position`."""
    window = values[max(0, position - baseline_bars):position]
    if len(window) < 5:
        return None
    with localcontext(Context(prec=80, rounding=ROUND_HALF_EVEN)):
        mean = sum(window) / len(window)
        variance = sum((value - mean) ** 2 for value in window) / len(window)
    if variance <= 0:
        return None
    return mean, variance.sqrt(), window


def _candidates(times, opens, highs, lows, closes, step, values, smallest, baseline_bars,
                gaps=frozenset(), target_seconds=None):
    found = []
    for position in range(5, len(values)):
        change = values[position]
        magnitude = abs(change)
        if magnitude < smallest:
            continue
        start = position - step
        if start < 0:
            continue
        if gaps and any(gap in gaps for gap in range(start + 1, position + 1)):
            continue
        if target_seconds is not None:
            span = (times[position] - times[start]).total_seconds()
            if abs(span - target_seconds) > max(target_seconds * 0.05, 1):
                continue
        baseline = _baseline(values, position, baseline_bars)
        if baseline is None:
            mean, deviation, window = Decimal(0), Decimal(0), []
        else:
            mean, deviation, window = baseline
        with localcontext(Context(prec=80, rounding=ROUND_HALF_EVEN)):
            z_score = (change - mean) / deviation if deviation > 0 else Decimal(0)
            percentile = (Decimal(sum(1 for value in window if abs(value) < magnitude))
                          / Decimal(len(window)) * 100) if window else None
            intrabar = Decimal(0)
            if start < len(lows):
                span = highs[position] - lows[position]
                if lows[position] > 0:
                    intrabar = span / lows[position] * 100
        found.append({
            "position": position, "start": start, "direction":
                "up" if change > 0 else "down", "change_percent": change,
            "start_price": opens[start], "end_price": closes[position],
            "terminal_bar_range_percent": intrabar, "baseline_mean_percent": mean,
            "baseline_std_percent": deviation, "z_score": z_score,
            "baseline_percentile": percentile, "baseline_bars": len(window),
            "start_time": times[start], "end_time": times[position],
        })
    return found


def _select(found, limit, cooldown_bars=0):
    """Keep the largest moves whose windows are independent.

    A candidate is rejected when it shares a bar with a retained move, which
    stops one crash from being counted once per bar, and when it sits inside the
    cooldown after a retained move in the same direction, which is the opt-in
    stricter reading of "one episode per trend". Selection is by magnitude, so
    the retained set is the largest available moves, not the first ones.
    """
    ordered = sorted(found, key=lambda item: (abs(item["change_percent"]), item["end_time"]),
                     reverse=True)
    chosen: list[dict] = []
    used: set[int] = set()
    reached: dict[str, int] = {}
    for candidate in ordered:
        span = range(candidate["start"], candidate["position"] + 1)
        if used.intersection(span):
            continue
        direction = candidate["direction"]
        if cooldown_bars and candidate["start"] <= reached.get(direction, -1) + cooldown_bars:
            continue
        used.update(span)
        reached[direction] = candidate["position"]
        chosen.append(candidate)
        if len(chosen) >= limit:
            break
    chosen.sort(key=lambda item: item["end_time"])
    return chosen


def detect(conn, instrument_key, interval_seconds, move_hours=24,
           thresholds=DEFAULT_THRESHOLDS, baseline_bars=90, limit=500, store=True,
           cooldown_bars=0):
    """Detect moves and record each one against every threshold it clears.

    A ladder of absolute percentage thresholds is cumulative by design: a 12%
    move is also a 3%, a 5%, a 7% and an 11% instance. That gives a large sample
    at the low thresholds, where a big-move threshold would find only a handful,
    and lets the same detector be asked whether the conditions preceding a 3%
    move differ from those preceding an 11% move.
    """
    thresholds = tuple(thresholds)
    validate(move_hours, thresholds, baseline_bars, limit, store, cooldown_bars)
    if type(interval_seconds) is not int or interval_seconds < 60:
        raise ValueError("interval seconds must be an integer of at least 60")
    times, opens, highs, lows, closes = _series(conn, instrument_key, interval_seconds)
    result = {
        "method": METHOD, "instrument_key": instrument_key,
        "interval_seconds": interval_seconds,
        "parameters": {"move_hours": move_hours, "thresholds_percent": list(thresholds),
                       "tier_rule": "a move is recorded at every threshold it clears, so each "
                                    "tier is a cumulative sample of all moves at or above it",
                       "baseline_bars": baseline_bars,
                       "selection": "largest first; retained windows may not share a bar, "
                                    "and cooldown_bars optionally separates same-direction moves",
                       "cooldown_bars": cooldown_bars,
                       "baseline": "mean and standard deviation of the baseline_bars returns "
                                   "strictly before each move window"},
        "coverage": {"bars": len(closes), "start": times[0].isoformat() if times else None,
                     "end": times[-1].isoformat() if times else None,
                     "candidates": 0, "selected": 0, "completeness": "unknown",
                     "completeness_note": "the provider's history start is not discoverable "
                                           "from a bounded fetch, so this is not a complete "
                                           "record and no frequency claim may be made from it"},
        "summary": {}, "moves": [], "stored": 0,
        "limitations": [
            "A move is a fixed-horizon return between stored bar closes; it is not an intraday "
            "extreme, a drawdown peak or a rally onset.",
            "Windows are forced to be non-overlapping by taking the largest move first, so the "
            "retained set is a sample of the largest moves, not every move that occurred.",
            "z_score and the percentile compare a move with the preceding baseline_bars returns "
            "only, so a regime that changes permanently is compared with its own recent past.",
            "A zero-variance baseline, a missing bar or a level shift produces z_score 0 and "
            "baseline_percentile unknown rather than a false significance claim.",
            "Stored bars are unadjusted provider values; no independent market-truth check, "
            "corporate-action repair or venue-outage correction was applied.",
            "Retained windows never share a bar, so a tier count is far below the candidate "
            "count, but a multi-day trend can still contribute several consecutive moves and a "
            "cooldown is available for the stricter reading of one episode per trend.",
            "A detected move is an outcome label. Nothing here identifies a cause, and no "
            "accuracy, calibration or trading claim is supported.",
        ],
    }
    if len(closes) < MIN_BASELINE_BARS + 2:
        result["summary"] = {"status": "insufficient_bars", "required_bars":
                             MIN_BASELINE_BARS + 2, "have": len(closes)}
        return result
    cadence = _cadence(times, interval_seconds)
    gaps = _gaps(times, cadence)
    step = _step(times, move_hours, cadence)
    if step is None:
        result["summary"] = {"status": "insufficient_span", "move_hours": move_hours,
                             "available_hours": (times[-1] - times[0]).total_seconds() / 3600}
        return result
    values = _returns(closes, step)
    found = _candidates(times, opens, highs, lows, closes, step, values, thresholds[0],
                        baseline_bars, gaps, move_hours * 3600)
    result["coverage"].update({
        "cadence_seconds": cadence,
        "gaps": len(gaps),
        "gap_index": sorted(gaps)[:20],
        "largest_gap_seconds": (
            max(int((times[index] - times[index - 1]).total_seconds()) for index in gaps)
            if gaps else 0),
        "contiguous_fraction": (
            round(1 - len(gaps) / max(1, len(times) - 1), 6)),
        "coverage_note": "candidates whose window spans a missing bar, or whose real elapsed "
                         "time is not the requested horizon, are excluded rather than "
                         "reported as moves of the wrong length",
    })
    chosen = _select(found, limit, cooldown_bars)
    result["coverage"]["candidates"] = len(found)
    result["coverage"]["selected"] = len(chosen)
    detected_at = clock.now().replace(microsecond=0).isoformat()
    moves = []
    rows = []
    for item in chosen:
        magnitude = abs(item["change_percent"])
        cleared = [value for value in thresholds if magnitude >= value]
        move = {
            "tiers": [registry.move_tier(value) for value in cleared],
            "tier": registry.move_tier(cleared[-1]) if cleared else None,
            "direction": item["direction"],
            "start_time": item["start_time"].isoformat(), "end_time": item["end_time"].isoformat(),
            "start_price": str(item["start_price"]), "end_price": str(item["end_price"]),
            "change_percent": _round(item["change_percent"]),
            "terminal_bar_range_percent": _round(item["terminal_bar_range_percent"]),
            "baseline_mean_percent": _round(item["baseline_mean_percent"]),
            "baseline_std_percent": _round(item["baseline_std_percent"]),
            "z_score": _round(item["z_score"]),
            "baseline_percentile": (_round(item["baseline_percentile"])
                                    if item["baseline_percentile"] is not None else None),
            "baseline_bars": item["baseline_bars"],
            "duration_hours": _round(Decimal(
                int((item["end_time"] - item["start_time"]).total_seconds())) / Decimal(3600)),
        }
        moves.append(move)
        for value in cleared:
            rows.append((registry.move_tier(value), value, move))
            if store:
                registry.add_move_event(
                    conn, instrument_key=instrument_key, interval_seconds=interval_seconds,
                    move_hours=move_hours, tier=registry.move_tier(value),
                    threshold_percent=str(value), direction=item["direction"],
                    start_time=item["start_time"].isoformat(),
                    end_time=item["end_time"].isoformat(), start_price=str(item["start_price"]),
                    end_price=str(item["end_price"]), change_percent=move["change_percent"],
                    terminal_bar_range_percent=move["terminal_bar_range_percent"],
                    baseline_mean_percent=move["baseline_mean_percent"],
                    baseline_std_percent=move["baseline_std_percent"], z_score=move["z_score"],
                    baseline_percentile=move["baseline_percentile"],
                    baseline_bars=item["baseline_bars"], detected_at=detected_at,
                    available_at=detected_at,
                    evidence=_evidence(instrument_key, interval_seconds, move_hours,
                                       list(thresholds), baseline_bars))
    if store:
        result["stored"] = len(rows)
    result["moves"] = moves
    counts = {}
    for value in thresholds:
        label = registry.move_tier(value)
        at_tier = [move for tier_label, _, move in rows if tier_label == label]
        up = sum(1 for move in at_tier if move["direction"] == "up")
        down = sum(1 for move in at_tier if move["direction"] == "down")
        counts[label] = {"up": up, "down": down, "total": up + down}
    result["summary"] = {
        "status": "ok", "moves": len(moves), "move_rows": len(rows),
        "by_tier_direction": counts,
        "thresholds_percent": list(thresholds),
        "median_abs_change_percent": _median([abs(Decimal(m["change_percent"])) for m in moves]),
        "largest_abs_change_percent": (_round(max(abs(Decimal(m["change_percent"]))
                                                   for m in moves)) if moves else None),
        "coverage_note": "selected moves are the largest non-overlapping windows in the stored "
                         "series and each is counted once per threshold it clears, so a tier "
                         "count is a cumulative sample rather than a market frequency",
    }
    return result


def _round(value, digits="0.0001"):
    with localcontext(Context(prec=60, rounding=ROUND_HALF_EVEN)):
        return str(value.quantize(Decimal(digits)))


def _median(values):
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    with localcontext(Context(prec=60, rounding=ROUND_HALF_EVEN)):
        if len(ordered) % 2:
            return _round(ordered[middle])
        return _round((ordered[middle - 1] + ordered[middle]) / 2)


def _evidence(instrument_key, interval_seconds, move_hours, thresholds, baseline_bars):
    return json.dumps({"method": METHOD, "instrument_key": instrument_key,
                       "interval_seconds": interval_seconds, "move_hours": move_hours,
                       "thresholds_percent": thresholds, "baseline_bars": baseline_bars},
                      sort_keys=True, separators=(",", ":"))


def current(conn, instrument_key, interval_seconds, move_hours=24):
    """The in-progress move implied by the newest stored bars, using the same rule as detect."""
    if type(move_hours) is not int or not MIN_MOVE_HOURS <= move_hours <= MAX_MOVE_HOURS:
        raise ValueError(f"move hours must be an integer from {MIN_MOVE_HOURS} to {MAX_MOVE_HOURS}")
    times, opens, highs, lows, closes = _series(conn, instrument_key, interval_seconds)
    if len(closes) < 3:
        return {"status": "insufficient_bars", "bars": len(closes)}
    cadence = _cadence(times, interval_seconds)
    gaps = _gaps(times, cadence)
    step = _step(times, move_hours, cadence)
    if step is None:
        return {"status": "insufficient_span", "move_hours": move_hours,
                "available_hours": (times[-1] - times[0]).total_seconds() / 3600}
    start = len(closes) - 1 - step
    if any(gap in gaps for gap in range(start + 1, len(closes))):
        return {"status": "window_spans_gap", "move_hours": move_hours,
                "start_time": times[start].isoformat(), "end_time": times[-1].isoformat(),
                "note": "a bar is missing inside the window, so the return covers more than "
                        "the requested horizon and is not reported as a move of that length"}
    elapsed = (times[-1] - times[start]).total_seconds()
    if abs(elapsed - move_hours * 3600) > max(move_hours * 180.0, 1):
        return {"status": "window_length_mismatch", "move_hours": move_hours,
                "elapsed_hours": round(elapsed / 3600, 4),
                "note": "the stored window does not cover the requested horizon"}
    with localcontext(Context(prec=80, rounding=ROUND_HALF_EVEN)):
        change = (closes[-1] - closes[start]) / closes[start] * 100
        span = (highs[-1] - lows[-1]) / lows[-1] * 100 if lows[-1] > 0 else Decimal(0)
    return {
        "status": "ok", "as_of": times[-1].isoformat(),
        "start_time": times[start].isoformat(), "end_time": times[-1].isoformat(),
        "elapsed_hours": _round(Decimal(int(elapsed)) / Decimal(3600)),
        "start_price": str(closes[start]), "end_price": str(closes[-1]),
        "change_percent": _round(change), "direction": "up" if change > 0 else "down",
        "terminal_bar_range_percent": _round(span), "move_hours": move_hours,
        "bars": len(closes),
    }


def windows(moves, pre_hours, provider_window_hours, now=None):
    """Pre-move news windows for each move, clipped to what the provider can serve.

    Returns (requested, servable) where each entry is the ISO pair the news
    fetcher must be asked for. Windows older than the provider's reach are
    reported as unavailable rather than silently treated as quiet. `now` is an
    explicit input so a historical run can be reproduced exactly.
    """
    if type(pre_hours) is not int or not 1 <= pre_hours <= 24 * 90:
        raise ValueError("pre hours must be an integer from 1 to 2160")
    if type(provider_window_hours) is not int or not 1 <= provider_window_hours <= 24 * 90:
        raise ValueError("provider window hours must be an integer from 1 to 2160")
    floor = (now if now is not None else clock.now()) - timedelta(hours=provider_window_hours)
    requested, servable = [], []
    for move in moves:
        start = _time(move["start_time"])
        window = (start - timedelta(hours=pre_hours), start)
        requested.append({"move_id": move.get("id"), "start_inclusive": window[0].isoformat(),
                          "end_exclusive": window[1].isoformat()})
        if window[0] < floor:
            continue
        servable.append({"move_id": move.get("id"), "start_inclusive": window[0].isoformat(),
                         "end_exclusive": window[1].isoformat()})
    return requested, servable
