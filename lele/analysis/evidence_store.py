"""Persist fetched world-state evidence so it can be queried by time range.

`fetch-evidence` writes a file and stops. That is the right shape for the
`worldstate` and `prospective` methods, which read a file at a known instant —
but it means the same numbers cannot be asked about later. A leverage reading
you fetched once and did not store is gone, and there is no way to ask "what was
the open interest doing in the week the price fell 8%".

This converts the evidence document into `observations` rows without changing
what any number means. The conversion is deliberately mechanical:

* the external id is reused, so re-running refines rather than duplicates;
* `occurred_at`, `available_at` and `source_url` are carried through unchanged,
  so a record published later is still visible as having been published later;
* the measurement value, unit, currency and basis go into the amount fields;
* the platform and the evidence document are kept in JSON;
* nothing is added. No counterparty, no direction, no motive, no "this caused".
"""
import json

from ..core import registry
from . import events, signals

METHOD = "evidence_to_observations_v1"
MAX_ROWS = 100_000

# Only kinds the evidence taxonomy actually defines are accepted. An unmapped
# kind is a bug in the fetcher, not a new kind to invent here.
ALLOWED_KINDS = frozenset(signals.OBSERVATION_FAMILY)


def validate(limit):
    if type(limit) is not int or not 1 <= limit <= MAX_ROWS:
        raise ValueError(f"limit must be an integer from 1 to {MAX_ROWS}")
    return limit


def convert(payload: dict, instrument_key: str, entity_id: int, source: str,
            retrieved_at: str = "") -> tuple[list, dict]:
    """Evidence-document records to `add_observation` keyword arguments.

    Split from the write so the mapping can be tested against a fixture without
    a database, and so a caller can see exactly what would be stored first.
    """
    if not isinstance(payload, dict):
        raise ValueError("evidence payload must be an object")
    rows = payload.get("observations")
    if not isinstance(rows, list):
        raise ValueError("evidence payload must contain an observations array")
    # The same key validation worldstate applies, so a bad key is refused here
    # rather than at the first insert with a less specific message.
    events._evidence(None, instrument_key)
    converted: list = []
    skipped: dict = {"no_measurement": 0, "unknown_kind": 0, "malformed": 0}
    for row in rows:
        if not isinstance(row, dict):
            skipped["malformed"] += 1
            continue
        kind = row.get("kind")
        if kind not in ALLOWED_KINDS:
            skipped["unknown_kind"] += 1
            continue
        measurement = row.get("measurement")
        if not isinstance(measurement, dict):
            skipped["no_measurement"] += 1
            continue
        value = measurement.get("value")
        if not isinstance(value, str) or not value.strip():
            skipped["no_measurement"] += 1
            continue
        observed = row.get("observed_at") or ""
        if not observed:
            skipped["malformed"] += 1
            continue
        external = row.get("id") or f"{source}:{kind}:{observed}"
        converted.append({
            "source": source,
            "external_id": str(external)[:512],
            "kind": kind,
            "description": str(row.get("description") or "")[:4096],
            "actor_key": "",
            "counterparty_key": "",
            "instrument_key": instrument_key,
            "action": "",
            "reason": "",
            "reason_basis": "unknown",
            "amount": str(value)[:64],
            "unit": str(measurement.get("unit") or "")[:1024],
            "currency": str(measurement.get("currency") or "")[:1024],
            "basis": "estimated" if measurement.get("basis") == "estimated" else "observed",
            "occurred_at": observed,
            "observed_at": observed,
            "available_at": row.get("available_at") or observed,
            "source_url": str(row.get("source_url") or "")[:4096],
            "evidence": json.dumps(
                {"method": METHOD, "platform": row.get("platform"),
                 "retrieved_at": retrieved_at, "entity_id": entity_id,
                 "evidence_id": external, "mapping": row.get("mapping"),
                 "conversion_note": "measurement copied unchanged; no counterparty, "
                                    "direction or motive was inferred"},
                sort_keys=True, separators=(",", ":"))[:1024],
        })
    return converted, skipped


def store(conn, payload: dict, instrument_key: str, entity_id: int, source: str,
          retrieved_at: str = "", limit: int = MAX_ROWS) -> dict:
    """Store an evidence document as observations and report what happened."""
    validate(limit)
    converted, skipped = convert(payload, instrument_key, entity_id, source, retrieved_at)
    truncated = len(converted) > limit
    converted = converted[:limit]
    stored = 0
    by_kind: dict = {}
    for row in converted:
        try:
            registry.add_observation(conn, **row)
            stored += 1
            by_kind[row["kind"]] = by_kind.get(row["kind"], 0) + 1
        except ValueError as error:
            skipped["rejected"] = skipped.get("rejected", 0) + 1
            if "rejected_first" not in skipped:
                skipped["rejected_first"] = str(error)[:300]
    return {
        "method": METHOD,
        "instrument_key": instrument_key,
        "entity_id": entity_id,
        "source": source,
        "source_url": str(payload.get("source_url") or "")[:4096],
        "retrieved_at": retrieved_at,
        "read": len(payload.get("observations") or []),
        "converted": len(converted),
        "stored": stored,
        "by_kind": dict(sorted(by_kind.items())),
        "skipped": skipped,
        "truncated": truncated,
        "now_queryable_by_window": True,
        "limitations": [
            "Only instrument-bound records are stored. The evidence document also "
            "describes the venue, the contract and the symbol, and none of those is "
            "turned into a counterparty here.",
            "A stored measurement is the same number the evidence file carried. Storing it "
            "adds no information, no timestamp accuracy and no history that the provider "
            "did not already return.",
            "Re-running refines the existing row for the same external id rather than "
            "adding a second one, so a repeated fetch cannot inflate a count.",
            "The stored values are point-in-time venue readings. They show what was "
            "positioned on one venue at five-minute resolution and identify no "
            "participant, and one venue is not the market.",
        ],
    }
