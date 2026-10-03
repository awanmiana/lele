"""Store context series as stationary quantities, so a drifting level stops
being read as a difference between groups.

This module exists because `causes._time_trend` flagged aggregate stablecoin
supply at 2.33 standard deviations of drift between the compared windows, and a
level that drifts with the calendar cannot be compared across groups whose
windows sit at different dates however large the sample is. The report then
withholds the finding for a reason that is a property of the *measurement*, not
of the market, and the whole comparison is unreadable in both directions.

A stationary quantity is one whose distribution does not move with time, so the
calendar stops being an alternative explanation. Two are derived here.

`change`
    The value minus the previous stored value of the same series. Stationary by
    construction, needs no tuned parameter, and costs exactly one point: the
    first value has nothing to difference against.

`zscore`
    The value against its own trailing baseline, reusing `volatility.z_score`
    and the same minimum baseline the threshold rule already requires of a
    published z-score. Stationary, and scale free, so two instruments in
    different units are comparable. It costs `minimum_baseline` points at the
    start of the series, which on a 364-day series is a sixth of it.

Two further transforms named in the plan are deliberately not offered, and
`NOT_OFFERED` states why rather than leaving the reader to assume they were
forgotten. `rate` is a `change` divided by a chosen period, and the period is a
free parameter that cannot change any conclusion a `change` does not already
give; `ratio` divides by a prior value that a change series crosses zero, so its
denominator is exactly undefined where the signal is largest.

Three rules govern every derivation, and each is the same rule the move detector
already follows.

1. *Cadence is measured, not assumed.* A difference is only a difference over
   the interval it spans, so the interval is read off the stored timestamps.
2. *A difference across a hole is refused, not computed.* Two points eight days
   apart have a difference, but not a daily one, and storing it as the latter is
   the same defect as a move measured across a missing bar.
3. *What cannot be computed is counted by name, never dropped silently.* Every
   refusal below increments a named counter in the report.

A derived value is stored as data with the parameters that produced it -- the
measure, the interval it spans, the baseline length, the baseline mean and
spread, and the degrees of freedom -- because the measure name alone does not
reproduce the number. Given the same stored observations the derivation is
byte-reproducible; `lele context derive` twice over an unchanged registry
rewrites the same rows and changes nothing.

Everything here is Decimal and nothing calls the system clock.
"""
import json
from datetime import datetime, timedelta, UTC
from decimal import (Context, Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext)

from ..core import clock, registry
from . import volatility

METHOD = "context_measure_v1"
PRECISION = 80
ROUNDING = ROUND_HALF_EVEN

MEASURES = ("change", "zscore")
MAX_SERIES_POINTS = 20000
MAX_MEASURE_ROWS = 20000

#: The sample standard deviation's relative standard error is about
#: 1/sqrt(2n): 9% at n=60. Below that a published z-score carries enough error
#: in its own denominator to move a borderline value across a threshold, which
#: is the reason `volatility.MIN_BASELINE_Z` exists and it is not relaxed here.
MINIMUM_BASELINE = volatility.MIN_BASELINE_Z

#: Degrees of freedom for a z-score's baseline spread: the sample standard
#: deviation, the same convention `volatility.z_score` uses. Stored on every row.
DDOF = 1

#: A step longer than this multiple of the measured cadence is a hole. Same
#: tolerance `volatility._gap_index` uses, for the same reason: a series that
#: skips a few periods is not a series with an irregular cadence.
GAP_TOLERANCE = 1.5

#: The scale a measured quantity is written at, and the character budget for a
#: parameter written at full precision. The registry holds decimal text with a
#: 64-character cap, so a parameter that needs more than this is refused by name.
#: 64 is the registry's own limit, not a rounder number.
PLACES = Decimal("0.00000001")
MAX_PARAMETER_CHARS = 64

#: Significant digits kept for a stored parameter. Stored with every row, for
#: the reason `ddof` is: the number alone does not say how it was written down,
#: and a reader cannot reproduce a score from parameters of unknown precision.
PARAMETER_DIGITS = 20

#: Stored kinds that are a level published on a fixed cadence, so a difference
#: between consecutive points means something. This is a list and not a rule
#: because the property is not inferable from the data: six irregularly dated
#: insider trades also admit a measured cadence, and differencing them would
#: produce a number in shares that reads as a change in holdings.
LEVEL_SERIES_KINDS = {
    "stablecoin_supply":
        "a daily aggregate level in USD that rises by an order of magnitude over "
        "its stored span, which is what made its raw difference unreadable",
    "social_sentiment":
        "a daily published index level on a fixed 0-100 scale, where a difference "
        "is an index point rather than a quantity",
    "market_activity":
        "a daily instrument-bound market capitalisation change, so its difference "
        "is a second difference in the same unit",
    "macro_release":
        "a daily published Treasury operating cash balance level, recorded with no "
        "instrument key",
}

#: Transforms named in the development plan that this module does not offer, with
#: the reason. Quoted by the capability summary and pinned by a test, so a
#: deliberate omission cannot read as an oversight.
NOT_OFFERED = {
    "rate":
        "a change divided by a chosen period; the period is a free parameter that "
        "cannot change a conclusion the change does not already give, and choosing "
        "one here would add a number to quote without adding evidence",
    "ratio":
        "the value divided by a prior value, which is undefined when the prior value "
        "is zero or negative -- exactly where a change series crosses zero, which is "
        "where the signal is largest",
}

#: Refusal reasons, counted per series and reported with the rows they cost.
REFUSALS = (
    "no_stored_level",
    "too_short_to_measure_cadence",
    "repeated_timestamp",
    "not_increasing",
    "unit_changed",
    "no_prior_point",
    "baseline_short",
    "flat_baseline",
    "interrupted",
    "unparsable_value",
    "value_not_representable",
)

UNITS = {
    "change": "{unit}_change_per_{cadence}s",
    "zscore": "sigma",
}


def _ctx() -> Context:
    return Context(prec=PRECISION, rounding=ROUNDING)


class _NotRepresentable(Exception):
    """A derived value the registry cannot hold as a finite decimal string."""


def _storable(value) -> bool:
    """Whether a measured quantity survives being written down at this project's scale.

    A registry that holds decimal text has to choose a scale, and a scale that
    rounds an ordinary figure to eight places is what every other estimate in this
    project does. It must not round one to *zero*, because a stored zero next to
    the level it came from states that nothing happened. Anything the scale would
    erase is refused by name instead.
    """
    with localcontext(_ctx()):
        quantized = value.quantize(PLACES)
    if not quantized.is_finite() or not -18 <= quantized.adjusted() <= 18:
        return False
    return not (quantized == 0 and value != 0)


def _text(value):
    """A measured quantity as plain decimal text.

    `str(Decimal('0E-8'))` is `'0E-8'`, which is not a finite decimal string to
    the registry that has to hold it, so exponent notation is rendered out.
    """
    with localcontext(_ctx()):
        quantized = value.quantize(PLACES)
    if not quantized.is_finite() or not -18 <= quantized.adjusted() <= 18:
        raise _NotRepresentable(str(quantized))
    if quantized == 0 and value != 0:
        raise _NotRepresentable(str(value))
    return format(quantized, "f")


def _exact(value):
    """A parameter as plain decimal text, at `PARAMETER_DIGITS` significant digits.

    The baseline mean and spread are what the score was divided by. At the
    eight-decimal scale the score itself is stored at, twenty significant digits
    reproduce it many times over, and a full eighty-digit rendering of a spread
    taken deep inside the default context's precision is both longer than the
    registry's decimal text allows and no more reproducible. A parameter that
    rounds away entirely at that budget is refused rather than stored as zero,
    because a spread of zero with a score beside it states that nothing varied.
    """
    with localcontext(_ctx()) as ctx:
        ctx.prec = PARAMETER_DIGITS
        rounded = +value
    rendered = format(rounded, "f") if rounded.is_finite() else str(rounded)
    if (not rounded.is_finite() or len(rendered) > MAX_PARAMETER_CHARS
            or not -18 <= rounded.adjusted() <= 18
            or (rounded == 0 and value != 0)):
        raise _NotRepresentable(rendered[:MAX_PARAMETER_CHARS])
    return rendered


def validate(measure: str, kind: str, baseline: int) -> None:
    if measure not in MEASURES:
        raise ValueError(f"measure must be one of {', '.join(MEASURES)}")
    if not isinstance(kind, str) or kind not in LEVEL_SERIES_KINDS:
        raise ValueError(f"kind must be one of {', '.join(sorted(LEVEL_SERIES_KINDS))}")
    if type(baseline) is not int or baseline < MINIMUM_BASELINE:
        raise ValueError(f"baseline must be an integer of at least {MINIMUM_BASELINE}")


def measure_unit(measure: str, unit: str, cadence_seconds: int) -> str:
    """The unit of the derived quantity, naming the interval when it matters.

    A change carries the interval it spans in its unit, because a difference over
    an hour and a difference over a day are different quantities and the reader
    cannot recover the interval from the number.
    """
    if measure == "zscore":
        return UNITS["zscore"]
    if cadence_seconds <= 0:
        return ""
    return UNITS["change"].format(unit=unit or "value", cadence=cadence_seconds)


def series_label(kind: str, instrument_key: str, measure: str) -> str:
    """A name for one measure series, distinct from the stored kind it came from."""
    base = f"{kind}@{instrument_key}" if instrument_key else kind
    return f"{base}.{measure}"


def _moment(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def stored_series(conn, kind: str, instrument_key: str = "") -> dict:
    """The measured points of one stored level series, with what went wrong.

    Read straight from the registry rather than through `signals.stored`, because
    a derivation needs the whole series in order, and because a series is
    identified by its kind and scope rather than by the window it happens to
    overlap.
    """
    sql = ("SELECT observed_at, amount, unit, available_at, source, source_url, evidence,"
           " occurred_at FROM observations WHERE kind=?")
    params: list = [kind]
    if instrument_key:
        sql += " AND instrument_key=?"
        params.append(instrument_key)
    else:
        sql += " AND instrument_key=''"
    sql += " ORDER BY observed_at, id LIMIT ?"
    params.append(MAX_SERIES_POINTS)
    rows = conn.execute(sql, params).fetchall()
    refused = dict.fromkeys(REFUSALS, 0)
    points = []
    for row in rows:
        value = (row["amount"] or "").strip()
        if not value:
            refused["no_stored_level"] += 1
            continue
        try:
            number = Decimal(value)
        except InvalidOperation:
            refused["unparsable_value"] += 1
            continue
        if not number.is_finite():
            refused["unparsable_value"] += 1
            continue
        points.append({"observed_at": row["observed_at"], "value": number,
                       "unit": row["unit"] or "", "available_at": row["available_at"],
                       "source": row["source"] or "", "source_url": row["source_url"] or "",
                       "evidence": row["evidence"] or "", "occurred_at": row["occurred_at"]})
    if not points:
        return {"kind": kind, "instrument_key": instrument_key, "points": [],
                "rows_read": len(rows), "cadence_seconds": 0, "holes": [],
                "refused": refused, "first": None, "last": None,
                "note": "no parsable measured level is stored for this series"}
    times = [point["observed_at"] for point in points]
    if len(set(times)) != len(times):
        refused["repeated_timestamp"] += len(times) - len(set(times))
        return {"kind": kind, "instrument_key": instrument_key, "points": [],
                "rows_read": len(rows), "cadence_seconds": 0, "holes": [], "refused": refused,
                "first": None, "last": None,
                "note": "two stored points share a timestamp, so consecutive pairs are not "
                        "determined and nothing is differenced"}
    instants = [_moment(value) for value in times]
    ordered = all(instants[index] > instants[index - 1] for index in range(1, len(instants)))
    if not ordered:
        refused["not_increasing"] += 1
        return {"kind": kind, "instrument_key": instrument_key, "points": [],
                "rows_read": len(rows), "cadence_seconds": 0, "holes": [], "refused": refused,
                "first": None, "last": None,
                "note": "stored timestamps do not strictly increase once normalised to UTC, "
                        "so a difference would run backwards. ISO text sorts by its own "
                        "characters, not by instant, so a row written with an offset can sort "
                        "after one it precedes"}
    cadence, holes = _cadence_and_holes(points)
    if cadence <= 0:
        refused["too_short_to_measure_cadence"] += len(points)
        return {"kind": kind, "instrument_key": instrument_key, "points": [],
                "rows_read": len(rows), "cadence_seconds": 0, "holes": [], "refused": refused,
                "first": None, "last": None,
                "note": "fewer than two stored points, so the interval a difference would span "
                        "is not measured and cannot be named"}
    return {"kind": kind, "instrument_key": instrument_key, "points": points,
            "rows_read": len(rows), "cadence_seconds": cadence, "holes": holes,
            "refused": refused, "first": points[0]["observed_at"],
            "last": points[-1]["observed_at"], "note": None}


def _cadence_and_holes(points: list[dict]) -> tuple[int, set[int]]:
    """The measured interval between consecutive points, and where it is broken.

    Measured as the most common step rather than read from a label, which is what
    lets a hole be recognised at all.
    """
    if len(points) < 2:
        return 0, set()
    deltas: dict[int, int] = {}
    for index in range(1, len(points)):
        step = int((_moment(points[index]["observed_at"])
                    - _moment(points[index - 1]["observed_at"])).total_seconds())
        deltas[step] = deltas.get(step, 0) + 1
    cadence = max(deltas.items(), key=lambda item: (item[1], -item[0]))[0]
    if cadence <= 0:
        return 0, set()
    holes = set()
    for index in range(1, len(points)):
        step = int((_moment(points[index]["observed_at"])
                    - _moment(points[index - 1]["observed_at"])).total_seconds())
        if step > cadence * GAP_TOLERANCE:
            holes.add(index)
    return cadence, holes


def derive(conn, kind: str, *, instrument_key: str = "", measure: str = "change",
           baseline: int = MINIMUM_BASELINE, now=None) -> dict:
    """One derived measure series, ready to store, with every refusal counted.

    Nothing is written. `store` does that separately, so a caller can read what
    a derivation would produce before deciding to keep it, and so a read-only
    report can state what is derivable from stored data.
    """
    validate(measure, kind, baseline)
    moment = now if now is not None else clock.now()
    series = stored_series(conn, kind, instrument_key)
    rows: list[dict] = []
    points = series["points"]
    refused = dict(series["refused"])
    cadence = series["cadence_seconds"]
    if points and cadence > 0:
        refused["no_prior_point"] += 1
        for index in range(1, len(points)):
            current, prior = points[index], points[index - 1]
            if index in series["holes"]:
                refused["interrupted"] += 1
                continue
            if current["unit"] != prior["unit"]:
                refused["unit_changed"] += 1
                continue
            if measure == "change":
                with localcontext(_ctx()):
                    value = current["value"] - prior["value"]
                if not _storable(value):
                    refused["value_not_representable"] += 1
                    continue
                rows.append(_row(kind, instrument_key, measure, current, prior, value,
                                 cadence, baseline=0, mean="", deviation="", ddof=0,
                                 now=moment))
                continue
            window = points[max(0, index - baseline):index]
            if len(window) < baseline:
                refused["baseline_short"] += 1
                continue
            history = [point["value"] for point in window]
            score = volatility.z_score(history, current["value"])
            if score is None:
                refused["flat_baseline"] += 1
                continue
            mean = sum(history, Decimal(0)) / len(history)
            spread = volatility.stdev(history, DDOF)
            try:
                rows.append(_row(kind, instrument_key, measure, current, prior, score, cadence,
                                 baseline=baseline, mean=_exact(mean),
                                 deviation=_exact(spread) if spread is not None else "",
                                 ddof=DDOF, now=moment))
            except _NotRepresentable:
                refused["value_not_representable"] += 1
    elif points:
        refused["too_short_to_measure_cadence"] += len(points)
    return {
        "method": METHOD, "kind": kind, "instrument_key": instrument_key,
        "series": series_label(kind, instrument_key, measure), "measure": measure,
        "baseline": baseline if measure == "zscore" else 0, "ddof": DDOF if measure == "zscore"
        else 0,
        "status": "ok" if rows else "nothing_derivable",
        "rows": rows, "derived": len(rows),
        "points_available": len(points), "rows_read": series["rows_read"],
        "cadence_seconds": cadence, "unit": measure_unit(measure, points[0]["unit"], cadence)
        if points else "",
        "holes": len(series["holes"]), "first": series["first"], "last": series["last"],
        "refused": {name: count for name, count in sorted(refused.items()) if count},
        "reason": series["note"] if not rows else None,
        "limitations": LIMITATIONS,
    }


def _row(kind, instrument_key, measure, current, prior, value, cadence, *, baseline, mean,
         deviation, ddof, now) -> dict:
    with localcontext(_ctx()):
        rendered = _text(value)
    unit = measure_unit(measure, current["unit"], cadence)
    return {
        "source_kind": kind, "instrument_key": instrument_key, "measure": measure,
        "value": rendered, "unit": unit, "prior_observed_at": prior["observed_at"],
        "cadence_seconds": cadence, "baseline_observations": baseline,
        "baseline_mean": mean, "baseline_deviation": deviation, "ddof": ddof,
        "observed_at": current["observed_at"], "available_at": current["available_at"],
        "source": "lele-derived:" + measure, "source_url": current["source_url"],
        "evidence": json.dumps({
            "method": METHOD, "measure": measure, "derived_from_kind": kind,
            "derived_from_source": current["source"], "derived_from_url": current["source_url"],
            "instrument_key": instrument_key, "value": rendered, "unit": unit,
            "prior_observed_at": prior["observed_at"],
            "prior_value": _exact(prior["value"]),
            "cadence_seconds": cadence, "baseline_observations": baseline,
            "baseline_mean": mean, "baseline_deviation": deviation, "ddof": ddof,
            "parameter_digits": PARAMETER_DIGITS,
            "derived_at": now.isoformat(),
            "semantics": SEMANTICS[measure],
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
    }


SEMANTICS = {
    "change": "the difference between two consecutive stored levels of the same series. It "
              "is a change in the recorded quantity, not a purchase, a sale, an inflow or an "
              "outflow, and it cannot separate a new actor from a rotation",
    "zscore": "the level in units of its own trailing standard deviation, with the baseline "
              "excluding the point being scored. It is a position in this series' own recent "
              "distribution, not a probability, and not comparable to a z-score of a "
              "different series without its baseline",
}

LIMITATIONS = [
    "A derived quantity is a re-expression of stored observations, not a new reading. "
    "Nothing here observed anything.",
    "The level each measure came from is preserved in the parent observation row and is "
    "not replaced by it.",
    "A difference spanning a hole is refused rather than computed, so a derived series "
    "can be shorter than its parent and holes are counted.",
    "A z-score needs a baseline that excludes the point scored, so it starts later than "
    "a change and its first points do not exist.",
    "A stationary quantity removes a calendar trend. It does not make a context series "
    "a cause of a move, and it does not remove any other confound, including a difference "
    "in which moves and controls happen to sit.",
    "The measures here are computed from the stored rows only. A restatement of a past "
    "value changes every derived point after it, which is why the parameters are stored "
    "on the row.",
]


def store(conn, derived: dict) -> dict:
    """Write one derived series, replacing any earlier derivation of it."""
    if not isinstance(derived, dict) or not isinstance(derived.get("rows"), list):
        raise ValueError("store needs a derived series")
    written = 0
    for row in derived["rows"]:
        if written >= MAX_MEASURE_ROWS:
            break
        registry.add_context_measure(conn, **row)
        written += 1
    return {"series": derived.get("series"), "measure": derived.get("measure"),
            "written": written, "status": derived.get("status")}


def report(conn, kind: str, *, instrument_key: str = "", measure: str = "",
           baseline: int = MINIMUM_BASELINE, now=None) -> dict:
    """What is stored for a measure series, and what could be derived from it."""
    moment = now if now is not None else clock.now()
    measures = [measure] if measure else list(MEASURES)
    stored = registry.context_measure_series(conn)
    series = []
    for name in measures:
        validate(name, kind, baseline)
        derived = derive(conn, kind, instrument_key=instrument_key, measure=name,
                         baseline=baseline, now=moment)
        stored_rows = [item for item in stored
                       if item["source_kind"] == kind
                       and item["instrument_key"] == instrument_key
                       and item["measure"] == name]
        series.append({
            "series": derived["series"], "measure": name, "kind": kind,
            "instrument_key": instrument_key, "baseline": derived["baseline"],
            "derivable": derived["derived"], "status": derived["status"],
            "stored": sum(int(item["observations"]) for item in stored_rows),
            "stored_from": min((item["first"] for item in stored_rows), default=None),
            "stored_to": max((item["last"] for item in stored_rows), default=None),
            "points_available": derived["points_available"],
            "cadence_seconds": derived["cadence_seconds"], "unit": derived["unit"],
            "holes": derived["holes"], "refused": derived["refused"], "reason": derived["reason"],
        })
    return {"method": METHOD, "kind": kind, "instrument_key": instrument_key,
            "measures": list(MEASURES), "not_offered": dict(sorted(NOT_OFFERED.items())),
            "minimum_baseline": MINIMUM_BASELINE, "series": series,
            "read_only": True, "limitations": LIMITATIONS}


def stored_points(conn, kind: str, measure: str, instrument_key: str, *,
                  start: str = "", end: str = "") -> list[dict]:
    """Every stored point of one measure series, in observation order.

    Read whole rather than per window: a comparison draws hundreds of windows, and
    one query per window per series turns a bounded report into hundreds of
    thousands of queries. Which points fall inside a window is decided by
    `window_value`, which is where the availability rule lives.
    """
    if measure not in MEASURES:
        raise ValueError(f"measure must be one of {', '.join(MEASURES)}")
    sql = ("SELECT * FROM context_measures WHERE source_kind=? AND measure=?"
           " AND instrument_key=?")
    params: list = [kind, measure, instrument_key]
    if start:
        sql += " AND observed_at>=?"
        params.append(start)
    if end:
        sql += " AND observed_at<=?"
        params.append(end)
    sql += " ORDER BY observed_at, id LIMIT ?"
    params.append(MAX_MEASURE_ROWS)
    return [dict(row) for row in conn.execute(sql, params)]


def baselines_in_use(conn) -> dict:
    """The baseline length stored for each measure series, and whether it is one.

    A z-score against 60 points and a z-score against 120 are different quantities
    and both are storable. When a series carries more than one, a reader that
    averaged them or picked the first would be making an unstated choice, so the
    ambiguity is returned and the caller reports it instead of resolving it.
    """
    found: dict[tuple, set] = {}
    for row in registry.context_measure_series(conn):
        key = (row["source_kind"], row["instrument_key"], row["measure"])
        found.setdefault(key, set()).add(int(row["baseline_observations"]))
    return {key: sorted(values) for key, values in found.items()}


def window_value(points: list[dict], start: datetime, end: datetime) -> tuple:
    """Mean stored measure inside the window, with what it left out.

    The availability rule is `signals.stored`'s, applied the same way and for the
    same reason: a point that falls inside the window by observation but was
    recorded as available after the window closed is newer than this reference
    point, so it is left out rather than passed off as context the window could
    have contained. Nothing is reported as absent that is merely withheld, so the
    count of withheld points is returned beside the mean.
    """
    inside = [point for point in points
              if _moment(point["observed_at"]) >= start
              and _moment(point["observed_at"]) < end]
    kept = [point for point in inside
            if not point["available_at"] or _moment(point["available_at"]) <= end]
    values = []
    for point in kept:
        try:
            values.append(float(point["value"]))
        except (ValueError, TypeError):
            return None, len(inside) - len(kept)
    if not values:
        return None, len(inside) - len(kept)
    return sum(values) / len(values), len(inside) - len(kept)


def coverage(conn, kind: str, measure: str, instrument_key: str, window_seconds: timedelta,
             *, baseline: int | None = None) -> tuple | None:
    """The span over which a full pre-window could contain this measure.

    The same rule the stored-observation coverage uses, and for the same reason: a
    measure recorded for the last few weeks compared against controls sampled
    from a year of history turns "not yet derived" into "did not happen".
    """
    clause = ""
    params: list = [kind, measure, instrument_key]
    if baseline is not None:
        clause = " AND baseline_observations=?"
        params.append(baseline)
    row = conn.execute(
        "SELECT MIN(observed_at) AS first, MAX(available_at) AS last FROM context_measures"
        " WHERE source_kind=? AND measure=? AND instrument_key=?" + clause, params).fetchone()
    if row is None or not row["first"] or not row["last"]:
        return None
    first = _moment(row["first"])
    last = _moment(row["last"])
    if last - (first - window_seconds) < window_seconds:
        return None
    return (first - window_seconds, last)
