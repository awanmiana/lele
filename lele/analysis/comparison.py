import hashlib
from datetime import timedelta
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext

from ..core import registry
from . import projection

METHODS = (
    projection.METHOD,
    "five_minute_linear_extrapolation_v1",
    "five_minute_trailing_mean_3_v1",
)


def _evaluate(observations, start, end):
    hits = dict.fromkeys(METHODS, 0)
    nonpositive = dict.fromkeys(METHODS, 0)
    errors = dict.fromkeys(METHODS, Decimal(0))
    predictions = []
    warmup, gaps = 0, 0
    step = timedelta(seconds=projection.HORIZON_SECONDS)
    for index in range(start, end):
        if index < 3:
            warmup += 1
            continue
        window = observations[index - 3:index + 1]
        if any(right[0] - left[0] != step for left, right in zip(window, window[1:])):
            gaps += 1
            continue
        history = [point[1] for point in window[:3]]
        numerators = (3 * history[-1], 3 * (2 * history[-1] - history[-2]), sum(history))
        actual = observations[index][1]
        prices = {}
        for method, numerator in zip(METHODS, numerators):
            error = abs(3 * actual - numerator)
            hits[method] += error == 0
            errors[method] += error
            nonpositive[method] += numerator <= 0
            prices[method] = str(numerator / 3)
        predictions.append({
            "target_index": index,
            "history_start": window[0][0].isoformat(),
            "as_of": window[2][0].isoformat(),
            "target_time": window[3][0].isoformat(),
            "actual": str(actual),
            "prices": prices,
        })
    count = len(predictions)
    return {
        "status": "evaluated" if count else "insufficient_data" if end <= 3 else "no_eligible_targets",
        "target_observations": end - start,
        "eligible_predictions": count,
        "skipped_warmup_targets": warmup,
        "skipped_gap_targets": gaps,
        "methods": {
            method: {
                "eligible_predictions": count,
                "exact_hits": hits[method],
                "exact_match_rate": str(Decimal(hits[method]) / count) if count else None,
                "mae": str(errors[method] / (3 * count)) if count else None,
                "nonpositive_forecasts": nonpositive[method],
            }
            for method in METHODS
        },
        "predictions": predictions,
    }, errors


def compare(conn, entity_id: int, path, train_percent: int = 70) -> dict:
    if type(entity_id) is not int or not 1 <= entity_id <= 2**63 - 1:
        raise ValueError("entity id must be a positive SQLite integer")
    if type(train_percent) is not int or not 1 <= train_percent <= 99:
        raise ValueError("train percent must be an integer from 1 to 99")
    with localcontext(Context(prec=128, rounding=ROUND_HALF_EVEN)):
        instrument, _, observations, _, raw = projection._load(path)
        entity = registry.get_entity(conn, entity_id)
        if entity is None:
            raise ValueError(f"entity {entity_id} not found")
        if entity["key"] != instrument["entity_key"]:
            raise ValueError("instrument.entity_key does not match the selected registry entity")
        split = len(observations) * train_percent // 100
        training, errors = _evaluate(observations, 0, split)
        selected = None
        if training["eligible_predictions"]:
            selected = min(METHODS, key=lambda method: (
                -training["methods"][method]["exact_hits"], errors[method], METHODS.index(method),
            ))
        test, _ = _evaluate(observations, split, len(observations))
    return {
        "method": "five_minute_chronological_comparison_v1",
        "evaluation_kind": "exploratory_historical_not_prospective",
        "entity_id": entity_id,
        "instrument": instrument,
        "horizon_seconds": projection.HORIZON_SECONDS,
        "split": {
            "train_percent": train_percent,
            "rule": "first floor(observation_count * train_percent / 100) observations; targets partitioned by index",
            "train_observations": split,
            "test_observations": len(observations) - split,
            "train_start": observations[0][0].isoformat() if split else None,
            "train_end": observations[split - 1][0].isoformat() if split else None,
            "test_start": observations[split][0].isoformat(),
            "test_end": observations[-1][0].isoformat(),
        },
        "selection": {
            "status": "selected" if selected else "no_eligible_training_targets",
            "rule": "training exact_hits descending, exact total absolute error ascending, fixed method order",
            "tie_order": list(METHODS),
            "method": selected,
            "test_score": test["methods"][selected] if selected else None,
        },
        "training": training,
        "test": test,
        "objective": {
            "status": "not_achieved",
            "reason": "Exploratory historical held-out scores are not independent prospective multi-asset validation.",
        },
        "provenance": {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "input_bytes": len(raw),
            "validation": "syntax_only",
            "truth_verified": False,
        },
        "limitations": [
            "Three fixed methods, no parameter fitting or recent-sample tuning; choose the split before inspecting scores.",
            "Selection uses only training targets and is frozen before test evaluation; test method scores are diagnostic, not reselection.",
            "Every method uses identical targets with three prior observations and a target exactly 300 seconds apart; no gap bridging.",
            "Test walk-forward uses observed prior test prices, never current or future labels; initial test history may include training observations.",
            "Nonpositive forecasts are retained unclamped, counted and scored on the same denominator, not valid positive-price quotes.",
            "Exact hits and error totals use exact numerator arithmetic in thirds; MAE ranking is equivalent to total error on identical targets.",
            "Forecast prices, rates and MAE are decimal strings rounded to 128 significant digits, half-even; displayed rounded means do not define exact hits.",
            "Constant, stale or selected prices can inflate hits; repeated inspection of held-out scores compromises the holdout.",
            "Input identity beyond the registry key and price truth are unverified; publication availability, revisions, sessions, splits and futures rolls are not repaired.",
            "No network, database writes, live forecast, prospective accuracy claim, cross-market generalization or trading advice.",
        ],
    }
