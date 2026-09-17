import ipaddress
import json
import math
import re
from urllib.parse import urlsplit
from uuid import uuid4

from .registry import RELATIONSHIPS, add_edge, add_filing, add_metric, set_attr, upsert_entity

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_ENTITIES = 10_000
MAX_RELATIONSHIPS = 50_000
MAX_RECORDS = 100_000


def _text(value, label, limit=4096, required=False):
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise ValueError(f"{label} must be text of at most {limit} characters without NUL")
    if required and not value.strip():
        raise ValueError(f"{label} must be nonempty")
    if any(0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise ValueError(f"{label} contains invalid Unicode")
    return value


def _object(value, fields, label):
    if not isinstance(value, dict) or value.keys() - fields:
        raise ValueError(f"Invalid fields in {label}")
    return value


def _array(value, limit, label):
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError(f"{label} must be an array of at most {limit} records")
    return value


def _scalar(value, label):
    if isinstance(value, str):
        _text(value, label, 16384, True)
    elif type(value) not in (int, float) or (isinstance(value, float) and not math.isfinite(value)):
        raise ValueError(f"{label} must be finite numeric data or nonempty text")
    if isinstance(value, str) and value.strip().lower() in {
        "nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity",
    }:
        raise ValueError(f"{label} must not be nonfinite")
    return value


def _provenance(value, label):
    _text(value, label, 4096, True)
    if value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f"Invalid {label}")
    if value.startswith("local:") and value[6:].strip():
        return value
    try:
        parsed = urlsplit(value)
        if parsed.scheme == "file" and not parsed.netloc and parsed.path.startswith("/") and len(parsed.path) > 1:
            return value
        host = parsed.hostname
        if (parsed.scheme != "https" or not host or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None, 443)
                or "\\" in value or any(c.isspace() for c in value)):
            raise ValueError(f"{label} must be public HTTPS, local:<reference>, or file:///path")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            host = host.rstrip(".").encode("idna").decode("ascii").lower()
            if ("." not in host or host.endswith((".localhost", ".local", ".internal", ".lan", ".home"))
                    or re.fullmatch(r"[0-9.]+", host)
                    or len(host) > 253
                    or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
                           for part in host.split("."))):
                raise ValueError(f"{label} requires a public host")
        else:
            if not address.is_global:
                raise ValueError(f"{label} requires a public address")
    except (ValueError, UnicodeError) as exc:
        raise ValueError(f"Invalid {label}: use public HTTPS, local:<reference>, or file:///path") from exc
    return value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def _constant(value):
    raise ValueError(f"Invalid JSON number: {value}")


def _validate(payload):
    _object(payload, {"entities", "relationships"}, "payload")
    entities = _array(payload.get("entities", []), MAX_ENTITIES, "entities")
    relationships = _array(payload.get("relationships", []), MAX_RELATIONSHIPS, "relationships")
    keys = set()
    total = len(entities) + len(relationships)
    for entity in entities:
        _object(entity, {"key", "kind", "name", "country", "website", "lei", "notes",
                         "attributes", "metrics", "filings"}, "entity")
        for field, limit in (("key", 512), ("kind", 128), ("name", 1024)):
            _text(entity.get(field), field, limit, True)
        if entity["key"] in keys:
            raise ValueError(f"Duplicate entity key: {entity['key']}")
        keys.add(entity["key"])
        for field, limit in (("country", 256), ("website", 4096), ("lei", 128), ("notes", 16384)):
            if entity.get(field) is not None:
                _text(entity[field], field, limit)
        attrs = entity.get("attributes", {})
        if not isinstance(attrs, dict) or len(attrs) > 100:
            raise ValueError("attributes must be an object with at most 100 fields")
        _provenance(attrs.get("source_url"), "attributes.source_url")
        for key, value in attrs.items():
            _text(key, "attribute key", 256, True)
            _scalar(value, f"attribute {key}")
        metrics = _array(entity.get("metrics", []), 1000, "metrics")
        filings = _array(entity.get("filings", []), 1000, "filings")
        total += len(attrs) + len(metrics) + len(filings)
        if total > MAX_RECORDS:
            raise ValueError("Import exceeds total record limit")
        for metric in metrics:
            _object(metric, {"k", "v", "period", "source"}, "metric")
            _text(metric.get("k"), "metric.k", 256, True)
            _scalar(metric.get("v"), "metric.v")
            _text(metric.get("period", ""), "metric.period", 128)
            _text(metric.get("source", ""), "metric.source")
        for filing in filings:
            _object(filing, {"form", "date", "title", "url", "source"}, "filing")
            for field in ("form", "date", "title", "url", "source"):
                _text(filing.get(field, ""), f"filing.{field}")
            if not filing.get("title", "").strip() and not filing.get("url", "").strip():
                raise ValueError("Filing requires title or url")
    seen_edges = set()
    for edge in relationships:
        _object(edge, {"src", "rel", "dst", "source_url", "observed_at", "evidence"}, "relationship")
        for field in ("src", "dst", "rel"):
            _text(edge.get(field), f"relationship.{field}", 512, True)
        if edge["src"] not in keys or edge["dst"] not in keys:
            raise ValueError("Relationship references an entity absent from this import")
        if edge["src"] == edge["dst"] or edge["rel"] not in RELATIONSHIPS:
            raise ValueError("Invalid relationship category or self-reference")
        identity = (edge["src"], edge["rel"], edge["dst"])
        if identity in seen_edges:
            raise ValueError("Duplicate relationship")
        seen_edges.add(identity)
        _provenance(edge.get("source_url"), "relationship.source_url")
        _text(edge.get("observed_at", ""), "relationship.observed_at", 128)
        _text(edge.get("evidence"), "relationship.evidence", 16384, True)
    return entities, relationships


def import_json(conn, path: str) -> dict:
    with open(path, "rb") as stream:
        raw = stream.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("JSON file exceeds 10 MiB")
    try:
        payload = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_pairs, parse_constant=_constant)
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("Invalid UTF-8 JSON or excessive nesting") from exc
    entities, relationships = _validate(payload)
    started = not conn.in_transaction
    if started:
        conn.execute("BEGIN")
    savepoint = "import_json_" + uuid4().hex
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        for entity in entities:
            row = conn.execute("SELECT kind FROM entities WHERE key=?", (entity["key"],)).fetchone()
            if row is not None and row[0] != entity["kind"]:
                raise ValueError("Entity key already belongs to a different kind")
        ids = {}
        result = {"entities": len(entities), "relationships": len(relationships),
                  "attributes": 0, "metrics": 0, "filings": 0}
        for entity in entities:
            fields = {k: entity[k] for k in ("key", "kind", "name", "country", "website", "lei", "notes") if k in entity}
            eid = upsert_entity(conn, **fields)
            ids[entity["key"]] = eid
            for key, value in entity["attributes"].items():
                set_attr(conn, eid, key, value)
                result["attributes"] += 1
            for metric in entity.get("metrics", []):
                add_metric(conn, eid, metric["k"], metric["v"], metric.get("period", ""), metric.get("source", ""))
                result["metrics"] += 1
            for filing in entity.get("filings", []):
                add_filing(conn, eid, *(filing.get(k, "") for k in ("form", "date", "title", "url", "source")))
                result["filings"] += 1
        for edge in relationships:
            add_edge(conn, ids[edge["src"]], edge["rel"], ids[edge["dst"]],
                     edge["source_url"], edge.get("observed_at", ""), edge["evidence"])
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except BaseException:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        if started:
            conn.rollback()
        raise
    return result
