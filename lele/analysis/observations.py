import hashlib
import json
import os
import re
import tempfile

from ..core import importer, registry
from . import events, projection

MAX_FILE_BYTES = projection.MAX_FILE_BYTES
MAX_OBSERVATIONS = 10_000
DATE_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2}))?")
FIELDS = {
    "source", "external_id", "kind", "description", "actor", "counterparty", "instrument",
    "action", "reason", "reason_basis", "amount", "unit", "currency", "basis", "occurred_at",
    "observed_at", "available_at", "source_url", "evidence",
}
REQUIRED = {"source", "external_id", "kind", "observed_at", "available_at", "source_url"}


def _occurred(value):
    if value is None:
        return ""
    importer._text(value, "occurred_at", 64, True)
    if not DATE_RE.fullmatch(value):
        raise ValueError("occurred_at must be an ISO date or datetime")
    return value


def _measurement(row):
    amount = row.get("amount")
    if amount is None:
        return None
    unit = importer._text(row.get("unit"), "observation unit", 128, True)
    currency = row.get("currency") or None
    if currency is not None:
        importer._text(currency, "observation currency", 128, True)
    basis = row.get("basis", "observed")
    if basis not in registry.OBSERVATION_BASES:
        raise ValueError("observation basis must be observed or estimated")
    return {"value": amount, "unit": unit, "currency": currency or None, "basis": basis}


def _evidence_row(observation, entity_key):
    observed = events._time(observation["observed_at"]).isoformat()
    available = events._time(observation["available_at"]).isoformat()
    kind = observation["kind"]
    actor = observation["actor_key"]
    counterparty = observation["counterparty_key"]
    if kind == "decision":
        mapping = {"from": None, "to": None, "actor": actor or None,
                   "action": observation["action"] or None,
                   "reason": observation["reason"] or None,
                   "reason_basis": observation["reason_basis"], "related_ids": [],
                   "source_locator": observation["source_url"] or None}
    elif kind in events.ROUTED_KINDS:
        mapping = {"from": actor or None, "to": counterparty or None, "actor": None, "action": None,
                   "reason": None, "reason_basis": "unknown", "related_ids": [],
                   "source_locator": observation["source_url"] or None}
    else:
        mapping = None
    row = {
        "id": f"{observation['source']}:{observation['external_id']}",
        "entity_key": entity_key,
        "observed_at": observed,
        "available_at": available,
        "kind": kind,
        "source_url": observation["source_url"],
        "description": observation["description"],
        "platform": None,
        "industry": None,
        "location": None,
        "measurement": _measurement(observation),
        "mapping": mapping,
    }
    if available < observed:
        raise ValueError("availability cannot precede observation")
    events._mapping(row)
    return row


def _validate_row(row, instrument_id):
    if not isinstance(row, dict) or row.keys() - FIELDS or not row.keys() >= REQUIRED:
        raise ValueError("observation requires exactly the documented fields")
    source = importer._text(row.get("source"), "observation source", 256, True)
    external = importer._text(row.get("external_id"), "observation external_id", 512, True)
    kind = importer._text(row.get("kind"), "observation kind", 128, True)
    if kind not in events.KINDS:
        raise ValueError("unsupported observation kind")
    instrument = row.get("instrument")
    if instrument is None:
        instrument = ""
    else:
        importer._text(instrument, "observation instrument", 512)
    if instrument and instrument_id is None:
        raise ValueError(f"observation instrument {instrument!r} is not in the registry")
    importer._text(row.get("description", "") or "", "observation description", 4096)
    for field in ("actor", "counterparty"):
        value = row.get(field)
        if value is not None:
            importer._text(value, "observation " + field, 512)
    for field in ("action", "reason"):
        value = row.get(field)
        if value is not None:
            importer._text(value, field, 1024)
    reason_basis = row.get("reason_basis", "unknown")
    if reason_basis is None:
        reason_basis = "unknown"
    source_url = importer._provenance(row.get("source_url"), "observation source_url")
    occurred = _occurred(row.get("occurred_at"))
    observed = events._time(row["observed_at"]).isoformat()
    available = events._time(row["available_at"]).isoformat()
    if available < observed:
        raise ValueError("availability cannot precede observation")
    amount = row.get("amount")
    if amount is not None:
        importer._text(amount, "observation amount", 64, True)
    stored = {
        "source": source, "external_id": external, "kind": kind,
        "description": row.get("description") or "", "actor_key": row.get("actor") or "",
        "counterparty_key": row.get("counterparty") or "", "instrument_key": instrument,
        "action": row.get("action") or "", "reason": row.get("reason") or "",
        "reason_basis": reason_basis, "amount": amount, "unit": row.get("unit") or "",
        "currency": row.get("currency") or "", "basis": row.get("basis", "observed"),
        "occurred_at": occurred, "observed_at": observed, "available_at": available,
        "source_url": source_url, "evidence": row.get("evidence") or "",
    }
    if kind == "decision":
        if not stored["action"]:
            raise ValueError("a decision observation requires an action")
    elif kind in events.ROUTED_KINDS:
        if not stored["actor_key"] or not stored["counterparty_key"]:
            raise ValueError("a routed flow observation requires actor (origin) and "
                             "counterparty (destination)")
        if stored["action"] or stored["reason"] or reason_basis != "unknown":
            raise ValueError("routed flow observations must not carry decision claims")
    elif stored["action"] or stored["reason"] or reason_basis != "unknown":
        raise ValueError("only a decision observation may carry an action, a reason or an "
                         "attributed reason basis")
    return stored


def evidence_to_rows(rows, source):
    """Convert world-state evidence observations into normalized import rows.

    A fetcher returns evidence-shaped observations; this is the only bridge from
    that shape into the registry `observations` table. Nothing is inferred: an
    absent actor, counterparty, amount or motive stays absent, the evidence
    measurement becomes the amount with its own unit, and the raw evidence row
    is preserved verbatim in the stored evidence field.
    """
    importer._text(source, "evidence source", 256, True)
    converted = []
    skipped = []
    for row in importer._array(rows, MAX_OBSERVATIONS, "evidence observations"):
        if not isinstance(row, dict):
            skipped.append({"reason": "not_an_object"})
            continue
        kind = row.get("kind")
        identifier = row.get("id")
        if not isinstance(kind, str) or kind not in events.KINDS:
            skipped.append({"reason": "unsupported_kind", "kind": str(kind)})
            continue
        if not isinstance(identifier, str) or not identifier.strip():
            skipped.append({"reason": "missing_id", "kind": str(kind)})
            continue
        raw_mapping = row.get("mapping")
        raw_measurement = row.get("measurement")
        mapping: dict = raw_mapping if isinstance(raw_mapping, dict) else {}
        measurement: dict = raw_measurement if isinstance(raw_measurement, dict) else {}
        actor = str(mapping.get("actor") or "")
        counterparty = str(mapping.get("to") or "")
        if kind in events.ROUTED_KINDS:
            actor = str(mapping.get("from") or actor)
            counterparty = str(mapping.get("to") or counterparty)
        converted.append({
            "source": source, "external_id": identifier, "kind": kind,
            "description": row.get("description") or "",
            "actor": actor or None, "counterparty": counterparty or None,
            "instrument": row.get("entity_key") or None,
            "action": mapping.get("action") if kind == "decision" else None,
            "reason": None, "reason_basis": "unknown",
            "amount": measurement.get("value"),
            "unit": str(measurement.get("unit") or ""),
            "currency": str(measurement.get("currency") or ""),
            "basis": str(measurement.get("basis") or "observed"),
            "occurred_at": row.get("observed_at"),
            "observed_at": row.get("observed_at"),
            "available_at": row.get("available_at") or row.get("observed_at"),
            "source_url": row.get("source_url") or "local:evidence",
            "evidence": json.dumps(row, sort_keys=True, ensure_ascii=True,
                                   separators=(",", ":"), allow_nan=False),
        })
    return converted, skipped


def ingest_evidence(conn, rows, source):
    """Store fetcher evidence observations in the registry observations table."""
    converted, skipped = evidence_to_rows(rows, source)
    result = store_observations(conn, converted)
    result["converted"] = len(converted)
    result["skipped"] = skipped[:20]
    result["skipped_count"] = len(skipped)
    return result


def import_observations(conn, path):
    with open(path, "rb") as stream:
        raw = stream.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("observations file exceeds 2 MiB")
    try:
        payload = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=importer._pairs,
                             parse_constant=importer._constant)
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("observations file is not valid UTF-8 JSON") from exc
    projection._exact_object(payload, {"observations"}, "observations file")
    rows = importer._array(payload["observations"], MAX_OBSERVATIONS, "observations")
    result = store_observations(conn, rows)
    result["file_sha256"] = hashlib.sha256(raw).hexdigest()
    return result


def store_observations(conn, rows):
    stored_rows = []
    sources, kinds = set(), set()
    for row in rows:
        instrument = (row.get("instrument") if isinstance(row, dict) else None) or ""
        instrument_id = registry.entity_id_by_key(conn, instrument) if instrument else None
        stored = _validate_row(row, instrument_id)
        _evidence_row(stored, instrument or "")
        stored_rows.append(stored)
    for stored in stored_rows:
        registry.add_observation(conn, **stored)
        sources.add(stored["source"])
        kinds.add(stored["kind"])
    return {"imported": len(rows), "sources": sorted(sources), "kinds": sorted(kinds)}


def evidence_payload(conn, entity_id):
    entity = registry.get_entity(conn, entity_id)
    if entity is None:
        raise ValueError(f"entity {entity_id} not found")
    entity_key = entity["key"]
    observations = [_evidence_row(row, entity_key)
                    for row in registry.observations_for_instrument(conn, entity_id)]
    payload = {"observations": observations}
    raw = json.dumps(payload, ensure_ascii=True, allow_nan=False,
                     separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("projected evidence exceeds 2 MiB")
    descriptor, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
        events._evidence(path, entity_key)
    finally:
        os.unlink(path)
    kinds: dict[str, int] = {}
    for observation in observations:
        kinds[observation["kind"]] = kinds.get(observation["kind"], 0) + 1
    return payload, {"entity_id": entity_id, "entity_key": entity_key,
                     "observations": len(observations), "by_kind": dict(sorted(kinds.items())),
                     "evidence_sha256": hashlib.sha256(raw).hexdigest(),
                     "instrument_bound_only": True}
