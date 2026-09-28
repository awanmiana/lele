"""Money flow attribution: traces money movements to events.

For each event, finds money flows in the same time window involving related
entities and classifies flow direction relative to the event.
"""
from ..core import clock
from datetime import datetime, timedelta
import sqlite3


DEFAULT_WINDOW_HOURS = 48
MAX_WINDOW_HOURS = 168


def _hours_diff(iso_a, iso_b):
    dt_a = datetime.fromisoformat(iso_a.replace("Z", "+00:00"))
    dt_b = datetime.fromisoformat(iso_b.replace("Z", "+00:00"))
    return abs((dt_a - dt_b).total_seconds() / 3600)


def _compute_attribution_score(event, flow):
    """Compute attribution score between an event and a money flow."""
    if not event["actor_key"] or not flow["src_id"]:
        return 0.0

    event_time = event["occurred_at"]
    flow_time = flow["flow_occurred_at"]
    time_diff_hours = _hours_diff(event_time, flow_time)

    if time_diff_hours > DEFAULT_WINDOW_HOURS:
        return 0.0

    if time_diff_hours <= 1:
        return 0.9
    if time_diff_hours <= 6:
        return 0.6
    if time_diff_hours <= 24:
        return 0.3
    return 0.1


def _classify_flow_direction(event, flow, instrument_key):
    """Classify flow direction relative to the affected instrument's entities."""
    event_actor = event["actor_key"] if event["actor_key"] else ""
    flow_src = flow["src_id"] if flow["src_id"] else ""
    flow_dst = flow["dst_id"] if flow["dst_id"] else ""

    event_entities = set()
    if event_actor:
        event_entities.add(event_actor)
    if event["counterparty_key"]:
        event_entities.add(event["counterparty_key"])

    if flow_src in event_entities:
        return "event_actor_flow"
    if flow_dst in event_entities:
        return "event_actor_flow"
    return "unrelated"


def attribute_money_flows_to_events(conn, event_ids=None, window_hours=DEFAULT_WINDOW_HOURS):
    """Link money flows to causal events by actor key and temporal proximity.

    Args:
        conn: SQLite connection
        event_ids: Optional list of event IDs to attribute flows for
        window_hours: Time window in hours around event occurrence

    Returns:
        Number of attributions created
    """
    if type(window_hours) is not int or not 1 <= window_hours <= MAX_WINDOW_HOURS:
        raise ValueError(f"window_hours must be 1..{MAX_WINDOW_HOURS}")

    if event_ids is None:
        events = conn.execute(
            "SELECT id, actor_key, counterparty_key, instrument_key, occurred_at "
            "FROM event_store WHERE occurred_at >= ?",
            ((clock.now() - timedelta(hours=window_hours)).isoformat(),),
        ).fetchall()
    else:
        placeholders = ",".join("?" for _ in event_ids)
        events = conn.execute(
            f"SELECT id, actor_key, counterparty_key, instrument_key, occurred_at "
            f"FROM event_store WHERE id IN ({placeholders})",
            event_ids,
        ).fetchall()

    if not events:
        return 0

    attribution_count = 0
    skipped = 0
    first_error = ""

    for event in events:
        event_id = event["id"]
        actor_key = event["actor_key"]
        instrument_key = event["instrument_key"]
        occurred = event["occurred_at"]

        flows = conn.execute(
            """SELECT m.id AS flow_id, m.src_id, m.dst_id, m.flow_type,
                      m.amount, m.currency, m.occurred_at AS flow_occurred_at
               FROM money_flows m
               WHERE m.occurred_at >= ? AND m.occurred_at <= ?
               ORDER BY ABS(julianday(m.occurred_at) - julianday(?))""",
            (occurred,
             (datetime.fromisoformat(occurred.replace("Z", "+00:00"))
              + timedelta(hours=window_hours)).isoformat(),
             occurred),
        ).fetchall()

        for flow in flows:
            attribution_score = _compute_attribution_score(event, flow)
            if attribution_score < 0.1:
                continue

            flow_direction = _classify_flow_direction(event, flow, instrument_key)

            time_diff_hours = _hours_diff(event["occurred_at"], flow["flow_occurred_at"])

            try:
                inserted = conn.execute(
                    """INSERT OR IGNORE INTO money_flow_attribution(
                         event_id, flow_id, src_id, dst_id, attribution_type,
                         attribution_score, rationale)
                       VALUES(?,?,?,?,?,?,?)""",
                    (event_id, flow["flow_id"], flow["src_id"], flow["dst_id"],
                     "direct" if attribution_score > 0.5 else "indirect",
                     attribution_score,
                     f"Actor {actor_key} linked to flow {flow['flow_id']} "
                     f"({flow_direction}) within {time_diff_hours:.1f}h"),
                )
                if inserted.rowcount > 0:
                    attribution_count += 1
            except sqlite3.Error as error:
                skipped += 1
                if not first_error:
                    first_error = f"{type(error).__name__}: {error}"

    if skipped:
        # A dropped attribution would read as "this event has no money behind
        # it", which is a different claim from "the attribution could not be
        # written", so it is raised rather than counted quietly.
        raise sqlite3.Error(f"{skipped} money-flow attribution(s) could not be written; "
                            f"first failure: {first_error}")
    return attribution_count


def attribute_all_money_flows(conn, window_hours=DEFAULT_WINDOW_HOURS):
    """Attribute money flows to all events in the time window."""
    return attribute_money_flows_to_events(conn, event_ids=None, window_hours=window_hours)


def get_money_flow_attributions(conn, event_id=None, flow_id=None, limit=100):
    """Get money flow attributions, optionally filtered."""
    if event_id:
        return conn.execute(
            "SELECT * FROM money_flow_attribution WHERE event_id = ? "
            "ORDER BY attribution_score DESC LIMIT ?",
            (event_id, limit),
        ).fetchall()
    if flow_id:
        return conn.execute(
            "SELECT * FROM money_flow_attribution WHERE flow_id = ? "
            "ORDER BY attribution_score DESC LIMIT ?",
            (flow_id, limit),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM money_flow_attribution ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
