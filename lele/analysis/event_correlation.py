"""Event correlation engine: links events to volatility instances.

For each ZigZag leg (volatility instance), finds events within a configurable
time window and scores attribution based on time proximity, kind relevance,
and actor overlap.
"""
from datetime import datetime, timedelta

from ..core import registry
from . import worldstate
import sqlite3


DEFAULT_WINDOW_HOURS = 24
MAX_WINDOW_HOURS = 168


def _hours_diff(iso_a, iso_b):
    dt_a = datetime.fromisoformat(iso_a.replace("Z", "+00:00"))
    dt_b = datetime.fromisoformat(iso_b.replace("Z", "+00:00"))
    return abs((dt_a - dt_b).total_seconds() / 3600)


def _score_attribution(event, volatility, kind_weights):
    """Score an event's attribution to a volatility instance."""
    time_diff_hours = _hours_diff(event["occurred_at"], volatility["as_of"])
    if time_diff_hours > DEFAULT_WINDOW_HOURS:
        return 0.0

    time_score = 1.0 / (1.0 + time_diff_hours)

    kind = event["event_type"] if event["event_type"] else ""
    kind_weight = kind_weights.get(kind, 0)
    max_weight = max(kind_weights.values()) if kind_weights else 1
    kind_score = kind_weight / max_weight if max_weight > 0 else 0

    actor_overlap = 0.0
    if event["actor_key"] and volatility["instrument_key"]:
        event_actor = event["actor_key"]
        inst_key = volatility["instrument_key"]
        if event_actor and inst_key:
            actor_overlap = 0.5

    return (time_score + kind_score + actor_overlap) / 3.0


def correlate_events_to_volatility(conn, instrument_key, threshold_percent=3,
                                    window_hours=DEFAULT_WINDOW_HOURS,
                                    min_confidence=0.1):
    """Correlate events to volatility instances for an instrument.

    Args:
        conn: SQLite connection
        instrument_key: The instrument key to analyze
        threshold_percent: ZigZag threshold for volatility detection
        window_hours: Time window in hours around volatility start to search for events
        min_confidence: Minimum confidence score to create a link

    Returns:
        Number of event-volatility links created
    """
    if type(window_hours) is not int or not 1 <= window_hours <= MAX_WINDOW_HOURS:
        raise ValueError(f"window_hours must be 1..{MAX_WINDOW_HOURS}")

    volatility_instances = registry.list_volatility_instances(
        conn, instrument_key=instrument_key, limit=1000
    )
    if not volatility_instances:
        return 0

    event_types = ("political_event", "geopolitical_event", "regulatory_action",
                   "market_shock", "news_event", "macro_release", "filing_event")
    kind_weights = worldstate.DIRECTION_WEIGHTS

    links_created = 0
    skipped = 0
    first_error = ""

    for vol in volatility_instances:
        vol_id = vol["id"]
        vol_start = vol["as_of"]
        vol_end = vol["target_time"]

        cutoff_start = (datetime.fromisoformat(vol_start.replace("Z", "+00:00"))
                        - timedelta(hours=window_hours)).isoformat()
        cutoff_end = (datetime.fromisoformat(vol_end.replace("Z", "+00:00"))
                      + timedelta(hours=window_hours)).isoformat()

        events = conn.execute(
            """SELECT * FROM event_store
               WHERE instrument_key = ? AND event_type IN ({})
               AND occurred_at >= ? AND occurred_at <= ?""".format(
                   ",".join("?" for _ in event_types)),
            [instrument_key] + list(event_types) + [cutoff_start, cutoff_end],
        ).fetchall()

        for event in events:
            confidence = _score_attribution(event, vol, kind_weights)
            if confidence < min_confidence:
                continue

            time_delta_seconds = int(abs(
                (datetime.fromisoformat(event["occurred_at"].replace("Z", "+00:00"))
                 - datetime.fromisoformat(vol_start.replace("Z", "+00:00"))).total_seconds()
            ))

            try:
                registry.add_event_volatility_link(
                    conn,
                    event_id=event["id"],
                    volatility_id=vol_id,
                    confidence=str(confidence),
                    time_delta_seconds=time_delta_seconds,
                    evidence_basis="temporal_proximity_kind_actor",
                )
                links_created += 1
            except sqlite3.Error as error:
                skipped += 1
                if not first_error:
                    first_error = f"{type(error).__name__}: {error}"

    if skipped:
        # A silently dropped link would read as "no correlation exists", which is
        # a different claim from "the link could not be written".
        raise sqlite3.Error(f"{skipped} event-volatility link(s) could not be written; "
                            f"first failure: {first_error}")
    return links_created


def correlate_all_instruments(conn, threshold_percent=3, window_hours=DEFAULT_WINDOW_HOURS,
                               min_confidence=0.1):
    """Correlate events to volatility instances across all instruments."""
    instruments = conn.execute(
        "SELECT DISTINCT instrument_key FROM volatility_instances WHERE instrument_key != ''"
    ).fetchall()

    total = 0
    for inst in instruments:
        total += correlate_events_to_volatility(
            conn, inst["instrument_key"], threshold_percent, window_hours, min_confidence
        )
    return total


def get_event_volatility_links(conn, instrument_key=None, limit=100):
    """Get event-volatility links, optionally filtered by instrument."""
    if instrument_key:
        vol_ids = [row["id"] for row in conn.execute(
            "SELECT id FROM volatility_instances WHERE instrument_key = ?", (instrument_key,)
        ).fetchall()]
        if not vol_ids:
            return []
        placeholders = ",".join("?" for _ in vol_ids)
        return conn.execute(
            f"SELECT * FROM event_volatility_links WHERE volatility_id IN ({placeholders}) "
            f"ORDER BY time_delta_seconds LIMIT ?",
            vol_ids + [limit],
        ).fetchall()
    return conn.execute(
        "SELECT * FROM event_volatility_links ORDER BY time_delta_seconds LIMIT ?",
        (limit,),
    ).fetchall()
