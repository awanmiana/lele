import hashlib
from datetime import timedelta
from decimal import Context, Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext

from ..core import registry
from . import events, projection

WINDOW_SECONDS_DEFAULT = 300
WINDOW_SECONDS_MAX = 86400
ROWS_DEFAULT = 200
ROWS_MAX = 1000
MAX_NAMES = 100
DIRECTIONAL_KINDS = frozenset({
    "fund_flow", "exchange_transfer", "onchain_transfer", "etf_flow", "stablecoin_mint_burn",
    "liquidation", "market_context", "social_sentiment", "capitulation_indicator",
    "order_book_imbalance", "trade_print", "block_trade", "order_flow", "insider_trade",
    "political_event", "geopolitical_event", "regulatory_action", "market_shock",
})
SENTIMENT_KINDS = frozenset({"market_context", "social_sentiment"})
EVENT_KINDS = frozenset({"news_event", "macro_release", "calendar_event", "filing_event",
                          "political_event", "geopolitical_event", "regulatory_action", "market_shock"})
WEIGHTED_SCORING_METHOD = "kind_weight_recency_buckets_v1"
DIRECTION_WEIGHTS: dict[str, int] = {
    "order_flow": 3,
    "trade_print": 3,
    "block_trade": 3,
    "order_book_imbalance": 3,
    "liquidation": 3,
    "fund_flow": 2,
    "exchange_transfer": 2,
    "onchain_transfer": 2,
    "etf_flow": 2,
    "stablecoin_mint_burn": 2,
    "insider_trade": 2,
    "social_sentiment": 1,
    "market_context": 1,
    "capitulation_indicator": 1,
    "political_event": 2,
    "geopolitical_event": 2,
    "regulatory_action": 1,
    "market_shock": 2,
}
OBSERVATION_INDICATOR_WEIGHTS: dict[str, int] = {
    "fund_flow": 2,
    "insider_trade": 2,
    "macro_release": 1,
    "holding": 1,
    "filing_event": 1,
}
OBSERVATION_INDICATOR_KINDS = frozenset(OBSERVATION_INDICATOR_WEIGHTS.keys())
RECENCY_BUCKETS = 3


def _signal(row):
    measurement = row.get("measurement")
    if row["kind"] not in DIRECTIONAL_KINDS or not isinstance(measurement, dict):
        return 0
    try:
        number = Decimal(measurement["value"])
    except (InvalidOperation, TypeError, KeyError):
        return 0
    if not number.is_finite():
        return 0
    return (number > 0) - (number < 0)


def build_features(records, as_of, window_seconds=WINDOW_SECONDS_DEFAULT):
    lower = as_of - timedelta(seconds=window_seconds)
    kinds: dict[str, int] = {}
    measured: dict[str, dict] = {}
    routed_amounts: dict[str, Decimal] = {}
    origins: set[str] = set()
    destinations: set[str] = set()
    reason_bases: dict[str, int] = {}
    observation_ids: list[str] = []
    direction_score = 0
    directional = 0
    sentiment_total = Decimal(0)
    sentiment_count = 0
    with localcontext(Context(prec=128, rounding=ROUND_HALF_EVEN)):
        for observed, available, row in records:
            if not lower <= observed <= as_of or available > as_of:
                continue
            observation_ids.append(row["id"])
            kind = row["kind"]
            kinds[kind] = kinds.get(kind, 0) + 1
            signal = _signal(row)
            if signal:
                direction_score += signal
                directional += 1
            measurement = row.get("measurement")
            number = None
            if isinstance(measurement, dict):
                try:
                    number = Decimal(measurement["value"])
                except (InvalidOperation, TypeError, KeyError):
                    number = None
            if number is not None and number.is_finite():
                bucket = measured.setdefault(kind, {"count": 0, "total": Decimal(0)})
                bucket["count"] += 1
                bucket["total"] += number
                if kind in SENTIMENT_KINDS:
                    sentiment_total += number
                    sentiment_count += 1
            mapping = row.get("mapping")
            if isinstance(mapping, dict):
                if (mapping.get("from") and mapping.get("to") and number is not None
                        and number.is_finite()):
                    currency = measurement.get("currency") or measurement.get("unit")
                    routed_amounts[currency] = routed_amounts.get(currency, Decimal(0)) + number
                    if len(origins) < MAX_NAMES:
                        origins.add(mapping["from"])
                    if len(destinations) < MAX_NAMES:
                        destinations.add(mapping["to"])
                if mapping.get("reason_basis"):
                    reason_bases[mapping["reason_basis"]] = reason_bases.get(mapping["reason_basis"], 0) + 1
    net_direction = "up" if direction_score > 0 else "down" if direction_score < 0 else "flat"
    return {
        "window": {"start": lower.isoformat(), "end": as_of.isoformat(),
                   "seconds": window_seconds},
        "records": len(observation_ids),
        "observation_ids": observation_ids,
        "kinds": kinds,
        "event_records": sum(count for kind, count in kinds.items() if kind in EVENT_KINDS),
        "direction_score": direction_score,
        "directional_records": directional,
        "net_direction": net_direction,
        "measured_by_kind": {
            kind: {"count": value["count"], "total": str(value["total"])}
            for kind, value in sorted(measured.items())
        },
        "routed_amount_by_currency": {
            currency: str(amount) for currency, amount in sorted(routed_amounts.items())
        },
        "origins": sorted(origins),
        "destinations": sorted(destinations),
        "reason_bases": reason_bases,
        "sentiment": {"records": sentiment_count, "total": str(sentiment_total)},
    }


def weighted_direction(records, as_of, window_seconds=WINDOW_SECONDS_DEFAULT):
    if type(window_seconds) is not int or not 1 <= window_seconds <= WINDOW_SECONDS_MAX:
        raise ValueError(f"window seconds must be an integer from 1 to {WINDOW_SECONDS_MAX}")
    lower = as_of - timedelta(seconds=window_seconds)
    score = 0
    scored = 0
    kinds: dict[str, int] = {}
    for observed, available, row in records:
        if not lower <= observed <= as_of or available > as_of:
            continue
        weight = DIRECTION_WEIGHTS.get(row["kind"], 0)
        signal = _signal(row)
        if not weight or not signal:
            continue
        delta = as_of - observed
        age = delta.days * 86400 + delta.seconds
        bucket = min(RECENCY_BUCKETS - 1, age * RECENCY_BUCKETS // window_seconds)
        score += signal * weight * (RECENCY_BUCKETS - bucket)
        scored += 1
        kinds[row["kind"]] = kinds.get(row["kind"], 0) + 1
    return {
        "method": WEIGHTED_SCORING_METHOD,
        "weights": {kind: DIRECTION_WEIGHTS[kind] for kind in sorted(DIRECTION_WEIGHTS)},
        "recency_buckets": RECENCY_BUCKETS,
        "window_seconds": window_seconds,
        "scored_records": scored,
        "scored_kinds": dict(sorted(kinds.items())),
        "weighted_score": score,
        "net_direction": _direction(score),
        "direction_convention": "positive measurement means upward pressure; fixed pre-registered "
                                 "kind weights and recency multipliers, not fitted to any sample",
    }


def observation_indicator(records, as_of, window_seconds=WINDOW_SECONDS_DEFAULT):
    if type(window_seconds) is not int or not 1 <= window_seconds <= WINDOW_SECONDS_MAX:
        raise ValueError(f"window seconds must be an integer from 1 to {WINDOW_SECONDS_MAX}")
    lower = as_of - timedelta(seconds=window_seconds)
    score = 0
    scored = 0
    kinds: dict[str, int] = {}
    for observed, available, row in records:
        if not lower <= observed <= as_of or available > as_of:
            continue
        weight = OBSERVATION_INDICATOR_WEIGHTS.get(row["kind"], 0)
        signal = _signal(row)
        if not weight or not signal:
            continue
        delta = as_of - observed
        age = delta.days * 86400 + delta.seconds
        bucket = min(RECENCY_BUCKETS - 1, age * RECENCY_BUCKETS // window_seconds)
        score += signal * weight * (RECENCY_BUCKETS - bucket)
        scored += 1
        kinds[row["kind"]] = kinds.get(row["kind"], 0) + 1
    return {
        "method": "observation_indicator_v1",
        "weights": {kind: OBSERVATION_INDICATOR_WEIGHTS[kind] for kind in sorted(OBSERVATION_INDICATOR_WEIGHTS)},
        "recency_buckets": RECENCY_BUCKETS,
        "window_seconds": window_seconds,
        "scored_records": scored,
        "scored_kinds": dict(sorted(kinds.items())),
        "weighted_score": score,
        "net_direction": _direction(score),
        "direction_convention": "fund_flow and insider_trade weight 2, macro_release/holding/filing_event weight 1; "
                                 "positive measurement means upward pressure; fixed pre-registered weights, not fitted",
    }


def _direction(change):
    if change > 0:
        return "up"
    if change < 0:
        return "down"
    return "flat"


def study(conn, entity_id, price_path, evidence_path, window_seconds=WINDOW_SECONDS_DEFAULT,
          limit=ROWS_DEFAULT):
    if type(entity_id) is not int or not 1 <= entity_id <= 2**63 - 1:
        raise ValueError("entity id must be a positive SQLite integer")
    if type(window_seconds) is not int or not 1 <= window_seconds <= WINDOW_SECONDS_MAX:
        raise ValueError(f"window seconds must be an integer from 1 to {WINDOW_SECONDS_MAX}")
    if type(limit) is not int or not 1 <= limit <= ROWS_MAX:
        raise ValueError(f"limit must be an integer from 1 to {ROWS_MAX}")
    instrument, _, observations, _, raw = projection._load(price_path)
    entity = registry.get_entity(conn, entity_id)
    if entity is None:
        raise ValueError(f"entity {entity_id} not found")
    if entity["key"] != instrument["entity_key"]:
        raise ValueError("instrument.entity_key does not match the selected registry entity")
    records, evidence_hash = events._evidence(evidence_path, instrument["entity_key"])
    prices = dict(observations)
    step = timedelta(seconds=projection.HORIZON_SECONDS)
    rows = []
    agreement = 0
    weighted_agreement = 0
    agreement_with_evidence = 0
    weighted_agreement_with_evidence = 0
    with_evidence = 0
    patterns: dict[str, dict] = {}
    with localcontext(Context(prec=128, rounding=ROUND_HALF_EVEN)):
        for as_of, price in observations:
            target = as_of + step
            if target not in prices:
                continue
            features = build_features(records, as_of, window_seconds)
            weighted = weighted_direction(records, as_of, window_seconds)
            target_price = prices[target]
            change = target_price - price
            direction = _direction(change)
            predicted = features["net_direction"]
            agrees = predicted == direction
            agreement += agrees
            weighted_agrees = weighted["net_direction"] == direction
            weighted_agreement += weighted_agrees
            if features["records"]:
                with_evidence += 1
                agreement_with_evidence += agrees
                weighted_agreement_with_evidence += weighted_agrees
            bucket = patterns.setdefault(direction, {
                "rows": 0, "sum_direction_score": 0, "sum_records": 0,
                "sum_sentiment": Decimal(0),
            })
            bucket["rows"] += 1
            bucket["sum_direction_score"] += features["direction_score"]
            bucket["sum_records"] += features["records"]
            bucket["sum_sentiment"] += Decimal(features["sentiment"]["total"])
            rows.append({
                "as_of": as_of.isoformat(),
                "target_time": target.isoformat(),
                "observed_price": str(price),
                "target_price": str(target_price),
                "change": str(change),
                "change_percent": str(change / price * 100),
                "actual_direction": direction,
                "predicted_direction": predicted,
                "direction_agreement": agrees,
                "weighted_direction": weighted["net_direction"],
                "weighted_direction_agreement": weighted_agrees,
                "weighted_score": weighted["weighted_score"],
                "features": features,
            })
    eligible = len(rows)
    for bucket in patterns.values():
        bucket["mean_direction_score"] = str(
            Decimal(bucket["sum_direction_score"]) / bucket["rows"])
        bucket["mean_records"] = str(Decimal(bucket["sum_records"]) / bucket["rows"])
        bucket["mean_sentiment"] = str(bucket["sum_sentiment"] / bucket["rows"])
        bucket["sum_sentiment"] = str(bucket["sum_sentiment"])
    return {
        "method": "world_state_evidence_v1",
        "evaluation_kind": "exploratory_historical_evidence_patterns",
        "entity": dict(entity),
        "instrument": instrument,
        "window_seconds": window_seconds,
        "horizon_seconds": projection.HORIZON_SECONDS,
        "direction_convention": "positive measurement means upward pressure, negative means downward; "
                                "an assumption of the supplied evidence, not independently verified",
        "coverage": {
            "price_points": len(observations),
            "eligible_boundaries": eligible,
            "boundaries_with_evidence": with_evidence,
            "evidence_records_supplied": len(records),
            "evidence_coverage": "unknown",
            "all_history": False,
        },
        "direction_agreement": {
            "evaluated": eligible,
            "agreements": agreement,
            "rate": str(Decimal(agreement) / eligible) if eligible else None,
        },
        "weighted_direction_agreement": {
            "evaluated": eligible,
            "agreements": weighted_agreement,
            "rate": str(Decimal(weighted_agreement) / eligible) if eligible else None,
        },
        "direction_agreement_with_evidence": {
            "evaluated": with_evidence,
            "agreements": agreement_with_evidence,
            "rate": str(Decimal(agreement_with_evidence) / with_evidence) if with_evidence else None,
        },
        "weighted_direction_agreement_with_evidence": {
            "evaluated": with_evidence,
            "agreements": weighted_agreement_with_evidence,
            "rate": (str(Decimal(weighted_agreement_with_evidence) / with_evidence)
                     if with_evidence else None),
        },
        "weighted_scoring": {
            "method": WEIGHTED_SCORING_METHOD,
            "weights": {kind: DIRECTION_WEIGHTS[kind] for kind in sorted(DIRECTION_WEIGHTS)},
            "recency_buckets": RECENCY_BUCKETS,
        },
        "patterns_by_actual_direction": patterns,
        "rows": rows[-limit:],
        "truncated": eligible > limit,
        "provenance": {
            "prices_sha256": hashlib.sha256(raw).hexdigest(),
            "evidence_sha256": evidence_hash,
            "truth_verified": False,
        },
        "limitations": [
            "Features come only from supplied evidence; the price series is used only as the scored outcome, never as a predictive input.",
            "A record enters a window only when observed inside it and available at or before the window end; later publication is excluded.",
            "Missing evidence is unknown coverage, not an absence of money movement, liabilities, events or sentiment.",
            "Directional signs and totals are taken from the supplied evidence; positive-is-upward is an assumption, not verified causation.",
            "Weighted scoring multiplies kind weights and recency-bucket factors that are fixed assumptions, not estimated from this sample.",
            "Agreement over all boundaries is diluted by boundaries with no supplied evidence, which predict flat; the with-evidence sections are the fairer comparison but remain exploratory.",
            "Historical direction agreement, weighted or not, is exploratory and not prospective evidence; overlapping windows are not independent.",
            "No model fitting, no guaranteed accuracy and no claim that the objective is met.",
        ],
    }
