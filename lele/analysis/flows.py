import hashlib
import json
import re

from ..core import importer, registry
from . import projection

MAX_FILE_BYTES = projection.MAX_FILE_BYTES
MAX_FLOWS = 10_000
DATE_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2}))?")
FLOW_FIELDS = {"src", "dst", "type", "amount", "currency", "occurred_at", "source_url", "evidence"}


def _occurred(value):
    if value is None:
        return ""
    importer._text(value, "occurred_at", 64, True)
    if not DATE_RE.fullmatch(value):
        raise ValueError("occurred_at must be an ISO date or datetime")
    return value


def import_flows(conn, path):
    with open(path, "rb") as stream:
        raw = stream.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("flows file exceeds 2 MiB")
    try:
        payload = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=importer._pairs,
                             parse_constant=importer._constant)
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("flows file is not valid UTF-8 JSON") from exc
    projection._exact_object(payload, {"flows"}, "flows file")
    rows = importer._array(payload["flows"], MAX_FLOWS, "flows")
    currencies = set()
    for row in rows:
        if not isinstance(row, dict) or row.keys() - FLOW_FIELDS:
            raise ValueError("flow has unsupported fields")
        missing = {"src", "dst", "type", "amount", "currency", "source_url"} - row.keys()
        if missing:
            raise ValueError("flow requires src, dst, type, amount, currency and source_url")
        source_key = importer._text(row.get("src"), "flow source", 512, True)
        destination_key = importer._text(row.get("dst"), "flow destination", 512, True)
        if source_key == destination_key:
            raise ValueError("flow source and destination must differ")
        source_id = registry.entity_id_by_key(conn, source_key)
        destination_id = registry.entity_id_by_key(conn, destination_key)
        if source_id is None:
            raise ValueError(f"flow source entity {source_key!r} is not in the registry")
        if destination_id is None:
            raise ValueError(f"flow destination entity {destination_key!r} is not in the registry")
        source_url = importer._text(row.get("source_url"), "flow source_url", 4096, True)
        importer._provenance(source_url, "flow source_url")
        flow_type = importer._text(row.get("type"), "flow type", 64, True)
        amount = importer._text(row.get("amount"), "flow amount", 64, True)
        currency = importer._text(row.get("currency"), "flow currency", 32, True)
        evidence = importer._text(row.get("evidence"), "flow evidence", 2048) or ""
        registry.add_money_flow(conn, source_id, destination_id, flow_type, amount, currency,
                                _occurred(row.get("occurred_at")), source_url, evidence)
        currencies.add(currency)
    return {"imported": len(rows), "currencies": sorted(currencies),
            "file_sha256": hashlib.sha256(raw).hexdigest(),
            "amounts_summed_across_currencies": False}
