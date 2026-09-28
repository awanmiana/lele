"""Price Volatility Anomaly Indicator: detects current patterns similar to past volatility instances.

Algorithm:
1. Run ZigZag to detect current volatility instances
2. Build world-state features for the current window
3. Query RAG for similar historical patterns using semantic similarity
4. Score anomaly based on similarity to past volatility instances that preceded large moves
"""
import json
import sqlite3
from contextlib import suppress
from decimal import Context, Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext

from ..core import registry
from . import episodes


DEFAULT_LOOKBACK_HOURS = 72
DEFAULT_HISTORY_DAYS = 365
MIN_SIMILARITY = 0.5
MAX_MATCHES = 10


def _cosine_similarity(vec_a, vec_b):
    """Compute cosine similarity between two feature vectors."""
    if not vec_a or not vec_b:
        return 0.0
    keys = set(vec_a.keys()) | set(vec_b.keys())
    dot = sum(vec_a.get(k, 0) * vec_b.get(k, 0) for k in keys)
    norm_a = sum(v * v for v in vec_a.values()) ** 0.5
    norm_b = sum(v * v for v in vec_b.values()) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _extract_feature_vector(features):
    """Extract a normalized feature vector from world-state features."""
    if not features:
        return {}

    vec = {}
    with localcontext(Context(prec=128, rounding=ROUND_HALF_EVEN)):
        if "direction_score" in features:
            vec["direction_score"] = float(features["direction_score"])
        if "weighted_score" in features:
            vec["weighted_score"] = float(features["weighted_score"])
        if "net_direction" in features:
            vec["net_direction"] = {"up": 1, "down": -1, "flat": 0}.get(
                features["net_direction"], 0
            )
        if "kinds" in features:
            for kind, count in features["kinds"].items():
                vec[f"kind_{kind}"] = float(count)
        if "event_records" in features:
            vec["event_records"] = float(features["event_records"])
        if "sentiment" in features:
            vec["sentiment_total"] = float(features["sentiment"].get("total", 0))
        if "measured_by_kind" in features:
            for kind, data in features["measured_by_kind"].items():
                vec[f"measured_{kind}_count"] = float(data.get("count", 0))
                with suppress(InvalidOperation, TypeError):
                    vec[f"measured_{kind}_total"] = float(Decimal(str(data.get("total", 0))))

    return vec


def _compute_feature_hash(features):
    """Compute a hash of the feature vector for storage."""
    import hashlib
    vec = _extract_feature_vector(features)
    content = json.dumps(vec, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(content.encode()).hexdigest()[:32]


def detect_volatility_instances(conn, instrument_key, price_path, evidence_path=None,
                                 threshold_percent=3, horizon_hours=72):
    """Detect volatility instances using ZigZag on price series."""
    entities = registry.find_entities(conn, q=instrument_key, limit=1)
    if not entities:
        return 0
    entity_id = entities[0]["id"]
    result = episodes.analyze(
        conn, entity_id, price_path, evidence_path,
        threshold_percent=threshold_percent,
        horizon_hours=horizon_hours,
        limit=1000,
    )

    stored = 0
    skipped = 0
    first_error = ""
    for episode in result.get("episodes", []):
        try:
            features = episode.get("precursor")
            features_hash = _compute_feature_hash(features) if features else ""

            registry.add_volatility_instance(
                conn,
                instrument_key=instrument_key,
                as_of=episode["start"],
                target_time=episode["end"],
                start_price=episode["start_price"],
                end_price=episode["end_price"],
                magnitude_percent=episode["magnitude_percent"],
                direction="up" if episode["direction"] == "up" else "down",
                episode_type="pump" if episode["direction"] == "up" else "dump",
                regime=episode.get("regime", {}).get("label", "unknown"),
                threshold_percent=threshold_percent,
                start_index=episode["start_index"],
                end_index=episode["end_index"],
                features_hash=features_hash,
                observed_at=episode["start"],
                available_at=episode["start"],
                source_url="",
                evidence=json.dumps(features, separators=(",", ":")) if features else "",
            )
            stored += 1
        except sqlite3.Error as error:
            skipped += 1
            if not first_error:
                first_error = f"{type(error).__name__}: {error}"

    if skipped:
        # A dropped instance would read as "the market did not move", which is a
        # different claim from "the record could not be stored".
        raise sqlite3.Error(f"{skipped} volatility instance(s) could not be stored; "
                            f"first failure: {first_error}")
    return stored


def find_similar_patterns(conn, current_features, instrument_key=None,
                          min_similarity=MIN_SIMILARITY, limit=MAX_MATCHES):
    """Find historical volatility instances with similar feature vectors."""
    current_vec = _extract_feature_vector(current_features)
    if not current_vec:
        return []

    history = registry.list_volatility_instances(conn, instrument_key=instrument_key, limit=1000)
    matches = []

    for vol in history:
        if vol["features_hash"]:
            hist_vec = json.loads(vol["evidence"]) if vol["evidence"] else {}
            hist_vec = _extract_feature_vector(hist_vec)
            similarity = _cosine_similarity(current_vec, hist_vec)
            if similarity >= min_similarity:
                matches.append({
                    "historical_volatility_id": vol["id"],
                    "similarity_score": str(similarity),
                    "matched_features": json.dumps(current_vec, separators=(",", ":")),
                    "matched_kinds": json.dumps(
                        list(current_features.get("kinds", {}).keys()) if current_features else [],
                        separators=(",", ":")),
                })

    matches.sort(key=lambda m: float(m["similarity_score"]), reverse=True)
    return matches[:limit]


def compute_anomaly_score(conn, entity_id, price_path, evidence_path=None,
                          threshold_percent=3, horizon_hours=72,
                          lookback_hours=DEFAULT_LOOKBACK_HOURS,
                          history_days=DEFAULT_HISTORY_DAYS):
    """Compute the price volatility anomaly indicator score.

    Returns:
        Dict with anomaly score, top matches, and breakdown
    """
    result = episodes.analyze(
        conn, entity_id, price_path, evidence_path,
        threshold_percent=threshold_percent,
        horizon_hours=horizon_hours,
        limit=1,
    )

    if not result.get("episodes"):
        return {
            "score": 0.0,
            "volatility_detected": False,
            "matches": [],
            "breakdown": {},
        }

    current_episode = result["episodes"][0]
    current_features = current_episode.get("precursor")

    if not current_features:
        return {
            "score": 0.0,
            "volatility_detected": True,
            "matches": [],
            "breakdown": {"error": "No evidence features available"},
        }

    entity = registry.get_entity(conn, entity_id)
    instrument_key = entity["key"] if entity else None

    matches = find_similar_patterns(
        conn, current_features, instrument_key,
        min_similarity=MIN_SIMILARITY, limit=MAX_MATCHES
    )

    if not matches:
        return {
            "score": 0.0,
            "volatility_detected": True,
            "matches": [],
            "breakdown": {"error": "No similar historical patterns found"},
        }

    top_match = matches[0]
    similarity = float(top_match["similarity_score"])

    current_direction = current_episode["direction"]
    hist_vol = registry.get_volatility_instance(conn, top_match["historical_volatility_id"])
    hist_magnitude = float(hist_vol.get("magnitude_percent", 0)) if hist_vol else 0.0
    historical_direction = "up" if hist_magnitude > 0 else "down"

    direction_agreement = 1.0 if current_direction == historical_direction else -0.5

    anomaly_score = round(similarity * direction_agreement * 10, 4)

    return {
        "score": anomaly_score,
        "volatility_detected": True,
        "current_episode": current_episode,
        "matches": matches,
        "breakdown": {
            "similarity": similarity,
            "direction_agreement": direction_agreement,
            "current_direction": current_direction,
            "historical_direction": historical_direction,
            "matched_volatility_id": top_match["historical_volatility_id"],
        },
    }


def volatility_anomaly_v1(conn, entity_id, price_path, evidence_path=None,
                          threshold_percent=3, horizon_hours=72,
                          observation_window_seconds=300):
    """Frozen method for prospective validation: volatility_anomaly_v1.

    Computes anomaly score based on similarity to historical volatility patterns.
    """
    entity = registry.get_entity(conn, entity_id)
    instrument_key = entity["key"] if entity else None
    result = compute_anomaly_score(
        conn, entity_id, price_path, evidence_path,
        threshold_percent=threshold_percent,
        horizon_hours=horizon_hours,
    )

    return {
        "method": "volatility_anomaly_v1",
        "instrument_key": instrument_key,
        "threshold_percent": threshold_percent,
        "horizon_hours": horizon_hours,
        "observation_window_seconds": observation_window_seconds,
        "anomaly_score": result["score"],
        "volatility_detected": result["volatility_detected"],
        "top_match": result["matches"][0] if result["matches"] else None,
        "all_matches": result["matches"],
        "breakdown": result["breakdown"],
    }


def pre_register_volatility_anomaly_v1():
    """Return the pre-registration spec for volatility_anomaly_v1."""
    return {
        "method": "volatility_anomaly_v1",
        "required_asset_classes": ["bitcoin", "gold", "oil", "stock"],
        "target_metric": "direction",
        "tolerance_bps": 100,
        "target_rate": 0.98,
        "min_settled_per_class": 1,
        "observation_window_seconds": 300,
    }
