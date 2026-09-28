import hashlib
import json
import math
import os
import re
import tempfile
from ..core import clock
from datetime import datetime, timedelta, UTC
from decimal import Context, Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext

from ..core import importer, registry
from . import comparison, events, projection, volatility_anomaly, worldstate

MAX_LEDGER_BYTES = 16 * 1024 * 1024
MAX_LEDGER_RECORDS = 200_000
HISTORY_POINTS = 3
RECORD_KINDS = ("run", "forecast", "settlement")
TARGET_METRICS = ("combined", "direction", "tolerance", "exact")
EVIDENCE_METHOD = "evidence_score_10bps_v1"
EVIDENCE_METHOD_V2 = "evidence_score_10bps_v2"
OBSERVATION_INDICATOR_METHOD = "observation_indicator_v1"
VOLATILITY_ANOMALY_METHOD = "volatility_anomaly_v1"
CONSTRUCTED_INDICATOR_METHOD = "constructed_indicator_v1"
CONSTRUCTED_INDICATOR_WEIGHTED_METHOD = "constructed_indicator_weighted_v1"
EVIDENCE_METHODS = (EVIDENCE_METHOD, EVIDENCE_METHOD_V2, OBSERVATION_INDICATOR_METHOD, VOLATILITY_ANOMALY_METHOD,
                    CONSTRUCTED_INDICATOR_METHOD, CONSTRUCTED_INDICATOR_WEIGHTED_METHOD)
EVIDENCE_STEP_BPS = 10
EVIDENCE_WINDOW_SECONDS = 300
METHODS = (*comparison.METHODS, *EVIDENCE_METHODS)
METRIC_HIT_FIELDS = {
    "combined": "combined_hit",
    "direction": "direction_hit",
    "tolerance": "tolerance_hit",
    "exact": "exact_hit",
}
CONFIG_FIELDS = {
    "method", "required_asset_classes", "target_metric", "tolerance_bps",
    "target_rate", "min_settled_per_class",
}
CONFIG_OPTIONAL = {"notes", "observation_window_seconds"}


def _now():
    return clock.now()


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def _hash(body):
    return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


def _rate(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("target_rate must be a decimal number or string")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("target_rate must be finite")
    text = value if isinstance(value, str) else str(value)
    importer._text(text, "target_rate", 64, True)
    if not projection._PRICE_RE.fullmatch(text):
        raise ValueError("target_rate must be a positive decimal")
    try:
        rate = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError("target_rate must be a positive decimal") from exc
    if not rate.is_finite() or not 0 < rate <= 1:
        raise ValueError("target_rate must be greater than 0 and at most 1")
    return rate


def _validate_config(value):
    importer._object(value, CONFIG_FIELDS | CONFIG_OPTIONAL, "config")
    missing = CONFIG_FIELDS - value.keys()
    if missing:
        raise ValueError("config requires method, required_asset_classes, target_metric, "
                         "tolerance_bps, target_rate and min_settled_per_class")
    method = value["method"]
    if method not in METHODS:
        raise ValueError("config.method is not a supported frozen method")
    classes = importer._array(value["required_asset_classes"], len(projection.ASSET_CLASSES),
                              "required_asset_classes")
    if (not classes or len(set(classes)) != len(classes)
            or any(cls not in projection.ASSET_CLASSES for cls in classes)):
        raise ValueError("required_asset_classes must be 1..5 unique supported asset classes")
    metric = value["target_metric"]
    if metric not in TARGET_METRICS:
        raise ValueError("config.target_metric is not supported")
    tolerance = value["tolerance_bps"]
    if type(tolerance) is not int or not 0 <= tolerance <= 10000:
        raise ValueError("config.tolerance_bps must be an integer from 0 to 10000")
    rate = _rate(value["target_rate"])
    minimum = value["min_settled_per_class"]
    if type(minimum) is not int or not 1 <= minimum <= 1_000_000:
        raise ValueError("config.min_settled_per_class must be an integer from 1 to 1000000")
    config = {
        "method": method,
        "required_asset_classes": list(classes),
        "target_metric": metric,
        "tolerance_bps": tolerance,
        "target_rate": str(rate),
        "min_settled_per_class": minimum,
    }
    notes = value.get("notes")
    if notes is not None:
        importer._text(notes, "config.notes", 4096)
        config["notes"] = notes
    window = value.get("observation_window_seconds")
    if window is not None:
        if type(window) is not int or not 1 <= window <= worldstate.WINDOW_SECONDS_MAX:
            raise ValueError("config.observation_window_seconds must be an integer from 1 to "
                             f"{worldstate.WINDOW_SECONDS_MAX}")
        config["observation_window_seconds"] = window
    return config


def _read_ledger(path):
    with open(path, "rb") as stream:
        raw = stream.read(MAX_LEDGER_BYTES + 1)
    if len(raw) > MAX_LEDGER_BYTES:
        raise ValueError("ledger exceeds 16 MiB")
    records: list[dict] = []
    previous = ""
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line.decode("utf-8"), object_pairs_hook=importer._pairs,
                                parse_constant=importer._constant)
        except (UnicodeError, RecursionError) as exc:
            raise ValueError("ledger line is not valid UTF-8 JSON") from exc
        if not isinstance(record, dict) or record.get("record") not in RECORD_KINDS:
            raise ValueError("ledger record is malformed")
        stored = record.get("hash")
        if not isinstance(stored, str) or not re.fullmatch(r"[0-9a-f]{64}", stored):
            raise ValueError("ledger record hash is malformed")
        body = {key: item for key, item in record.items() if key != "hash"}
        if body.get("seq") != len(records) or body.get("prev") != previous:
            raise ValueError("ledger sequence or hash chain is invalid")
        if _hash(body) != stored:
            raise ValueError("ledger record hash mismatch")
        previous = stored
        records.append(record)
    if len(records) > MAX_LEDGER_RECORDS:
        raise ValueError("ledger exceeds the record limit")
    return records, raw


def _append_many(path, bodies):
    records, raw = _read_ledger(path)
    if len(records) + len(bodies) > MAX_LEDGER_RECORDS:
        raise ValueError("ledger exceeds the record limit")
    previous = records[-1]["hash"] if records else ""
    lines: list[str] = []
    for body in bodies:
        record = dict(body)
        record["seq"] = len(records) + len(lines)
        record["prev"] = previous
        record["hash"] = _hash(record)
        previous = record["hash"]
        lines.append(_canonical(record))
    blob = ("\n".join(lines) + "\n").encode("utf-8")
    if len(raw) + len(blob) > MAX_LEDGER_BYTES:
        raise ValueError("ledger exceeds 16 MiB")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, blob)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return [json.loads(line) for line in lines]


def _publish(path, record, force):
    parent = os.path.dirname(os.path.abspath(path)) or "."
    if not os.path.isdir(parent):
        raise ValueError("ledger parent directory must already exist")
    if os.path.isdir(path):
        raise ValueError("ledger path must be a file")
    if os.path.lexists(path) and not force:
        raise ValueError("ledger already exists; use --force to replace it")
    if not force and not hasattr(os, "link"):
        raise ValueError("atomic no-overwrite publication is unavailable on this runtime")
    line = (_canonical(record) + "\n").encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(dir=parent, prefix=".lele-ledger-")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())
        if force:
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path)
            except FileExistsError:
                raise ValueError("ledger already exists; use --force to replace it") from None
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return record


def _run_record(records, run_id=None):
    runs = [record for record in records if record.get("record") == "run"]
    if not runs:
        raise ValueError("ledger has no pre-registration run")
    if run_id is None:
        return runs[-1]
    for record in runs:
        if record.get("run_id") == run_id:
            return record
    raise ValueError("run not found in ledger")


def _forecast_price(method, history):
    if method == comparison.METHODS[0]:
        return history[-1]
    if method == comparison.METHODS[1]:
        return 2 * history[-1] - history[-2]
    return sum(history[-3:]) / 3


def _direction(change):
    if change > 0:
        return "up"
    if change < 0:
        return "down"
    return "flat"


def _outcomes(reference, forecast_price, actual, tolerance_bps):
    with localcontext(Context(prec=128, rounding=ROUND_HALF_EVEN)):
        error = abs(forecast_price - actual)
        relative = error / reference
        predicted = _direction(forecast_price - reference)
        observed = _direction(actual - reference)
    return {
        "predicted_direction": predicted,
        "actual_direction": observed,
        "direction_hit": predicted == observed,
        "absolute_error": str(error),
        "relative_error": str(relative),
        "tolerance_bps": tolerance_bps,
        "tolerance_hit": relative * 10000 <= tolerance_bps,
        "exact_hit": forecast_price == actual,
        "combined_hit": predicted == observed and relative * 10000 <= tolerance_bps,
    }


def _require_run_config(records, run_id=None):
    run = _run_record(records, run_id)
    config = _validate_config(run["config"])
    return run, config


def preregister(config_path, ledger_path, force=False, now=None):
    with open(config_path, "rb") as stream:
        raw = stream.read(projection.MAX_FILE_BYTES + 1)
    if len(raw) > projection.MAX_FILE_BYTES:
        raise ValueError("config file exceeds 2 MiB")
    try:
        payload = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=importer._pairs,
                             parse_constant=importer._constant)
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("config file is not valid UTF-8 JSON") from exc
    config = _validate_config(payload)
    created_at = (now or _now()).astimezone(UTC).isoformat()
    run_id = _hash({"config": config, "created_at": created_at})
    record = {"record": "run", "run_id": run_id, "created_at": created_at, "config": config,
              "seq": 0, "prev": ""}
    record["hash"] = _hash(record)
    _publish(ledger_path, record, force)
    return {
        "action": "preregister",
        "run_id": run_id,
        "created_at": created_at,
        "config": config,
        "ledger": str(ledger_path),
        "records": 1,
        "ledger_sha256": hashlib.sha256((_canonical(record) + "\n").encode("utf-8")).hexdigest(),
        "limitations": _ledger_limitations(),
    }


def forecast(conn, entity_id, price_path, ledger_path, now=None, evidence_path=None):
    if type(entity_id) is not int or not 1 <= entity_id <= 2**63 - 1:
        raise ValueError("entity id must be a positive SQLite integer")
    records, _ = _read_ledger(ledger_path)
    run, config = _require_run_config(records)
    instrument, _, observations, target_time, price_raw = projection._load(price_path)
    entity = registry.get_entity(conn, entity_id)
    if entity is None:
        raise ValueError(f"entity {entity_id} not found")
    if entity["key"] != instrument["entity_key"]:
        raise ValueError("instrument.entity_key does not match the selected registry entity")
    issued_at = (now or _now()).astimezone(UTC)
    as_of = observations[-1][0].isoformat()
    extra: dict = {}
    if config["method"] in (EVIDENCE_METHOD, EVIDENCE_METHOD_V2, OBSERVATION_INDICATOR_METHOD, VOLATILITY_ANOMALY_METHOD):
        if evidence_path is None:
            raise ValueError("the evidence method requires an evidence file")
        evidence_records, evidence_hash = events._evidence(evidence_path, instrument["entity_key"])
        window_seconds = config.get("observation_window_seconds", EVIDENCE_WINDOW_SECONDS)
        features = worldstate.build_features(evidence_records, observations[-1][0], window_seconds)
        extra = {"evidence_file_sha256": evidence_hash, "world_state": features,
                  "evidence_step_bps": EVIDENCE_STEP_BPS}
        if config["method"] == EVIDENCE_METHOD:
            net_direction = features["net_direction"]
        elif config["method"] == OBSERVATION_INDICATOR_METHOD:
            scoring = worldstate.observation_indicator(evidence_records,
                                                       observations[-1][0],
                                                       window_seconds)
            net_direction = scoring["net_direction"]
            extra["evidence_scoring"] = scoring
        elif config["method"] == VOLATILITY_ANOMALY_METHOD:
            result = volatility_anomaly.volatility_anomaly_v1(
                conn, entity_id, price_path, evidence_path,
                threshold_percent=3, horizon_hours=72,
                observation_window_seconds=window_seconds)
            net_direction = result["breakdown"].get("current_direction", "flat")
            extra["volatility_anomaly"] = result
        else:
            scoring = worldstate.weighted_direction(evidence_records, observations[-1][0],
                                                   window_seconds)
            net_direction = scoring["net_direction"]
            extra["evidence_scoring"] = scoring
        sign = {"up": 1, "down": -1, "flat": 0}[net_direction]
        with localcontext(Context(prec=128, rounding=ROUND_HALF_EVEN)):
            price = observations[-1][1] * (1 + Decimal(sign * EVIDENCE_STEP_BPS) / 10000)
    elif config["method"] in (CONSTRUCTED_INDICATOR_METHOD, CONSTRUCTED_INDICATOR_WEIGHTED_METHOD):
        # Constructed indicator methods: compute from wired observations at forecast time
        if evidence_path is not None:
            raise ValueError("constructed indicator methods do not use an evidence file")
        if config["method"] == CONSTRUCTED_INDICATOR_METHOD:
            net_direction = _compute_constructed_indicator_direction(conn, instrument["entity_key"])
            extra["constructed_indicator"] = {"direction": net_direction}
        else:
            net_direction = _compute_weighted_constructed_indicator_direction(conn, instrument["entity_key"])
            extra["constructed_indicator_weighted"] = {"direction": net_direction}
        sign = {"up": 1, "down": -1, "flat": 0}[net_direction]
        with localcontext(Context(prec=128, rounding=ROUND_HALF_EVEN)):
            price = observations[-1][1] * (1 + Decimal(sign * EVIDENCE_STEP_BPS) / 10000)
    else:
        if evidence_path is not None:
            raise ValueError("evidence is only used by evidence methods")
        if len(observations) < HISTORY_POINTS:
            raise ValueError("forecast requires at least three recorded observations")
        window = observations[-HISTORY_POINTS:]
        if any(right[0] - left[0] != timedelta(seconds=projection.HORIZON_SECONDS)
               for left, right in zip(window, window[1:])):
            raise ValueError("forecast history has a gap; observations are never bridged")
        with localcontext(Context(prec=128, rounding=ROUND_HALF_EVEN)):
            price = _forecast_price(config["method"], [point[1] for point in window])
    identity = _hash({"run_id": run["run_id"], "entity_id": entity_id,
                      "as_of": as_of, "method": config["method"]})
    for record in records:
        if (record.get("record") == "forecast" and record.get("run_id") == run["run_id"]
                and record.get("entity_id") == entity_id and record.get("as_of") == as_of
                and record.get("method") == config["method"]):
            return {"action": "forecast", "appended": False, "run_id": run["run_id"],
                    "forecast": record, "ledger": str(ledger_path),
                    "limitations": _ledger_limitations()}
    body = {
        "record": "forecast",
        "run_id": run["run_id"],
        "forecast_id": identity,
        "entity_id": entity_id,
        "instrument": instrument,
        "method": config["method"],
        "issued_at": issued_at.isoformat(),
        "as_of": as_of,
        "target_time": target_time.isoformat(),
        "last_price": str(observations[-1][1]),
        "forecast_price": str(price),
        "status": "prospective" if target_time > issued_at else "backfilled",
        "price_file_sha256": hashlib.sha256(price_raw).hexdigest(),
    }
    body.update(extra)
    appended = _append_many(ledger_path, [body])[0]
    return {"action": "forecast", "appended": True, "run_id": run["run_id"],
            "forecast": appended, "ledger": str(ledger_path),
            "limitations": _ledger_limitations()}


def settle(conn, entity_id, price_path, ledger_path, now=None):
    if type(entity_id) is not int or not 1 <= entity_id <= 2**63 - 1:
        raise ValueError("entity id must be a positive SQLite integer")
    records, _ = _read_ledger(ledger_path)
    run, config = _require_run_config(records)
    instrument, _, observations, _, price_raw = projection._load(price_path)
    entity = registry.get_entity(conn, entity_id)
    if entity is None:
        raise ValueError(f"entity {entity_id} not found")
    if entity["key"] != instrument["entity_key"]:
        raise ValueError("instrument.entity_key does not match the selected registry entity")
    prices = dict(observations)
    settled_ids = {record.get("forecast_id") for record in records
                   if record.get("record") == "settlement" and record.get("run_id") == run["run_id"]}
    bodies, pending = [], 0
    settled_at = (now or _now()).astimezone(UTC).isoformat()
    file_hash = hashlib.sha256(price_raw).hexdigest()
    for record in records:
        if (record.get("record") != "forecast" or record.get("run_id") != run["run_id"]
                or record.get("entity_id") != entity_id):
            continue
        if record.get("forecast_id") in settled_ids:
            continue
        target = datetime.fromisoformat(record["target_time"])
        actual = prices.get(target)
        if actual is None:
            pending += 1
            continue
        reference = Decimal(record["last_price"])
        forecast_price = Decimal(record["forecast_price"])
        bodies.append({
            "record": "settlement",
            "run_id": run["run_id"],
            "forecast_id": record["forecast_id"],
            "settled_at": settled_at,
            "observed_at": target.isoformat(),
            "actual_price": str(actual),
            "outcomes": _outcomes(reference, forecast_price, actual, config["tolerance_bps"]),
            "price_file_sha256": file_hash,
        })
    appended = _append_many(ledger_path, bodies) if bodies else []
    return {"action": "settle", "run_id": run["run_id"], "settled": len(appended),
            "pending": pending, "settlements": appended, "ledger": str(ledger_path),
            "limitations": _ledger_limitations()}


def _aggregate(rows, target_metric):
    count = len(rows)
    field = METRIC_HIT_FIELDS[target_metric]
    hits = sum(1 for row in rows if row["outcomes"][field])
    units = sorted({row["unit"] for row in rows})
    with localcontext(Context(prec=128, rounding=ROUND_HALF_EVEN)):
        total_error = sum((row["absolute_error"] for row in rows), Decimal(0))
        total_relative = sum((row["relative_error"] for row in rows), Decimal(0))
        return {
            "settled": count,
            "direction_hits": sum(1 for row in rows if row["outcomes"]["direction_hit"]),
            "tolerance_hits": sum(1 for row in rows if row["outcomes"]["tolerance_hit"]),
            "exact_hits": sum(1 for row in rows if row["outcomes"]["exact_hit"]),
            "combined_hits": sum(1 for row in rows if row["outcomes"]["combined_hit"]),
            "target_metric": target_metric,
            "target_hits": hits,
            "rate": str(Decimal(hits) / count) if count else None,
            "mae": str(total_error / count) if count and len(units) == 1 else None,
            "mae_units": units if count and len(units) == 1 else None,
            "mean_relative_error": str(total_relative / count) if count else None,
        }


def score(ledger_path, run_id=None):
    records, raw = _read_ledger(ledger_path)
    run, config = _require_run_config(records, run_id)
    forecasts = [record for record in records
                 if record.get("record") == "forecast" and record.get("run_id") == run["run_id"]]
    settlement_by_id = {record["forecast_id"]: record for record in records
                        if record.get("record") == "settlement" and record.get("run_id") == run["run_id"]}
    prospective = [record for record in forecasts if record.get("status") == "prospective"]
    rows = []
    for record in prospective:
        settlement = settlement_by_id.get(record.get("forecast_id"))
        if settlement is None:
            continue
        outcomes = settlement["outcomes"]
        rows.append({
            "asset_class": record["instrument"]["asset_class"],
            "unit": record["instrument"]["unit"],
            "outcomes": outcomes,
            "absolute_error": Decimal(outcomes["absolute_error"]),
            "relative_error": Decimal(outcomes["relative_error"]),
        })
    target_metric = config["target_metric"]
    target_rate = Decimal(config["target_rate"])
    minimum = config["min_settled_per_class"]
    overall = _aggregate(rows, target_metric)
    by_asset_class = {
        cls: _aggregate([row for row in rows if row["asset_class"] == cls], target_metric)
        for cls in sorted({row["asset_class"] for row in rows})
    }
    required = {
        cls: _aggregate([row for row in rows if row["asset_class"] == cls], target_metric)
        for cls in config["required_asset_classes"]
    }
    classes_ok = all(
        section["settled"] >= minimum and section["rate"] is not None
        and Decimal(section["rate"]) >= target_rate
        for section in required.values()
    )
    overall_ok = (
        overall["settled"] >= minimum * len(required) and overall["rate"] is not None
        and Decimal(overall["rate"]) >= target_rate
    )
    target_met = classes_ok and overall_ok
    coverage = {
        "issued": len(forecasts),
        "prospective": len(prospective),
        "backfilled": len(forecasts) - len(prospective),
        "settled_prospective": len(rows),
        "pending_prospective": len(prospective) - len(rows),
        "settled_all_statuses": sum(1 for record in forecasts if record.get("forecast_id") in settlement_by_id),
        "by_asset_class": {
            cls: {
                "issued": sum(1 for record in forecasts
                              if record["instrument"]["asset_class"] == cls),
                "prospective": sum(1 for record in prospective
                                   if record["instrument"]["asset_class"] == cls),
                "settled": sum(1 for row in rows if row["asset_class"] == cls),
            }
            for cls in sorted({record["instrument"]["asset_class"] for record in forecasts})
        },
    }
    return {
        "method": "prospective_ledger_evaluation_v1",
        "evaluation_kind": "prospective_self_recorded_ledger",
        "run_id": run["run_id"],
        "config": config,
        "coverage": coverage,
        "metrics": {
            "overall": overall,
            "by_asset_class": by_asset_class,
            "required_asset_classes": required,
        },
        "objective": {
            "status": "prospective_target_met_unverified" if target_met else "not_achieved",
            "target_metric": target_metric,
            "target_rate": config["target_rate"],
            "min_settled_per_class": minimum,
            "required_asset_classes": config["required_asset_classes"],
            "target_met": target_met,
            "reason": (
                "The self-recorded prospective ledger meets the pre-registered target, but "
                "independence, provider truth, wall-clock integrity and licensing are not verified here."
                if target_met else
                "Pre-registered prospective coverage or target rate is not yet met; no accuracy is claimed."
            ),
        },
        "provenance": {
            "ledger": str(ledger_path),
            "records": len(records),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "chain_valid": True,
            "truth_verified": False,
        },
        "limitations": _ledger_limitations(),
    }


def _compute_constructed_indicator_direction(conn, entity_key: str) -> str:
    """Compute direction from the latest constructed indicator for an entity.

    Computes all frozen indicators on-the-fly from wired observations and returns
    the direction of the most recently updated indicator with a non-neutral signal.
    """
    from lele.analysis import indicators as ind_mod

    # Need to find entity_id from entity_key
    entity = registry.entity_id_by_key(conn, entity_key)
    if not entity:
        return "flat"

    results = ind_mod.compute_all_indicators(conn, entity)

    # Find the indicator with the most recent non-neutral direction
    # For now, just use macro_release_momentum_v1 as primary (has actual data)
    for result in results:
        if result.get("indicator") == "macro_release_momentum_v1":
            direction = result.get("result", {}).get("direction", "neutral")
            if direction != "neutral":
                return "up" if direction == "positive" else "down"
        if result.get("indicator") == "flight_activity_v1":
            direction = result.get("result", {}).get("direction", "neutral")
            if direction != "neutral":
                return "up" if direction == "positive" else "down"
        if result.get("indicator") == "insider_net_buy_v1":
            direction = result.get("result", {}).get("direction", "neutral")
            if direction != "neutral":
                return "up" if direction == "positive" else "down"
        if result.get("indicator") == "filing_event_momentum_v1":
            direction = result.get("result", {}).get("direction", "neutral")
            if direction != "neutral":
                return "up" if direction == "positive" else "down"
        if result.get("indicator") == "labor_stress_v1":
            direction = result.get("result", {}).get("direction", "neutral")
            if direction != "neutral":
                return "up" if direction == "positive" else "down"
        if result.get("indicator") == "insider_cluster_v1":
            direction = result.get("result", {}).get("direction", "neutral")
            if direction != "neutral":
                return "up" if direction == "positive" else "down"

    return "flat"


def _compute_weighted_constructed_indicator_direction(conn, entity_key: str) -> str:
    """Compute direction from weighted combination of all constructed indicators.

    Weights (pre-registered, not fitted):
    - insider_net_buy_v1: 3 (direct insider pressure)
    - insider_cluster_v1: 2 (cluster signal)
    - filing_event_momentum_v1: 1 (corporate action)
    - macro_release_momentum_v1: 2 (liquidity)
    - labor_stress_v1: 1 (macro stress)
    - flight_activity_v1: 1 (real economy)
    """
    from lele.analysis import indicators as ind_mod

    weights = {
        "insider_net_buy_v1": 3,
        "insider_cluster_v1": 2,
        "filing_event_momentum_v1": 1,
        "macro_release_momentum_v1": 2,
        "labor_stress_v1": 1,
        "flight_activity_v1": 1,
    }

    entity = registry.entity_id_by_key(conn, entity_key)
    if not entity:
        return "flat"

    results = ind_mod.compute_all_indicators(conn, entity)

    total_score = 0
    total_weight = 0

    for result in results:
        indicator = result.get("indicator", "")
        if indicator in weights:
            direction = result.get("result", {}).get("direction", "neutral")
            sign = 1 if direction == "positive" else -1 if direction == "negative" else 0
            total_score += sign * weights[indicator]
            total_weight += weights[indicator]

    if total_weight == 0:
        return "flat"

    return "up" if total_score > 0 else "down" if total_score < 0 else "flat"


def _ledger_limitations():
    return [
        "The ledger is append-only and hash-chained locally; it proves internal consistency, not honest wall-clock issuance.",
        "A forecast counts as prospective only when its target time is after the recorded issuance time; later backfills are excluded from scoring.",
        "Forecast prices come from a frozen method; price methods use recorded closes, while the evidence method uses world-state evidence observed before the window end and the last close only as an anchor.",
        "Settlements require an exact observation at the target boundary; gaps stay pending and are never bridged or imputed.",
        "Direction, tolerance and exact outcomes are reported; the pre-registered target metric alone drives the objective status.",
        "A met self-recorded target stays unverified until independent, out-of-sample, multi-asset evidence and data-rights review exist.",
        "No network access, registry writes, trading advice or trade execution is performed.",
    ]
