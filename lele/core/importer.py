import ipaddress
import json
import math
import re
from datetime import date, datetime
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
                raise ValueError(f"{label} requires a public host") from None
        else:
            if not address.is_global:
                raise ValueError(f"{label} requires a public address")
    except (ValueError, UnicodeError) as exc:
        raise ValueError(f"Invalid {label}: use public HTTPS, local:<reference>, or file:///path") from exc
    return value


SEC_METRIC_TAGS = {
    "total_assets": ("Assets",),
    "total_liabilities": ("Liabilities",),
    "total_equity": ("StockholdersEquity",),
    "net_income": ("NetIncomeLoss",),
    "revenue": ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"),
    "cash": ("CashAndCashEquivalentsAtCarryingValue",),
    "inventory_net": ("InventoryNet",),
    "long_term_debt_current": ("LongTermDebtCurrent",),
    "long_term_debt_noncurrent": ("LongTermDebtNoncurrent",),
    "short_term_borrowings": ("ShortTermBorrowings",),
    "net_cash_from_operating_activities": ("NetCashProvidedByUsedInOperatingActivities",),
    "net_cash_from_investing_activities": ("NetCashProvidedByUsedInInvestingActivities",),
    "net_cash_from_financing_activities": ("NetCashProvidedByUsedInFinancingActivities",),
}
SEC_CASH_FLOW_METRICS = (
    "net_cash_from_operating_activities", "net_cash_from_investing_activities",
    "net_cash_from_financing_activities",
)
SEC_DURATION_TAGS = frozenset({
    "NetIncomeLoss", *SEC_METRIC_TAGS["revenue"],
    *(tag for metric in SEC_CASH_FLOW_METRICS for tag in SEC_METRIC_TAGS[metric]),
})
SEC_FORMS = frozenset(form + suffix for form in (
    "10-K", "10-Q", "10-KT", "10-QT", "20-F", "40-F", "6-K", "8-K",
    "S-1", "S-4", "S-11", "F-1", "F-4",
) for suffix in ("", "/A"))
SEC_SELECTION = (
    "selected snapshot only: latest(end,filed), earliest valid duration start, tag priority, "
    "greatest canonical JSON tie; conflicting same-metadata values marked ambiguous; "
    "no form or frame preference; not all-history ingestion"
)


def _observation_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("invalid observation date")
    date.fromisoformat(value)
    return value


def validate_metric_envelope(value, period):
    fields = {"value", "currency", "unit", "scale", "observation"}
    _object(value, fields, "metric envelope")
    if value.keys() != fields:
        raise ValueError("metric envelope requires value/currency/unit/scale/observation")
    number = value["value"]
    try:
        finite = type(number) in (int, float) and math.isfinite(number)
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError("metric envelope value must be finite numeric data")
    if (value["currency"] != "USD" or value["unit"] != "currency"
            or type(value["scale"]) not in (int, float) or value["scale"] != 1):
        raise ValueError("SEC envelope requires USD/currency/scale 1")
    observation = value["observation"]
    required = {"schema", "taxonomy", "tag", "accession", "form", "filed", "start", "end",
                "period_type", "source_url", "retrieved_at", "selection"}
    _object(observation, required | {"ambiguous", "invalid"}, "observation")
    if not required <= observation.keys():
        raise ValueError("partial observation metadata")
    for field, item in observation.items():
        if field in ("ambiguous", "invalid"):
            if type(item) is not bool:
                raise ValueError(f"observation.{field} must be boolean")
        elif item is None and field in ("accession", "form", "start"):
            continue
        else:
            _text(item, f"observation.{field}", 4096 if field == "source_url" else 1024, True)
            if item != item.strip() or any(ord(c) < 32 or ord(c) == 127 for c in item):
                raise ValueError(f"invalid observation.{field}")
    if observation["schema"] != "sec_fact_v1" or observation["taxonomy"] != "us-gaap":
        raise ValueError("unsupported observation schema or taxonomy")
    tag = observation["tag"]
    if tag not in {tag for tags in SEC_METRIC_TAGS.values() for tag in tags}:
        raise ValueError("unsupported SEC tag")
    expected = "duration" if tag in SEC_DURATION_TAGS else "instant"
    if observation["period_type"] != expected:
        raise ValueError("observation period_type does not match tag")
    accession, form = observation["accession"], observation["form"]
    if accession is not None and not re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", accession):
        raise ValueError("invalid observation accession")
    if form is not None and form not in SEC_FORMS:
        raise ValueError("unsupported observation form")
    end = _observation_date(observation["end"])
    _observation_date(observation["filed"])
    if period != end:
        raise ValueError("metric period must equal observation end")
    start = observation["start"]
    if start is not None:
        if _observation_date(start) > end:
            raise ValueError("observation start exceeds end")
        if expected == "instant" and observation.get("invalid") is not True:
            raise ValueError("instant observation with start must be marked invalid")
    _provenance(observation["source_url"], "observation.source_url")
    retrieved = observation["retrieved_at"]
    if not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})", retrieved
    ):
        raise ValueError("invalid observation retrieved_at")
    datetime.fromisoformat(retrieved)
    return value


def serialize_metric_envelope(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


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
            if isinstance(metric.get("v"), dict):
                validate_metric_envelope(metric["v"], metric.get("period", ""))
            else:
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
                value = metric["v"]
                if isinstance(value, dict):
                    value = serialize_metric_envelope(value)
                add_metric(conn, eid, metric["k"], value, metric.get("period", ""), metric.get("source", ""))
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
