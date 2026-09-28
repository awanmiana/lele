"""RAG-style event, money-flow, and anomaly analysis layer.

Provides:
- store_events: persist political/regulatory/market events into event_store
- build_event_graph: create event_relationships between correlated events
- attribute_money_flows: link money flows to causal events
- detect_price_anomalies: identify price volatility anomalies
- event_indicator_v1: composite indicator combining event, flow, and anomaly signals
"""
import json
import sqlite3
from ..core import clock
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


def detect_price_anomalies(conn, instrument_key, anomaly_type="volatility_spike",
                            lookback_days=30, threshold_sigma=2.0):
    """Detect price volatility anomalies. Returns list of anomaly dicts."""
    start = (clock.now() - timedelta(days=lookback_days)).date().isoformat()
    end = clock.now().date().isoformat() + "T23:59:59+00:00"

    observations = conn.execute(
        """SELECT o.id, o.instrument_key, o.observed_at, o.evidence
           FROM observations o
           WHERE o.instrument_key = ? AND o.source = 'price'
           AND o.observed_at >= ? AND o.observed_at <= ?
           ORDER BY o.observed_at""",
        (instrument_key, start, end),
    ).fetchall()

    if len(observations) < 5:
        return []

    values = []
    for obs in observations:
        try:
            evidence = json.loads(obs["evidence"]) if obs["evidence"] else {}
            val = float(evidence.get("value", 0))
            values.append((obs["id"], obs["observed_at"], val))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue

    if len(values) < 5:
        return []

    price_vals = [v[2] for v in values]
    mean = sum(price_vals) / len(price_vals)
    variance = sum((x - mean) ** 2 for x in price_vals) / len(price_vals)
    std = variance ** 0.5
    if std == 0:
        return []

    anomalies = []
    for _obs_id, observed_at, val in values:
        z_score = (val - mean) / std
        if abs(z_score) >= threshold_sigma:
            severity = "critical" if abs(z_score) >= 3 * threshold_sigma else "high" if abs(z_score) >= 2.5 * threshold_sigma else "moderate"
            anomaly = {
                "instrument_key": instrument_key,
                "anomaly_type": anomaly_type,
                "observed_at": observed_at,
                "severity": severity,
                "score": round(abs(z_score), 4),
                "description": f"Price {val} is {z_score:.2f} sigma from mean {mean:.2f}",
            }
            try:
                inserted = conn.execute(
                    """INSERT OR IGNORE INTO price_anomalies(
                         instrument_key, anomaly_type, observed_at, occurred_at,
                         severity, score, description, evidence, source_url)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (instrument_key, anomaly_type, observed_at, observed_at,
                     severity, abs(z_score), anomaly["description"],
                     json.dumps({"z_score": z_score, "value": val}, separators=(",", ":")), ""),
                )
                if inserted.rowcount > 0:
                    anomalies.append(anomaly)
            except sqlite3.IntegrityError:
                continue
    return anomalies


def detect_anomalies(conn, anomaly_type="volatility_spike", lookback_days=30,
                     threshold_sigma=2.0):
    """Detect price anomalies across all instruments. Returns count of anomalies found."""
    instruments = conn.execute(
        "SELECT DISTINCT instrument_key FROM observations WHERE source = 'price' AND instrument_key != ''",
    ).fetchall()
    total = 0
    for inst in instruments:
        total += len(detect_price_anomalies(conn, inst["instrument_key"], anomaly_type,
                                             lookback_days, threshold_sigma))
    return total


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
