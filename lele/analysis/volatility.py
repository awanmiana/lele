"""Measure how much an asset moved, and how much it usually moves.

This module exists because `moves.py` had a column named
`realized_volatility_percent` holding the high-low range of one terminal bar. That
number was correct arithmetic under the wrong name, so nothing in the project
could say how volatile an instrument actually is. These estimators can, and each
one records the convention that produced it so the number is reproducible.

Three properties decide how these are written.

1. *Variance is aggregated, then one square root is taken.* Averaging per-bar
   variances and rooting once is correct; averaging per-bar volatilities is biased
   low by Jensen's inequality. Every range estimator here sums its per-bar terms
   first. Garman-Klass and Rogers-Satchell terms are signed, so a window total can
   legitimately be zero or negative -- that is reported as `unknown`, never
   clamped, because clamping biases the estimate silently.

2. *Log ratios are taken directly.* `ln(high / low)` rounds once; `ln(high) -
   ln(low)` rounds three times. Mixed conventions are the usual reason two
   libraries disagree in the fourth decimal.

3. *What cannot be computed is `unknown`, never `False`.* Realized kernel and
   bipower jump detection both need an intraday sampling grid. Given daily bars
   they are not approximations, they are unavailable, and an estimate that
   silently substituted something else would look like a result.

Estimator choice is not a matter of taste. Across replicated studies the R-squared
of all five range estimators against next-period realized volatility is nearly
identical (roughly 43-46%); what the high/low information buys is calibration, not
predictive power. The average of Parkinson, Garman-Klass and Rogers-Satchell
variances -- `lpv` here -- is the defensible default for the reason Lyocsa, Plihal
and Vyrost give: with no prior information about which estimator is most accurate,
averaging is the choice that is hard to get badly wrong. Yang-Zhang is offered
separately because it is the only range estimator that carries an explicit
overnight-gap term, which matters where a session opens against a closed market.

Everything here is Decimal. Nothing calls the system clock.
"""

import json
from decimal import (Context, Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext)

from ..core import clock, registry

METHOD = "realized_volatility_v1"
PRECISION = 80
ROUNDING = ROUND_HALF_EVEN

MIN_WINDOW_BARS = 2
MAX_WINDOW_BARS = 2000
MAX_BARS = 20000

#: Yang-Zhang's weight on the open-to-close term. Two reputable references give
#: two formulas for this (a ~0.2% difference in `k`, which will fail a
#: cross-library comparison). TTR's form is used because its open-to-close term is
#: internally consistent, and the choice is stored in every row's `convention`.
YANG_ZHANG_ALPHA = Decimal("1.34")

#: Wilder's smoothing length for ATR. Seeding convention is pinned below.
ATR_PERIOD = 14

#: Trading or calendar days in a year, by asset class. A convention for
#: comparability, not a derived scaling law: sqrt-of-time is an extrapolation from
#: a random walk and understates how volatility actually scales in fat-tailed data.
ANNUALIZATION_DAYS = {
    "bitcoin": 365, "crypto": 365,
    "gold": 252, "silver": 252, "oil": 252, "fuel": 252, "commodity": 252,
    "stock": 252, "etf": 252, "index": 252, "bond": 252, "fx": 252, "volatility": 252,
}
SECONDS_PER_DAY = 86400

ESTIMATORS = ("close_to_close", "close_to_close_demeaned", "parkinson", "garman_klass",
              "rogers_satchell", "yang_zhang", "lpv", "atr", "natr")

#: Estimators that read the bar before the first one of the window, so a two-bar
#: window is not enough for them. Parkinson is deliberately absent: it uses only
#: each bar's own high and low.
NEEDS_PREVIOUS_CLOSE = frozenset({"garman_klass", "rogers_satchell", "yang_zhang", "lpv", "atr",
                                  "natr"})

UNKNOWN = "unknown"


def _ctx() -> Context:
    return Context(prec=PRECISION, rounding=ROUNDING)


def _ln_ratio(high: Decimal, low: Decimal) -> Decimal:
    """ln(high / low), taking one division and one logarithm rather than three ops."""
    return (high / low).ln()


def variance(values: list[Decimal], ddof: int) -> Decimal:
    """Sample variance by the two-pass route.

    The algebraically equivalent shortcut sum(x*x) - n*mean*mean cancels
    catastrophically when the variance is small relative to the mean, which for
    prices is always. `ddof` is a required argument rather than a default because
    the callers legitimately disagree: close-to-close uses n-2, Bollinger uses n
    (population), and a trailing baseline uses its own. One function with an
    explicit argument, three call sites, no chance of a silent mismatch.
    """
    data = list(values)
    count = len(data)
    if count < 1:
        raise ValueError("variance needs at least one value")
    if type(ddof) is not int or ddof < 0:
        raise ValueError("ddof must be a nonnegative integer")
    if ddof >= count:
        raise ValueError(f"ddof {ddof} needs more than {count} value(s)")
    with localcontext(_ctx()):
        mean = sum(data, Decimal(0)) / count
        total = sum(((value - mean) ** 2 for value in data), Decimal(0))
        return total / (count - ddof)


def stdev(values: list[Decimal], ddof: int) -> Decimal | None:
    """sqrt(variance), or None when the total is not strictly positive."""
    value = variance(values, ddof)
    if value <= 0:
        return None
    with localcontext(_ctx()):
        return value.sqrt()


def _percent(value: Decimal, places: str = "0.0001") -> str:
    """A reported percent, quantized once and rendered as text.

    Text, not Decimal: these values end up in a JSON report, and `json.dumps`
    refuses a Decimal. `moves._round` renders the same way for the same reason.
    """
    with localcontext(_ctx()):
        return str(value.quantize(Decimal(places)))


def _log_returns(closes: list[Decimal]) -> list[Decimal]:
    with localcontext(_ctx()):
        return [_ln_ratio(closes[i], closes[i - 1]) for i in range(1, len(closes))]


# --- per-bar variance contributions ---

def _close_to_close_terms(closes: list[Decimal]) -> list[Decimal]:
    with localcontext(_ctx()):
        return [value ** 2 for value in _log_returns(closes)]


def _demeaned_close_to_close(closes: list[Decimal]) -> Decimal:
    returns = _log_returns(closes)
    count = len(returns)
    if count < 2:
        return Decimal(0)
    with localcontext(_ctx()):
        mean = sum(returns, Decimal(0)) / count
        return sum(((value - mean) ** 2 for value in returns), Decimal(0)) / (count - 1)


def _parkinson_terms(highs: list[Decimal], lows: list[Decimal]) -> list[Decimal]:
    with localcontext(_ctx()):
        scale = Decimal(1) / (4 * Decimal(2).ln())
        return [scale * _ln_ratio(highs[i], lows[i]) ** 2 for i in range(len(highs))]


def _garman_klass_terms(opens: list[Decimal], highs: list[Decimal], lows: list[Decimal],
                      closes: list[Decimal]) -> list[Decimal]:
    """Signed per-bar terms. Sum over the window before rooting."""
    adjustment = 2 * Decimal(2).ln() - 1
    with localcontext(_ctx()):
        terms = []
        for index in range(1, len(closes)):
            range_term = _ln_ratio(highs[index], lows[index]) ** 2 / 2
            body_term = adjustment * _ln_ratio(closes[index], opens[index]) ** 2
            terms.append(range_term - body_term)
        return terms


def _rogers_satchell_terms(opens: list[Decimal], highs: list[Decimal], lows: list[Decimal],
                          closes: list[Decimal]) -> list[Decimal]:
    """Signed per-bar terms. Sum over the window before rooting."""
    with localcontext(_ctx()):
        terms = []
        for index in range(1, len(closes)):
            high, low = highs[index], lows[index]
            open_, close = opens[index], closes[index]
            terms.append(_ln_ratio(high, close) * _ln_ratio(high, open_)
                         + _ln_ratio(low, close) * _ln_ratio(low, open_))
        return terms


def _yang_zhang_variance(opens: list[Decimal], highs: list[Decimal], lows: list[Decimal],
                        closes: list[Decimal]) -> Decimal | None:
    """Multi-period estimator carrying an explicit overnight-gap term.

    Yang and Zhang proved no single-period estimator can handle both non-zero
    drift and an opening jump; this one pays for it by decomposing the return into
    overnight, open-to-close and intraday-range parts.
    """
    bars = len(closes)
    if bars < 3:
        return None
    with localcontext(_ctx()):
        overnight, body = [], []
        for index in range(1, bars):
            overnight.append(_ln_ratio(opens[index], closes[index - 1]))
            body.append(_ln_ratio(closes[index], opens[index]))
        overnight_mean = sum(overnight, Decimal(0)) / len(overnight)
        body_mean = sum(body, Decimal(0)) / len(body)
        overnight_var = sum(((x - overnight_mean) ** 2 for x in overnight),
                            Decimal(0)) / (len(overnight) - 1)
        body_var = sum(((x - body_mean) ** 2 for x in body), Decimal(0)) / (len(body) - 1)
        rsv = sum(_rogers_satchell_terms(opens, highs, lows, closes), Decimal(0)) / (bars - 1)
        alpha = YANG_ZHANG_ALPHA
        weight = (alpha - 1) / (alpha + Decimal(bars) / Decimal(bars - 2))
        total = overnight_var + weight * body_var + (1 - weight) * rsv
        return total if total > 0 else None


def _true_ranges(opens: list[Decimal], highs: list[Decimal], lows: list[Decimal],
                 closes: list[Decimal]) -> list[Decimal]:
    """max(high - low, |high - previous close|, |low - previous close|)."""
    with localcontext(_ctx()):
        ranges = [highs[0] - lows[0]]
        for index in range(1, len(closes)):
            previous = closes[index - 1]
            ranges.append(max(highs[index] - lows[index],
                              abs(highs[index] - previous),
                              abs(lows[index] - previous)))
        return ranges


def _bars(opens, highs, lows, closes) -> int:
    """Check the four series are the same length before any arithmetic on them.

    Without this a caller that passes a misaligned series gets an IndexError from
    deep inside an estimator, which names neither the caller nor the lengths.
    """
    lengths = {len(opens), len(highs), len(lows), len(closes)}
    if len(lengths) != 1:
        raise ValueError(f"open, high, low and close must have equal lengths, got "
                         f"{len(opens)}, {len(highs)}, {len(lows)}, {len(closes)}")
    return lengths.pop()


def wilder_atr(opens: list[Decimal], highs: list[Decimal], lows: list[Decimal],
              closes: list[Decimal], period: int = ATR_PERIOD) -> list[tuple[Decimal, Decimal]]:
    """Wilder's smoothed average true range, and the smoothed high-low range.

    The first value is the arithmetic mean of the first `period` true ranges,
    measured from the start of the series rather than from the newest bar. That
    seeding choice is the classic ATR implementation disagreement and it shifts
    every later value, so it is pinned here and recorded in the convention.

    ATR is subtracted from prices, so it is invariant to the additive level shifts
    that a back-adjusted or spliced series introduces. Log-return volatility is
    invariant to those shifts too, but NATR, which divides ATR by the price, is
    not -- so on a spliced series the two usable figures are the log-return
    estimators and absolute ATR, and NATR is the one to distrust.
    """
    _bars(opens, highs, lows, closes)
    if type(period) is not int or not 2 <= period <= MAX_WINDOW_BARS:
        raise ValueError(f"period must be an integer from 2 to {MAX_WINDOW_BARS}")
    if len(closes) < period:
        return []
    with localcontext(_ctx()):
        ranges = _true_ranges(opens, highs, lows, closes)
        widths = [highs[index] - lows[index] for index in range(len(closes))]
        atr_values, width_values = [], []
        atr = sum(ranges[:period], Decimal(0)) / period
        width = sum(widths[:period], Decimal(0)) / period
        atr_values.append(atr)
        width_values.append(width)
        for index in range(period, len(closes)):
            atr = (atr * (period - 1) + ranges[index]) / period
            width = (width * (period - 1) + widths[index]) / period
            atr_values.append(atr)
            width_values.append(width)
        return list(zip(atr_values, width_values))


# --- windowed variance dispatch ---

def window_variance(opens, highs, lows, closes, estimator: str) -> Decimal | None:
    """Variance over one window, or None where the window cannot support it.

    None means the estimator is undefined or unusable here -- too few bars, a
    non-positive Garman-Klass or Rogers-Satchell total, or an all-identical-price
    window whose variance is genuinely zero. A zero variance is not a volatility
    of zero to be divided by later, so it is refused on the same footing as a
    missing value and never clamped to a small number.
    """
    if estimator not in ESTIMATORS:
        raise ValueError(f"estimator must be one of {', '.join(ESTIMATORS)}")
    _bars(opens, highs, lows, closes)
    bars = len(closes)
    if bars < MIN_WINDOW_BARS:
        return None
    if estimator in NEEDS_PREVIOUS_CLOSE and bars < 3:
        return None
    with localcontext(_ctx()):
        if estimator == "close_to_close":
            terms = _close_to_close_terms(closes)
            if not terms:
                return None
            total = sum(terms, Decimal(0)) / len(terms)
            return total if total > 0 else None
        if estimator == "close_to_close_demeaned":
            value = _demeaned_close_to_close(closes)
            return value if value > 0 else None
        if estimator == "parkinson":
            terms = _parkinson_terms(highs, lows)
            total = sum(terms, Decimal(0)) / len(terms)
            return total if total > 0 else None
        if estimator == "garman_klass":
            terms = _garman_klass_terms(opens, highs, lows, closes)
            total = sum(terms, Decimal(0)) / len(terms)
            return total if total > 0 else None
        if estimator == "rogers_satchell":
            terms = _rogers_satchell_terms(opens, highs, lows, closes)
            total = sum(terms, Decimal(0)) / len(terms)
            return total if total > 0 else None
        if estimator == "yang_zhang":
            return _yang_zhang_variance(opens, highs, lows, closes)
        if estimator == "lpv":
            candidates = [
                _average(_parkinson_terms(highs, lows)),
                _average(_garman_klass_terms(opens, highs, lows, closes)),
                _average(_rogers_satchell_terms(opens, highs, lows, closes)),
            ]
            usable = [value for value in candidates if value is not None and value > 0]
            if not usable:
                return None
            return sum(usable, Decimal(0)) / len(usable)
        if estimator in ("atr", "natr"):
            smoothed = wilder_atr(opens, highs, lows, closes)
            if not smoothed:
                return None
            value, _ = smoothed[-1]
            if value <= 0:
                return None
            return (value / closes[-1]) ** 2
    return None


def _average(terms: list[Decimal]) -> Decimal | None:
    if not terms:
        return None
    return sum(terms, Decimal(0)) / len(terms)


def annualization_basis(interval_seconds: int, asset_class: str) -> int | None:
    """Bars in a year for this series, or None when no whole number exists.

    Bars per year is `days per year for the class` times `bars per day`, so a
    daily equity bar annualizes over 252 sessions while a daily crypto bar
    annualizes over 365 calendar days. Anything that does not divide into a whole
    number of bars returns None rather than a rounded count, and an unrecognized
    class returns None rather than defaulting to 252 -- a wrong annualization is a
    plausible-looking wrong number.
    """
    days = ANNUALIZATION_DAYS.get(asset_class)
    if days is None or interval_seconds <= 0:
        return None
    periods = Decimal(days) * Decimal(SECONDS_PER_DAY) / Decimal(interval_seconds)
    if periods != periods.to_integral_value():
        return None
    value = int(periods)
    return value if value >= 1 else None


def annualized(variance_value: Decimal | None, interval_seconds: int,
               asset_class: str) -> Decimal | None:
    """Scale a per-bar variance to a per-year one under the stated convention."""
    if variance_value is None or variance_value <= 0:
        return None
    bars = annualization_basis(interval_seconds, asset_class)
    if bars is None:
        return None
    with localcontext(_ctx()):
        return variance_value * bars


def convention_for(estimator: str, asset_class: str, interval_seconds: int) -> dict:
    """Everything needed to reproduce a number from the estimator name alone."""
    bars = annualization_basis(interval_seconds, asset_class)
    convention = {
        "precision": PRECISION,
        "rounding": ROUNDING,
        "annualization_bars_per_year": bars,
        "sqrt_of_time_is_a_convention": True,
    }
    if estimator == "close_to_close":
        convention["drift"] = "zero, so the estimate is biased upward by mu squared"
        convention["divisor"] = "n, one unit observation per squared return"
    if estimator == "close_to_close_demeaned":
        convention["drift"] = "mean removed, n-1 over n-1 returns"
    if estimator == "parkinson":
        convention["assumes_no_opening_jump"] = True
        convention["drift"] = "zero"
    if estimator == "garman_klass":
        convention["assumes_no_opening_jump"] = True
        convention["drift"] = "zero"
        convention["per_bar_terms_are_signed"] = True
    if estimator == "rogers_satchell":
        convention["allows_drift"] = True
        convention["assumes_no_opening_jump"] = True
        convention["per_bar_terms_are_signed"] = True
    if estimator == "yang_zhang":
        convention["weight_formula"] = "(alpha-1)/(alpha + T/(T-2))"
        convention["alpha"] = str(YANG_ZHANG_ALPHA)
        convention["handles_overnight_gap"] = True
    if estimator == "lpv":
        convention["components"] = ["parkinson", "garman_klass", "rogers_satchell"]
        convention["weighting"] = "equal on variance; components undefined on the window are dropped"
    if estimator in ("atr", "natr"):
        convention["smoothing"] = "wilder"
        convention["period"] = ATR_PERIOD
        convention["seed"] = "arithmetic mean of the first period true ranges, from the start"
    if estimator == "atr":
        convention["invariant_to_additive_splices"] = True
    if estimator == "natr":
        convention["invariant_to_additive_splices"] = False
        convention["why_not"] = ("NATR divides the price range by the price, so a constant "
                                 "additive splice still rescales it; use absolute ATR or a "
                                 "log-return estimator on a spliced series")
    return convention


# --- movement measures over a window ---

def percent_change(opens: list[Decimal], highs: list[Decimal], lows: list[Decimal],
                  closes: list[Decimal], start_index: int, end_index: int) -> Decimal | None:
    if not (0 <= start_index < end_index < len(closes)):
        return None
    with localcontext(_ctx()):
        base = opens[start_index]
        if base <= 0:
            return None
        return (closes[end_index] - base) / base * 100


def empirical_percentile(baseline: list[Decimal], value: Decimal) -> Decimal | None:
    """Share of `baseline` strictly below `value`, as a percent.

    Preferred over a z-score for fat-tailed returns: it is the empirical CDF, so
    it makes no normality assumption, and a Gaussian table would report a 4-sigma
    move as far more exceptional than an empirical distribution does.
    """
    data = list(baseline)
    if not data:
        return None
    with localcontext(_ctx()):
        below = sum(1 for item in data if abs(item) < abs(value))
        return Decimal(below) / len(data) * 100


def z_score(baseline: list[Decimal], value: Decimal) -> Decimal | None:
    """(value - mean) / stdev over `baseline`, or None when the spread is zero."""
    data = list(baseline)
    if len(data) < 2:
        return None
    with localcontext(_ctx()):
        mean = sum(data, Decimal(0)) / len(data)
        spread = stdev(data, 1)
        if spread is None or spread <= 0:
            return None
        return (value - mean) / spread


def drawdowns(closes: list[Decimal]) -> list[Decimal]:
    """Percent below the running maximum, at each bar. Zero when at a new high.

    The running maximum is one forward pass with O(1) state, so this needs no
    lookback window and is safe to compute over a whole series.
    """
    if not closes:
        return []
    out: list[Decimal] = []
    peak = closes[0]
    for close in closes:
        with localcontext(_ctx()):
            if close > peak:
                peak = close
            out.append((close - peak) / peak * 100 if peak > 0 else Decimal(0))
    return out


def max_drawdown(closes: list[Decimal]) -> Decimal | None:
    series = drawdowns(closes)
    if not series:
        return None
    with localcontext(_ctx()):
        return min(series)


def ulcer_index(closes: list[Decimal], window_bars: int) -> Decimal | None:
    """Root mean squared drawdown over a trailing window.

    Squaring the drawdown penalises depth and persistence together, which is why
    it distinguishes a long shallow decline from a brief deep one. Returned in
    percent, so a 10 value means a typical drawdown of about 10% from the running
    peak.
    """
    if type(window_bars) is not int or not 2 <= window_bars <= MAX_WINDOW_BARS:
        raise ValueError(f"window_bars must be an integer from 2 to {MAX_WINDOW_BARS}")
    series = drawdowns(closes)
    if len(series) < window_bars:
        return None
    tail = series[-window_bars:]
    with localcontext(_ctx()):
        return (sum((value ** 2 for value in tail), Decimal(0)) / len(tail)).sqrt()

# --- thresholds: a percentage that means something ---

#: A hard-coded percent is not scale free (2% is a different event for bitcoin at
#: 60000 than for a large-cap equity), not volatility adjusted, and unreliable on
#: fat-tailed returns. So a threshold is stated against the instrument's own
#: trailing distribution. Percentile is the primary mode because it is the
#: empirical CDF and assumes no distribution at all; z-score is reported beside it
#: because a percentile cannot distinguish the 94th from the 96th at n=20.
THRESHOLD_MODES = ("percentile", "z_score", "absolute")

#: A percentile needs enough baseline to resolve the percentiles it claims. With
#: n observations the finest resolvable percentile is 1/n, so n=20 cannot produce
#: a 99th percentile at all.
MIN_BASELINE_PERCENTILE = 100
MIN_BASELINE_Z = 60

#: The sample standard deviation's relative standard error is about
#: 1/sqrt(2n): 15% at n=20, 9% at n=60, 4% at n=252. Below n=60 a published
#: z-score carries an error in its own denominator large enough to move a
#: borderline observation across a threshold, so it is not published at all.
MIN_BASELINE_ABSOLUTE = 20


def validate_threshold(mode: str, level) -> None:
    if mode not in THRESHOLD_MODES:
        raise ValueError(f"mode must be one of {', '.join(THRESHOLD_MODES)}")
    if type(level) is int:
        level_value = Decimal(level)
    elif isinstance(level, str):
        try:
            level_value = Decimal(level)
        except InvalidOperation as exc:
            raise ValueError("level must be a finite decimal") from exc
    else:
        raise ValueError("level must be an integer or a decimal string")
    if not level_value.is_finite():
        raise ValueError("level must be a finite decimal")
    if mode == "percentile":
        if not 0 < level_value < 100:
            raise ValueError("a percentile level must be strictly between 0 and 100")
    elif mode == "z_score":
        if level_value <= 0:
            raise ValueError("a z_score level must be positive")
    else:
        if level_value <= 0:
            raise ValueError("an absolute level in percent must be positive")


def minimum_baseline(mode: str) -> int:
    return {"percentile": MIN_BASELINE_PERCENTILE, "z_score": MIN_BASELINE_Z,
            "absolute": MIN_BASELINE_ABSOLUTE}[mode]


def evaluate_threshold(baseline: list[Decimal], value: Decimal, *, mode: str,
                       level: str) -> dict:
    """Decide whether `value` is unusual against a baseline that excludes it.

    `baseline` must already omit the observation being judged. Including it
    inflates the mean and deflates the spread, which compresses the score toward
    zero -- the single most common way a threshold ends up never firing.
    """
    validate_threshold(mode, level)
    target = Decimal(level)
    needed = minimum_baseline(mode)
    with localcontext(_ctx()):
        percentile = empirical_percentile(baseline, value)
        score = z_score(baseline, value)
        result = {
            "mode": mode,
            "level": str(target),
            "value": _percent(value),
            "baseline_bars": len(baseline),
            "minimum_baseline_bars": needed,
            "baseline_sufficient": len(baseline) >= needed,
            "baseline_percentile": _percent(percentile) if percentile is not None else None,
            "z_score": _percent(score) if score is not None else None,
        }
        if len(baseline) < needed:
            result["breached"] = None
            result["reason"] = (f"{mode} needs at least {needed} baseline observations and "
                                f"{len(baseline)} are available")
            return result
        if mode == "percentile":
            breached = percentile is not None and percentile >= target
        elif mode == "z_score":
            breached = score is not None and abs(score) >= target
        else:
            breached = abs(value) >= target
        result["breached"] = bool(breached)
        result["reason"] = None
        return result


def series(conn, instrument_key: str, interval_seconds: int) -> dict:
    """Stored bars as Decimals, plus the measured cadence and hole count.

    Cadence is measured from the bars rather than taken from the interval label,
    because that is what lets a window spanning a missing bar be recognised. A
    "24 hour move" computed across a hole is really an eight day move, and calling
    it the former is the defect this whole file is downstream of.
    """
    rows = registry.list_price_bars(conn, instrument_key, interval_seconds,
                                    limit=MAX_BARS, order="asc")
    times, opens, highs, lows, closes = [], [], [], [], []
    for row in rows:
        times.append(row["open_time"])
        opens.append(Decimal(row["open"]))
        highs.append(Decimal(row["high"]))
        lows.append(Decimal(row["low"]))
        closes.append(Decimal(row["close"]))
    return {"rows": rows, "times": times, "opens": opens, "highs": highs, "lows": lows,
            "closes": closes, "cadence_seconds": _cadence(times, interval_seconds),
            "gaps": _gap_index(times, _cadence(times, interval_seconds))}


def _cadence(times: list[str], interval_seconds: int) -> int:
    from datetime import datetime
    if len(times) < 2:
        return interval_seconds
    deltas: dict[int, int] = {}
    for index in range(1, len(times)):
        step = int((datetime.fromisoformat(times[index])
                    - datetime.fromisoformat(times[index - 1])).total_seconds())
        deltas[step] = deltas.get(step, 0) + 1
    return max(deltas.items(), key=lambda item: (item[1], -item[0]))[0]


def _gap_index(times: list[str], cadence: int) -> set[int]:
    from datetime import datetime
    gaps: set[int] = set()
    if cadence <= 0 or len(times) < 2:
        return gaps
    tolerance = cadence * 1.5
    for index in range(1, len(times)):
        step = (datetime.fromisoformat(times[index])
                - datetime.fromisoformat(times[index - 1])).total_seconds()
        if step > tolerance:
            gaps.add(index)
    return gaps


def _window(bars: dict, start: int, end: int, window_bars: int) -> dict:
    if end - start + 1 != window_bars:
        raise ValueError("window length does not match the requested size")
    return {"opens": bars["opens"][start:end + 1], "highs": bars["highs"][start:end + 1],
            "lows": bars["lows"][start:end + 1], "closes": bars["closes"][start:end + 1]}


def estimate_latest(bars: dict, estimator: str, window_bars: int, interval_seconds: int,
                    asset_class: str) -> dict:
    """The most recent windowed estimate, with whatever it could not do recorded."""
    count = len(bars["closes"])
    result: dict = {"estimator": estimator, "window_bars": window_bars,
                    "bars_available": count, "convention": convention_for(
                        estimator, asset_class, interval_seconds)}
    if count < window_bars:
        result.update({"status": "insufficient_bars", "volatility_percent": None,
                       "annualized_percent": None,
                       "reason": f"{window_bars} bars are needed and {count} are stored"})
        return result
    start = count - window_bars
    if any(index in bars["gaps"] for index in range(start + 1, count)):
        result.update({"status": "window_spans_gap", "volatility_percent": None,
                       "annualized_percent": None,
                       "reason": "a bar is missing inside the window, so the estimate would "
                                 "span more elapsed time than the window names"})
        return result
    value = window_variance(**_window(bars, start, count - 1, window_bars), estimator=estimator)
    if value is None or value <= 0:
        result.update({"status": "not_computable", "volatility_percent": None,
                       "annualized_percent": None,
                       "reason": "this estimator is undefined on this window: too few bars, an "
                                 "all-identical-price window, or a non-positive Garman-Klass or "
                                 "Rogers-Satchell total"})
        return result
    scaled = annualized(value, interval_seconds, asset_class)
    with localcontext(_ctx()):
        per_bar = value.sqrt() * 100
        annual_percent = scaled.sqrt() * 100 if scaled is not None else None
    result.update({
        "status": "ok",
        "as_of": bars["times"][-1],
        "window_start": bars["times"][start],
        "variance": _variance_text(value),
        "volatility_percent": _percent(per_bar),
        "annualized_percent": _percent(annual_percent) if annual_percent is not None else None,
        "annualization_bars_per_year": annualization_basis(interval_seconds, asset_class),
        "annualization_known": annual_percent is not None,
    })
    if annual_percent is None:
        result["annualization_reason"] = (
            f"asset class {asset_class!r} has no recorded bars-per-year convention, so the "
            "annualized figure is unknown rather than scaled by an assumed one")
    return result


def measure(conn, instrument_key: str, interval_seconds: int, *, estimators=None,
            window_bars: int = 30, asset_class: str = "", store: bool = False,
            now=None) -> dict:
    """Estimate realized volatility from stored bars, per estimator, with coverage."""
    if type(interval_seconds) is not int or interval_seconds < 60:
        raise ValueError("interval_seconds must be an integer of at least 60")
    if type(window_bars) is not int or not MIN_WINDOW_BARS <= window_bars <= MAX_WINDOW_BARS:
        raise ValueError(f"window_bars must be an integer from {MIN_WINDOW_BARS} to "
                         f"{MAX_WINDOW_BARS}")
    chosen = list(ESTIMATORS) if estimators is None else list(estimators)
    for name in chosen:
        if name not in ESTIMATORS:
            raise ValueError(f"estimator must be one of {', '.join(ESTIMATORS)}")
    instrument = registry.get_instrument(conn, instrument_key)
    classes = [asset_class] if asset_class else []
    if instrument is not None:
        classes = [instrument["asset_class"]] + [item for item in classes
                                                 if item != instrument["asset_class"]]
    resolved_class = classes[0] if classes else UNKNOWN
    bars = series(conn, instrument_key, interval_seconds)
    stamp = (now or clock.now()).replace(microsecond=0).isoformat()
    estimates: list[dict] = []
    not_computable: list[dict] = []
    report: dict = {
        "method": METHOD,
        "instrument_key": instrument_key,
        "interval_seconds": interval_seconds,
        "window_bars": window_bars,
        "asset_class": resolved_class,
        "as_of": bars["times"][-1] if bars["times"] else None,
        "coverage": {
            "bars": len(bars["closes"]),
            "cadence_seconds": bars["cadence_seconds"],
            "gaps": len(bars["gaps"]),
            "contiguous_fraction": _contiguous_fraction(bars),
            "first_bar": bars["times"][0] if bars["times"] else None,
            "last_bar": bars["times"][-1] if bars["times"] else None,
            "completeness": UNKNOWN,
            "completeness_note": "a bounded fetch cannot prove it reached the provider's first "
                                 "bar, so completeness is unknown rather than true or false",
        },
        "instrument_registered": instrument is not None,
        "estimators": estimates,
        "not_computable": not_computable,
        "stored": 0,
    }
    if instrument is not None:
        report["instrument"] = {"symbol": instrument["symbol"], "venue": instrument["venue"],
                                "asset_class": instrument["asset_class"],
                                "quote_currency": instrument["quote_currency"],
                                "adjustment_basis": instrument["adjustment_basis"],
                                "rights_basis": instrument["rights_basis"],
                                "rights_verified": bool(instrument["rights_verified"])}
    if asset_class:
        report["asset_class"] = asset_class
    for name in chosen:
        estimate = estimate_latest(bars, name, window_bars, interval_seconds, resolved_class)
        estimates.append(estimate)
        if estimate["status"] != "ok":
            not_computable.append(
                {"estimator": name, "status": estimate["status"], "reason": estimate["reason"]})
            continue
        if store:
            registry.add_volatility_estimate(
                conn, instrument_key=instrument_key, interval_seconds=interval_seconds,
                estimator=name, window_bars=window_bars, as_of=estimate["as_of"],
                window_start=estimate["window_start"],
                volatility_percent=str(estimate["volatility_percent"]),
                annualized_percent=str(estimate["annualized_percent"]
                                       if estimate["annualized_percent"] is not None else "0"),
                variance=estimate["variance"],
                basis="per_bar",
                annualization=(f"{estimate['annualization_bars_per_year']} bars per year"
                               if estimate.get("annualization_known") else ""),
                convention=json.dumps(estimate["convention"], sort_keys=True),
                observed_at=stamp, available_at=estimate["as_of"])
            report["stored"] += 1
    report["limitations"] = [
        "every estimator here is a realized measure from stored OHLC and says nothing about why "
        "a move happened",
        "realized kernel and bipower jump detection need an intraday sampling grid and are not "
        "computable from daily bars; they are absent rather than approximated",
        "annualization multiplies by a conventional bars-per-year count and is a comparability "
        "convention, not a scaling law",
        "the window names bars, not elapsed time; a window spanning a gap is refused rather "
        "than reported",
    ]
    return report


def _contiguous_fraction(bars: dict) -> str | None:
    """Share of bar-to-bar steps that are adjacent, as a fraction, or None.

    Computed in Decimal because it is a reported figure like any other, and a
    float ratio here is the kind of thing that prints as 0.666667 in one place
    and 0.6666666666666666 in another.
    """
    total = len(bars["times"])
    if not total:
        return None
    with localcontext(_ctx()):
        return _round(Decimal(1) - Decimal(len(bars["gaps"])) / Decimal(total))


def _round(value: Decimal) -> str:
    with localcontext(_ctx()):
        return str(value.quantize(Decimal("0.000001")))


def _variance_text(value: Decimal) -> str:
    """The variance as stored text.

    Twelve decimal places, which keeps enough significant digits for a daily
    per-bar variance (order 1e-4) that `sqrt` reproduces the reported volatility
    percent, while fitting the storage field. Writing the full eighty-digit
    intermediate would be eighty characters of noise past that point.
    """
    with localcontext(_ctx()):
        return str(value.quantize(Decimal("0.000000000001")))

