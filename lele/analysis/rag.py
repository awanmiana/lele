"""RAG-style event, money-flow, and anomaly analysis layer.

Provides:
- store_events: persist political/regulatory/market events into event_store
- build_event_graph: create event_relationships between correlated events
- attribute_money_flows: link money flows to causal events
- detect_price_anomalies: flag windows whose move is unusual for that instrument
- event_indicator_v1: composite indicator combining event, flow, and anomaly signals
"""
import json
import sqlite3
from ..core import clock
from . import volatility
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation


def store_events(conn, events_list, source="political-event"):
    """Store a batch of events into event_store. Returns (stored, skipped)."""
    if not events_list:
        return {"stored": 0, "skipped": 0}
    stored = 0
    skipped = 0
    for ev in events_list:
        try:
            row = _validate_event(ev, source)
        except (ValueError, KeyError):
            skipped += 1
            continue
        try:
            stored_row = conn.execute(
                """INSERT OR IGNORE INTO event_store(
                     source, event_type, event_subtype, external_id, title, description,
                     actor_key, actor_name, counterparty_key, counterparty_name,
                     instrument_key, occurred_at, observed_at, severity, confidence,
                     source_url, evidence, metadata)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row["source"], row["event_type"], row.get("event_subtype", ""),
                 row["external_id"], row["title"], row.get("description", ""),
                 row["actor_key"], row.get("actor_name", ""),
                 row.get("counterparty_key", ""), row.get("counterparty_name", ""),
                 row.get("instrument_key", ""), row["occurred_at"], row["observed_at"],
                 row.get("severity", "unknown"), row.get("confidence", 0.0),
                 row.get("source_url", ""), row.get("evidence", ""),
                 json.dumps(row.get("metadata", {}), separators=(",", ":"))),
            )
            if stored_row.rowcount > 0:
                stored += 1
            else:
                skipped += 1
        except sqlite3.IntegrityError:
            skipped += 1
    return {"stored": stored, "skipped": skipped}


def _validate_event(ev, source):
    event_type = ev.get("event_type") or ev.get("kind")
    if not event_type or not str(event_type).strip():
        raise ValueError("event_type is required")
    external_id = ev.get("external_id") or ev.get("document_number") or ""
    title = ev.get("title", "")
    if not title.strip():
        raise ValueError("title is required")
    occurred_at = ev.get("occurred_at") or ev.get("publication_date") or ""
    if not occurred_at:
        raise ValueError("occurred_at is required")
    return {
        "source": source,
        "event_type": event_type,
        "event_subtype": ev.get("event_subtype", ev.get("subtype", "")),
        "external_id": external_id,
        "title": title,
        "description": ev.get("description", ev.get("abstract", "")),
        "actor_key": ev.get("actor_key", ev.get("actor", "")),
        "actor_name": ev.get("actor_name", ev.get("government_entity", "")),
        "counterparty_key": ev.get("counterparty_key", ""),
        "counterparty_name": ev.get("counterparty_name", ""),
        "instrument_key": ev.get("instrument_key", ""),
        "occurred_at": occurred_at,
        "observed_at": ev.get("observed_at", ev.get("retrieved_at", "")),
        "severity": ev.get("severity", "unknown"),
        "confidence": float(ev.get("confidence", 0.0)),
        "source_url": ev.get("source_url", ev.get("html_url", "")),
        "evidence": json.dumps(ev.get("evidence", {}), separators=(",", ":")) if isinstance(ev.get("evidence"), dict) else str(ev.get("evidence", "")),
        "metadata": ev.get("metadata", {}),
    }


def build_event_graph(conn, window_hours=24, min_strength=0.1):
    """Create event_relationships between events that occurred within window_hours.
    Returns the number of relationships created."""
    cutoff = (clock.now() - timedelta(hours=window_hours)).isoformat()
    rows = conn.execute(
        """SELECT a.id AS src, b.id AS dst, a.event_type AS type_a, b.event_type AS type_b,
                  a.actor_key AS actor_a, b.actor_key AS actor_b
           FROM event_store a, event_store b
           WHERE a.id < b.id AND a.occurred_at >= ? AND b.occurred_at >= ?""",
        (cutoff, cutoff),
    ).fetchall()
    created = 0
    for row in rows:
        strength = _compute_strength(row)
        if strength < min_strength:
            continue
        relationship = _infer_relationship(row["type_a"], row["type_b"])
        if relationship is None:
            continue
        try:
            inserted = conn.execute(
                """INSERT OR IGNORE INTO event_relationships(
                     source_event_id, target_event_id, relationship, strength, direction,
                     source_url, evidence)
                   VALUES(?,?,?,?,?,?,?)""",
                (row["src"], row["dst"], relationship, strength, "causal", "", ""),
            )
            if inserted.rowcount > 0:
                created += 1
        except sqlite3.IntegrityError:
            continue
    return created


def _compute_strength(row):
    actor_match = row["actor_a"] == row["actor_b"] and row["actor_a"] != ""
    type_match = row["type_a"] == row["type_b"]
    if actor_match and type_match:
        return 0.9
    if actor_match or type_match:
        return 0.5
    return 0.2


def _infer_relationship(type_a, type_b):
    event_types = {"political_event", "geopolitical_event", "regulatory_action", "market_shock",
                   "news_event", "macro_release"}
    if type_a not in event_types or type_b not in event_types:
        return None
    if "shock" in (type_a, type_b):
        return "triggers"
    if "regulatory" in (type_a, type_b) or "political" in (type_a, type_b):
        return "precedes"
    return "related_to"


def attribute_money_flows(conn, event_ids=None, window_hours=48):
    """Link money flows to causal events. Returns attribution count."""
    cutoff = (clock.now() - timedelta(hours=window_hours)).isoformat()
    if event_ids is None:
        events = conn.execute(
            "SELECT id, actor_key, occurred_at FROM event_store WHERE occurred_at >= ?",
            (cutoff,),
        ).fetchall()
    else:
        events = conn.execute(
            "SELECT id, actor_key, occurred_at FROM event_store WHERE id IN ({})".format(
                ",".join("?" for _ in event_ids)),
            event_ids,
        ).fetchall()
    if not events:
        return 0
    attribution_count = 0
    for event in events:
        event_id = event["id"]
        actor_key = event["actor_key"]
        occurred = event["occurred_at"]
        flows = conn.execute(
            """SELECT m.id AS flow_id, m.src_id, m.dst_id, m.amount, m.currency,
                      m.occurred_at AS flow_occurred_at
               FROM money_flows m
               WHERE m.occurred_at >= ? AND m.occurred_at <= ?
               ORDER BY ABS(julianday(m.occurred_at) - julianday(?))""",
            (occurred, _add_hours(occurred, window_hours), occurred),
        ).fetchall()
        for flow in flows:
            attribution_score = _compute_attribution(event, flow)
            if attribution_score < 0.1:
                continue
            try:
                inserted = conn.execute(
                    """INSERT OR IGNORE INTO money_flow_attribution(
                         event_id, flow_id, src_id, dst_id, attribution_type,
                         attribution_score, rationale)
                       VALUES(?,?,?,?,?,?,?)""",
                    (event_id, flow["flow_id"], flow["src_id"], flow["dst_id"],
                     "direct" if attribution_score > 0.5 else "indirect",
                     attribution_score,
                     f"Actor {actor_key} linked to flow {flow['flow_id']}"),
                )
                if inserted.rowcount > 0:
                    attribution_count += 1
            except sqlite3.IntegrityError:
                continue
    return attribution_count


def _compute_attribution(event, flow):
    if not event["actor_key"] or not flow["src_id"]:
        return 0.0
    time_diff_hours = abs(_hours_diff(event["occurred_at"], flow["flow_occurred_at"]))
    if time_diff_hours > 48:
        return 0.0
    if time_diff_hours <= 1:
        return 0.9
    if time_diff_hours <= 6:
        return 0.6
    if time_diff_hours <= 24:
        return 0.3
    return 0.1


def _add_hours(iso_str, hours):
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    return (dt + timedelta(hours=hours)).isoformat()


def _hours_diff(iso_a, iso_b):
    dt_a = datetime.fromisoformat(iso_a.replace("Z", "+00:00"))
    dt_b = datetime.fromisoformat(iso_b.replace("Z", "+00:00"))
    return abs((dt_a - dt_b).total_seconds() / 3600)


def detect_price_anomalies(conn, instrument_key, interval_seconds, *, mode="percentile",
                           level="95", window_bars=24, baseline_bars=120, store=True):
    """Scan stored bars for windows whose move is unusual against this instrument.

    This used to read `observations` rows whose `source` was `'price'`. No fetcher
    has ever written one, so the function could only ever return an empty list and
    `rag detect-anomalies` always reported zero anomalies -- a confident answer
    from a query that could not match. It now reads `price_bars`, which is where
    prices actually live.

    Two properties the old version got wrong and this one does not:

    * The baseline excludes the window being judged. Scoring an observation against
      a mean and spread that contain it deflates the spread and compresses the
      score toward zero, so a threshold quietly stops firing.
    * Prices are compared as *returns* over a window, not as levels. A z-score of
      a price level is not a statement about unusual movement; prices are not
      stationary, so a rising asset is "anomalous" forever.
    """
    volatility.validate_threshold(mode, level)
    if type(window_bars) is not int or not volatility.MIN_WINDOW_BARS <= window_bars <= 2000:
        raise ValueError(f"window bars must be an integer from {volatility.MIN_WINDOW_BARS} "
                         "to 2000")
    if type(baseline_bars) is not int or not 1 <= baseline_bars <= 10000:
        raise ValueError("baseline bars must be an integer from 1 to 10000")
    bars = volatility.series(conn, instrument_key, interval_seconds)
    closes = bars["closes"]
    total = len(closes)
    result = {
        "instrument_key": instrument_key,
        "interval_seconds": interval_seconds,
        "parameters": {"mode": mode, "level": level, "window_bars": window_bars,
                       "baseline_bars": baseline_bars},
        "bars_available": total,
        "windows_examined": 0,
        "windows_skipped": 0,
        "anomalies": [],
        "anomaly_type": "price_move",
        "coverage": {
            "cadence_seconds": bars["cadence_seconds"],
            "gaps": len(bars["gaps"]),
            "first_bar": bars["times"][0] if bars["times"] else None,
            "last_bar": bars["times"][-1] if bars["times"] else None,
            "completeness": volatility.UNKNOWN,
        },
    }
    needed = window_bars + baseline_bars
    if total < needed:
        result["status"] = "insufficient_bars"
        result["reason"] = (f"{needed} bars are needed for a {window_bars} bar window judged "
                            f"against a {baseline_bars} bar baseline, and {total} are stored")
        result["skipped_reasons"] = {"insufficient_bars": result["reason"]}
        return result
    result["status"] = "ok"
    opens, highs, lows = bars["opens"], bars["highs"], bars["lows"]
    skipped: dict[str, int] = {}
    start = window_bars
    while start + window_bars <= total:
        end = start + window_bars
        result["windows_examined"] += 1
        if any(index in bars["gaps"] for index in range(start + 1, end)):
            skipped["window_spans_gap"] = skipped.get("window_spans_gap", 0) + 1
            result["windows_skipped"] += 1
            start += 1
            continue
        change = volatility.percent_change(opens, highs, lows, closes, start, end - 1)
        if change is None:
            skipped["unusable_window"] = skipped.get("unusable_window", 0) + 1
            result["windows_skipped"] += 1
            start += 1
            continue
        baseline: list[Decimal] = []
        cursor = start
        while cursor > 0 and len(baseline) < baseline_bars:
            cursor -= 1
            value = volatility.percent_change(opens, highs, lows, closes,
                                              max(0, cursor - window_bars), cursor)
            if value is not None:
                baseline.append(value)
        verdict = volatility.evaluate_threshold(baseline, change, mode=mode, level=level)
        if verdict["breached"] is None:
            skipped["baseline_too_short"] = skipped.get("baseline_too_short", 0) + 1
        elif verdict["breached"]:
            record = _record_anomaly(
                conn, instrument_key=instrument_key, interval_seconds=interval_seconds,
                observed_at=bars["times"][end - 1], direction="up" if change > 0 else "down",
                change=change, verdict=verdict, window_bars=window_bars,
                window_start=bars["times"][start], store=store)
            result["anomalies"].append(record)
        start += 1
    if skipped:
        result["skipped_reasons"] = dict(sorted(skipped.items()))
    return result


def _percentile_number(score):
    """The percentile as a Decimal, from either text or a number.

    Reported values are text so the report survives `json.dumps`, so anything that
    compares against one has to parse it back rather than assume a float.
    """
    if score is None or score == "":
        return None
    try:
        value = Decimal(str(score))
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def _severity(score, breached: bool) -> str:
    """Severity from the percentile actually observed, not from the level asked for.

    A window that clears a 95th-percentile rule is 'moderate' even when 95 is a
    very low threshold, and 'critical' when it is beyond the 99.5th. Severity that
    tracked the configured level instead would make a permissive rule look alarming.
    """
    value = _percentile_number(score)
    if not breached or value is None:
        return "unknown"
    if value >= Decimal("99.5"):
        return "critical"
    if value >= Decimal("99"):
        return "high"
    if value >= Decimal("95"):
        return "moderate"
    return "low"


def _record_anomaly(conn, *, instrument_key, interval_seconds, observed_at, direction,
                    change, verdict, window_bars, window_start, store):
    score = verdict.get("baseline_percentile")
    record = {
        "instrument_key": instrument_key,
        "interval_seconds": interval_seconds,
        "anomaly_type": "price_move",
        "observed_at": observed_at,
        "window_start": window_start,
        "window_bars": window_bars,
        "direction": direction,
        "change_percent": str(verdict["value"]),
        "severity": _severity(score, bool(verdict["breached"])),
        "score": str(score) if score is not None else "",
        "z_score": str(verdict["z_score"]) if verdict["z_score"] is not None else "",
        "baseline_bars": verdict["baseline_bars"],
        "baseline_percentile": str(score) if score is not None else "",
        "rule": {"mode": verdict["mode"], "level": verdict["level"]},
        "stored": False,
    }
    if not store:
        return record
    payload = json.dumps({
        "rule": record["rule"], "direction": direction, "window_bars": window_bars,
        "window_start": window_start, "baseline_bars": verdict["baseline_bars"],
        "z_score": record["z_score"], "interval_seconds": interval_seconds,
        "basis": "threshold rule against this instrument's own trailing distribution",
        "not_a_cause": True,
    }, sort_keys=True, separators=(",", ":"))
    numeric = _percentile_number(score)
    before = conn.total_changes
    conn.execute(
        """INSERT OR IGNORE INTO price_anomalies(
             instrument_key, anomaly_type, observed_at, occurred_at, severity, score,
             description, evidence, source_url)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (instrument_key, "price_move", observed_at, observed_at, record["severity"],
         float(numeric) if numeric is not None else 0.0,
         f"{direction} move of {verdict['value']}% over {window_bars} bars, at the "
         f"{score if score not in (None, '') else 'unranked'}th percentile of its own "
         f"trailing distribution of {verdict['baseline_bars']} observations",
         payload, ""))
    record["stored"] = conn.total_changes > before
    return record


def detect_anomalies(conn, *, interval_seconds=86400, mode="percentile", level="95",
                     window_bars=24, baseline_bars=120, instrument_key="", store=True):
    """Scan every instrument that actually has stored bars.

    The instrument list comes from `price_bars` rather than from
    `observations.source = 'price'`, which nothing has ever written, so the old
    version had nothing to iterate over and always reported zero.
    """
    if instrument_key:
        keys = [instrument_key]
    else:
        rows = conn.execute(
            """SELECT DISTINCT instrument_key, interval_seconds FROM price_bars
               WHERE instrument_key != '' ORDER BY instrument_key""").fetchall()
        keys = [(row["instrument_key"], row["interval_seconds"]) for row in rows]
    reports = []
    total = 0
    for entry in keys:
        if isinstance(entry, tuple):
            key, interval = entry
        else:
            key, interval = entry, interval_seconds
        report = detect_price_anomalies(
            conn, key, interval, mode=mode, level=level, window_bars=window_bars,
            baseline_bars=baseline_bars, store=store)
        reports.append(report)
        total += len(report["anomalies"])
    return {"scanned": len(reports), "anomalies": total, "reports": reports,
            "parameters": {"interval_seconds": interval_seconds, "mode": mode,
                           "level": level, "window_bars": window_bars,
                           "baseline_bars": baseline_bars}}



def event_indicator_v1(conn, instrument_key=None, limit=20):
    """Composite indicator combining event, flow, and anomaly signals.

    Returns a dict with:
    - score: weighted composite score
    - event_count: number of relevant events
    - flow_count: number of attributed money flows
    - anomaly_count: number of detected anomalies
    - breakdown: dict of scores by category
    """
    event_score = 0.0
    flow_score = 0.0
    anomaly_score = 0.0
    event_count = 0
    flow_count = 0
    anomaly_count = 0

    cutoff = (clock.now() - timedelta(days=30)).isoformat()

    query = """SELECT id, event_type AS kind, severity, confidence FROM event_store WHERE occurred_at >= ?"""
    params = [cutoff]
    if instrument_key:
        query += " AND instrument_key = ?"
        params.append(instrument_key)
    events = conn.execute(query, params).fetchall()
    for ev in events:
        weight = {"critical": 3, "high": 2, "moderate": 1, "low": 0.5, "unknown": 0.2}.get(
            ev["severity"], 0.2) * (ev["confidence"] or 0.5)
        event_score += weight
        event_count += 1

    if instrument_key:
        flows = conn.execute(
            """SELECT m.amount, m.flow_type FROM money_flows m
               JOIN money_flow_attribution mfa ON mfa.flow_id = m.id
               JOIN event_store es ON es.id = mfa.event_id
               WHERE es.instrument_key = ? AND es.occurred_at >= ?""",
            (instrument_key, cutoff),
        ).fetchall()
    else:
        flows = conn.execute(
            """SELECT m.amount, m.flow_type FROM money_flows m
               JOIN money_flow_attribution mfa ON mfa.flow_id = m.id
               WHERE mfa.created_at >= ?""",
            (cutoff,),
        ).fetchall()
    for flow in flows:
        try:
            amount = Decimal(str(flow["amount"]))
            flow_score += float(amount) * (3 if flow["flow_type"] == "large" else 1)
        except (InvalidOperation, ValueError, TypeError):
            flow_score += 1
        flow_count += 1

    if instrument_key:
        anomalies = conn.execute(
            "SELECT score, severity FROM price_anomalies WHERE instrument_key = ? AND occurred_at >= ?",
            (instrument_key, cutoff),
        ).fetchall()
    else:
        anomalies = conn.execute(
            "SELECT score, severity FROM price_anomalies WHERE occurred_at >= ?",
            (cutoff,),
        ).fetchall()
    for anom in anomalies:
        anomaly_score += anom["score"] * {"critical": 3, "high": 2, "moderate": 1}.get(
            anom["severity"], 0.5)
        anomaly_count += 1

    total = event_score + flow_score + anomaly_score
    score = round(min(total / max(1, (event_count + flow_count + anomaly_count) * 2), 10.0), 4)

    return {
        "score": score,
        "event_count": event_count,
        "flow_count": flow_count,
        "anomaly_count": anomaly_count,
        "breakdown": {
            "event_score": round(event_score, 4),
            "flow_score": round(flow_score, 4),
            "anomaly_score": round(anomaly_score, 4),
        },
    }
