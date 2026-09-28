import hashlib
import json
import re
from ..core import clock
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from ..analysis import observations
from ..core import registry
from .http import HTTPClient, SourceError

MAX_LIMIT_DEFAULT = 25
MAX_LIMIT = 100
SOURCE = "political-event"
FEDERAL_REGISTER = "https://www.federalregister.gov/api/v1/documents"
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
MAX_DOCUMENTS = 1000


def _iso_date(value, label):
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise ValueError(f"{label} must be an ISO date YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a valid ISO date") from exc


def _decimal(text):
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        number = Decimal(text.strip().replace(",", ""))
    except InvalidOperation:
        return None
    if not number.is_finite() or not -12 <= number.adjusted() <= 12:
        return None
    return number


ABSTRACT_LIMIT = 400
AGENCY_LIMIT = 8
EVIDENCE_LIMIT = 1000


def _agency_names(raw):
    """Agency names only, bounded.

    The registry stores observation evidence as bounded text, and the Federal
    Register returns nested agency objects with their own URLs, so keeping them
    whole overflows the field. Names are kept and the reduction is recorded.
    """
    names = []
    if isinstance(raw, list):
        for entry in raw[:AGENCY_LIMIT]:
            if isinstance(entry, dict):
                name = entry.get("name") or entry.get("raw_name") or ""
            else:
                name = entry
            if isinstance(name, str) and name.strip():
                names.append(name.strip()[:120])
    return names, len(raw) if isinstance(raw, list) else 0


def _abstract(raw):
    if not isinstance(raw, str):
        return "", False
    text = " ".join(raw.split())
    if len(text) <= ABSTRACT_LIMIT:
        return text, False
    return text[:ABSTRACT_LIMIT], True


def _evidence(document_number, record, raw_type, publication_date, title, agencies,
              agency_count, abstract, abstract_truncated):
    """Build bounded observation evidence.

    The registry stores observation evidence as bounded text, and Federal
    Register documents carry nested agency objects, long abstracts and two URLs,
    so the payload is reduced in a fixed order until it fits. Identity fields are
    kept, and the document URL is not repeated because the observation already
    carries it in `source_url`; whatever else is dropped is named in
    `bounded_fields.dropped`, so a consumer sees the reduction instead of
    inferring completeness.
    """
    payload = {
        "document_number": document_number, "citation": record.get("citation", "") or "",
        "title": title[:300], "type": raw_type, "publication_date": publication_date,
        "effective_on": record.get("effective_on", "") or "",
        "government_entity": (record.get("government_entity", "") or "")[:120],
        "agencies": agencies, "agencies_total": agency_count, "abstract": abstract,
        "document_urls": "carried by the observation source_url, not repeated here",
        "amount_semantics": "Federal Register publication of a government action; no monetary "
                            "amount is implied",
    }
    dropped = []
    for field in ("abstract", "agencies", "effective_on", "government_entity", "citation",
                  "title"):
        if len(json.dumps(payload, sort_keys=True, ensure_ascii=True,
                          separators=(",", ":"))) <= EVIDENCE_LIMIT:
            break
        payload.pop(field, None)
        dropped.append(field)
    payload["bounded_fields"] = {"abstract": abstract_truncated,
                                 "agencies": agency_count > len(agencies),
                                 "dropped": dropped}
    evidence = _render(payload)
    if len(evidence) <= EVIDENCE_LIMIT:
        return evidence
    reduced = _minimal(document_number, raw_type, publication_date, title)
    reduced["bounded_fields"] = {"abstract": True, "agencies": True,
                                 "dropped": dropped + ["all_optional_fields"]}
    return _render(reduced)



def _render(payload):
    return json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def _minimal(document_number, raw_type, publication_date, title):
    """Last-resort payload: durable identity only, with the title bounded."""
    payload = {"document_number": document_number, "type": raw_type,
               "publication_date": publication_date,
               "amount_semantics": "Federal Register publication of a government action; no "
                                   "monetary amount is implied"}
    if len(_render(payload)) > EVIDENCE_LIMIT:
        payload["title"] = title[:80]
    return payload


def _observation(record, retrieved_at):
    document_number = record.get("document_number") or record.get("citation") or ""
    title = record.get("title", "") or ""
    raw_type = record.get("type", "") or ""
    if isinstance(raw_type, list):
        raw_type = raw_type[0] if raw_type else ""
    publication_date = record.get("publication_date", "")
    if not isinstance(publication_date, str) or not DATE_RE.fullmatch(publication_date):
        return None
    if not title.strip():
        return None
    description = (f"Political/regulatory event: {title} ({raw_type})")
    actor_key = f"political:{raw_type}:{document_number}" if document_number else f"political:{raw_type}"
    agencies, agency_count = _agency_names(record.get("agencies", []))
    abstract, abstract_truncated = _abstract(record.get("abstract", ""))
    evidence = _evidence(document_number, record, raw_type, publication_date, title, agencies,
                         agency_count, abstract, abstract_truncated)
    source_url = record.get("html_url") or record.get("pdf_url") or f"{FEDERAL_REGISTER}/{document_number}"
    return {
        "source": SOURCE, "external_id": f"fr-{document_number}",
        "kind": "political_event",
        "description": description[:4096], "actor": actor_key,
        "counterparty": "", "instrument": "",
        "action": "", "reason": "", "reason_basis": "unknown",
        "amount": None, "unit": "", "currency": "",
        "basis": "observed", "occurred_at": publication_date,
        "observed_at": retrieved_at, "available_at": retrieved_at,
        "source_url": source_url, "evidence": evidence,
    }


def fetch_political(conn, limit=MAX_LIMIT_DEFAULT, start=None, end=None, category=None):
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be an integer from 1 to {MAX_LIMIT}")
    today = clock.now().date()
    finish = _iso_date(end, "end") if end is not None else today
    begin = _iso_date(start, "start") if start is not None else finish - timedelta(days=365)
    if begin > finish:
        raise ValueError("start must not be after end")
    started = clock.now()
    client = HTTPClient(ttl=0)
    query_params = {"conditions[publication_date][gte]": begin.isoformat(),
                       "conditions[publication_date][lte]": finish.isoformat(),
                       "per_page": limit, "order": "newest"}
    if category:
        query_params["conditions[type][]"] = category
    query = urlencode(query_params, doseq=True)
    url = f"{FEDERAL_REGISTER}?{query}"
    try:
        payload = client.get_json(url)
    except (SourceError, ValueError, TypeError) as exc:
        raise SourceError(f"political-event: Federal Register fetch failed; "
                          f"no writes applied. {exc}") from exc
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        raise SourceError("political-event: unexpected Federal Register response")
    retrieved = clock.now().replace(microsecond=0).isoformat()
    rows, skipped = [], 0
    for record in results:
        if not isinstance(record, dict):
            skipped += 1
            continue
        observation = _observation(record, retrieved)
        if observation is None:
            skipped += 1
            continue
        rows.append(observation)
    stored = observations.store_observations(conn, rows)
    finished = clock.now()
    canonical = json.dumps({"endpoint": "federal_register/documents",
                               "filters": query_params, "limit": limit},
                          sort_keys=True, ensure_ascii=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    warnings = list(client.warnings)
    truncated = len(results) >= limit
    if truncated:
        warnings.append("the page returned the full limit; older documents may be absent")
    registry.record_ingest_run(
        conn, SOURCE, started.isoformat(), finished.isoformat(),
        query=f"{begin.isoformat()}..{finish.isoformat()}",
        fetched=len(results), stored=stored["imported"], skipped=skipped,
        pages=1, truncated=truncated, request_sha256=hashlib.sha256(canonical).hexdigest(),
        warnings=warnings,
        coverage=f"political/regulatory events {begin.isoformat()}..{finish.isoformat()}")
    return {
        "source": SOURCE, "endpoint": "federal_register/documents", "source_url": url,
        "retrieved_at": client.retrieved_at,
        "period": {"start": begin.isoformat(), "end": finish.isoformat()},
        "records": len(results), "observations": stored["imported"],
        "skipped": skipped, "by_kind": stored["kinds"], "truncated": truncated,
        "warnings": warnings,
        "limitations": [
            "The Federal Register provides government publications (regulations, "
            "notices, presidential documents) as the primary structured source for "
            "political/regulatory events; this captures documented government actions, "
            "not political intent or unreported motives.",
            "Only one bounded page is queried; amounts are not applicable since "
            "political events are measured as documented actions, not monetary transfers.",
            "occurred_at is the publication date and available_at is the retrieval time; "
            "the Federal Register does not expose a per-document action timestamp.",
            "The kind label is derived from the document type field; some types may "
            "map to political_event rather than regulatory_action if the type "
            "classification is ambiguous.",
        ],
    }
