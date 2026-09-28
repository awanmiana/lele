import bisect
import hashlib
from datetime import timedelta
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext

from ..core import registry
from . import events, projection, worldstate

THRESHOLD_DEFAULT = 3
THRESHOLD_MAX = 1000
HORIZON_HOURS_DEFAULT = 72
HORIZON_HOURS_MAX = 72
EPISODES_DEFAULT = 100
EPISODES_MAX = 1000
CONTROL_STRIDE_DEFAULT = 12
CONTROL_STRIDE_MAX = 10000


def _load_prices(price_path):
    instrument, _, observations, _, raw = projection._load(price_path)
    return instrument, observations, raw


def _leg(points, start, end, direction):
    start_price, end_price = points[start][1], points[end][1]
    with localcontext(Context(prec=128, rounding=ROUND_HALF_EVEN)):
        magnitude = (end_price - start_price) / start_price * 100
        duration = Decimal(int((points[end][0] - points[start][0]).total_seconds())) / Decimal(3600)
    return {
        "direction": direction,
        "start": points[start][0].isoformat(),
        "end": points[end][0].isoformat(),
        "start_index": start,
        "end_index": end,
        "start_price": str(start_price),
        "end_price": str(end_price),
        "magnitude_percent": str(magnitude),
        "duration_hours": str(duration),
    }


def _zigzag(points, threshold):
    legs: list[dict] = []
    if len(points) < 2:
        return legs
    start = 0
    direction = 0
    extreme = 0
    with localcontext(Context(prec=128, rounding=ROUND_HALF_EVEN)):
        for index in range(1, len(points)):
            price = points[index][1]
            if direction == 0:
                change = (price - points[0][1]) / points[0][1] * 100
                if change > threshold:
                    direction, extreme = 1, index
                elif change < -threshold:
                    direction, extreme = -1, index
                continue
            if direction == 1:
                if price > points[extreme][1]:
                    extreme = index
                elif (points[extreme][1] - price) / points[extreme][1] * 100 >= threshold:
                    legs.append(_leg(points, start, extreme, "up"))
                    start, direction, extreme = extreme, -1, index
            else:
                if price < points[extreme][1]:
                    extreme = index
                elif (price - points[extreme][1]) / points[extreme][1] * 100 >= threshold:
                    legs.append(_leg(points, start, extreme, "down"))
                    start, direction, extreme = extreme, 1, index
    if direction != 0 and extreme != start:
        legs.append(_leg(points, start, extreme, "up" if direction == 1 else "down"))
    return legs


def _regime(times, points, start_index, horizon_seconds, threshold):
    target = points[start_index][0] - timedelta(seconds=horizon_seconds)
    position = bisect.bisect_right(times, target) - 1
    if position < 0 or position >= start_index:
        return {"lookback_return_percent": None, "label": "unknown"}
    with localcontext(Context(prec=128, rounding=ROUND_HALF_EVEN)):
        change = (points[start_index][1] - points[position][1]) / points[position][1] * 100
    if change > threshold:
        label = "bull"
    elif change < -threshold:
        label = "bear"
    else:
        label = "range"
    return {"lookback_return_percent": str(change), "label": label}


def _aggregate_precursors(samples):
    with_evidence = [sample for sample in samples
                     if sample["precursor"] and sample["precursor"]["records"]]
    kind_totals: dict[str, int] = {}
    total_score = 0
    total_sentiment = Decimal(0)
    for sample in with_evidence:
        for kind, count in sample["precursor"]["kinds"].items():
            kind_totals[kind] = kind_totals.get(kind, 0) + count
        total_score += sample["precursor"]["direction_score"]
        total_sentiment += Decimal(sample["precursor"]["sentiment"]["total"])
    count = len(with_evidence)
    return {
        "with_evidence": count,
        "mean_direction_score": str(Decimal(total_score) / count) if count else None,
        "mean_sentiment": str(total_sentiment / count) if count else None,
        "precursor_kind_totals": kind_totals,
    }


def analyze(conn, entity_id, price_path, evidence_path=None, threshold_percent=THRESHOLD_DEFAULT,
            horizon_hours=HORIZON_HOURS_DEFAULT, limit=EPISODES_DEFAULT,
            control_stride=CONTROL_STRIDE_DEFAULT):
    if type(entity_id) is not int or not 1 <= entity_id <= 2**63 - 1:
        raise ValueError("entity id must be a positive SQLite integer")
    if type(threshold_percent) is not int or not 1 <= threshold_percent <= THRESHOLD_MAX:
        raise ValueError(f"threshold percent must be an integer from 1 to {THRESHOLD_MAX}")
    if type(horizon_hours) is not int or not 1 <= horizon_hours <= HORIZON_HOURS_MAX:
        raise ValueError(f"horizon hours must be an integer from 1 to {HORIZON_HOURS_MAX}")
    if type(limit) is not int or not 1 <= limit <= EPISODES_MAX:
        raise ValueError(f"limit must be an integer from 1 to {EPISODES_MAX}")
    if type(control_stride) is not int or not 1 <= control_stride <= CONTROL_STRIDE_MAX:
        raise ValueError(f"control stride must be an integer from 1 to {CONTROL_STRIDE_MAX}")
    instrument, points, raw = _load_prices(price_path)
    entity = registry.get_entity(conn, entity_id)
    if entity is None:
        raise ValueError(f"entity {entity_id} not found")
    if entity["key"] != instrument["entity_key"]:
        raise ValueError("instrument.entity_key does not match the selected registry entity")
    records, evidence_hash = events._evidence(evidence_path, instrument["entity_key"])
    legs = _zigzag(points, Decimal(threshold_percent))
    times = [point[0] for point in points]
    horizon_seconds = horizon_hours * 3600
    episodes = []
    for leg in legs:
        leg["regime"] = _regime(times, points, leg["start_index"], horizon_seconds, threshold_percent)
        if records:
            precursor = worldstate.build_features(records, points[leg["start_index"]][0], horizon_seconds)
        else:
            precursor = None
        leg["precursor"] = precursor
        episodes.append(leg)
    newest = episodes[-limit:]
    comparison = {}
    for label in ("pump", "dump"):
        selected = [episode for episode in episodes
                    if (episode["direction"] == "up") == (label == "pump")]
        with_evidence = [episode for episode in selected if episode["precursor"]
                         and episode["precursor"]["records"]]
        kind_totals: dict[str, int] = {}
        total_score = 0
        total_sentiment = Decimal(0)
        for episode in with_evidence:
            for kind, count in episode["precursor"]["kinds"].items():
                kind_totals[kind] = kind_totals.get(kind, 0) + count
            total_score += episode["precursor"]["direction_score"]
            total_sentiment += Decimal(episode["precursor"]["sentiment"]["total"])
        count = len(with_evidence)
        comparison[label] = {
            "episodes": len(selected),
            "episodes_with_evidence": count,
            "mean_direction_score": str(Decimal(total_score) / count) if count else None,
            "mean_sentiment": str(total_sentiment / count) if count else None,
            "precursor_kind_totals": kind_totals,
        }
    excluded: set[int] = set()
    for leg in legs:
        excluded.update(range(leg["start_index"], leg["end_index"] + 1))
        lower = bisect.bisect_left(times, points[leg["start_index"]][0]
                                   - timedelta(seconds=horizon_seconds))
        excluded.update(range(max(0, lower), leg["start_index"] + 1))
    candidates = [index for index in range(len(points)) if index not in excluded]
    strided = candidates[::control_stride]
    sampled = strided[:limit]
    controls = []
    with localcontext(Context(prec=128, rounding=ROUND_HALF_EVEN)):
        for index in sampled:
            precursor = (worldstate.build_features(records, points[index][0], horizon_seconds)
                         if records else None)
            controls.append({"index": index, "as_of": points[index][0].isoformat(),
                             "precursor": precursor})
        control_comparison = _aggregate_precursors(controls)
    control_comparison.update({
        "definition": "price points outside every leg span and outside the horizon before each "
                      "leg start",
        "stride": control_stride,
        "eligible": len(candidates),
        "sampled": len(sampled),
        "truncated": len(strided) > limit,
    })
    return {
        "method": "price_episodes_v1",
        "entity": dict(entity),
        "instrument": instrument,
        "parameters": {
            "threshold_percent": threshold_percent,
            "horizon_hours": horizon_hours,
            "zigzag_rule": "confirm a reversal only after a strict opposite move of at least the threshold",
        },
        "coverage": {
            "price_points": len(points),
            "start": points[0][0].isoformat(),
            "end": points[-1][0].isoformat(),
            "legs": len(legs),
            "evidence_records_supplied": len(records),
            "evidence_coverage": "unknown",
            "partial_final_leg": len(legs) > 0 and (
                legs[-1]["end_index"] == len(points) - 1),
        },
        "summary": {
            "pumps": sum(1 for episode in episodes if episode["direction"] == "up"),
            "dumps": sum(1 for episode in episodes if episode["direction"] == "down"),
            "precursor_comparison": comparison,
            "control_comparison": control_comparison,
        },
        "episodes": newest,
        "controls": controls,
        "truncated": len(episodes) > limit,
        "provenance": {
            "prices_sha256": hashlib.sha256(raw).hexdigest(),
            "evidence_sha256": evidence_hash,
            "truth_verified": False,
        },
        "limitations": [
            "ZigZag legs are threshold-defined and depend on the chosen percent; a different threshold yields different episodes.",
            "Each leg's precursor covers the preceding horizon ending at the leg start, and uses only evidence available by then; coverage is unknown.",
            "Overlapping or nested legs are not independent samples; a final leg may be incomplete and is flagged in coverage.partial_final_leg.",
            "Non-episode controls are price points outside every leg span and outside each leg's preceding horizon; they are not matched to episodes, can still overlap each other, and no significance test is applied.",
            "A precursor association or a pump-versus-dump-versus-control mean difference is not causation and no next-occurrence prediction is validated; there is no accuracy or objective claim.",
            "Prices and evidence are user supplied and syntactically validated only; no split, roll or corporate-action repair.",
        ],
    }
