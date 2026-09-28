import hashlib
import json
import re
from ..core import clock
from urllib.parse import quote

from ..analysis import observations
from ..core import registry
from ..core.constants import SOURCES
from .form4 import _accepted, _cik_value, _entity_cik
from .http import HTTPClient, SourceError

MAX_FILINGS_DEFAULT = 25
MAX_FILINGS = 200
MAX_SUBMISSIONS_BYTES = 8 * 1024 * 1024
SOURCE = "sec-material"
ACCESSION_RE = re.compile(r"[0-9]{10}-[0-9]{2}-[0-9]{6}")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
EXACT_FORMS = frozenset({"8-K", "8-K/A", "S-1", "S-1/A", "DEF 14A", "DEFA14A"})
FORM_FIELDS = ("form", "filingDate", "reportDate", "acceptanceDateTime", "accessionNumber",
               "primaryDocument", "primaryDocDescription", "items")


def _is_material(form):
    return form in EXACT_FORMS or (isinstance(form, str) and form.startswith("424B"))


def _date_or_none(value):
    return value if isinstance(value, str) and DATE_RE.fullmatch(value) else None


def _filings(payload, cik, limit):
    if not isinstance(payload, dict) or payload.get("error") or payload.get("errors"):
        raise ValueError("expected submissions object")
    if _cik_value(payload.get("cik")) != int(cik):
        raise ValueError("submissions CIK mismatch")
    recent = payload["filings"]["recent"]
    if not isinstance(recent, dict):
        raise ValueError("invalid filings object")
    columns = [recent.get(field) for field in FORM_FIELDS]
    if any(not isinstance(column, list) for column in columns):
        raise ValueError("filing arrays must be arrays")
    arrays = [column for column in columns if isinstance(column, list)]
    if len({len(column) for column in arrays}) != 1:
        raise ValueError("filing arrays must have equal lengths")
    filings, skipped = [], 0
    for form, filed, report, accepted, accession, document, label, items in zip(*arrays):
        if not _is_material(form):
            continue
        segments = document.split("/") if isinstance(document, str) else []
        if (not isinstance(accession, str) or not ACCESSION_RE.fullmatch(accession)
                or not segments or not all(segments)
                or any(segment in (".", "..") for segment in segments)
                or "\\" in document or any(ord(c) < 32 for c in document)):
            skipped += 1
            continue
        stamp = _accepted(accepted)
        if stamp is None:
            skipped += 1
            continue
        raw_document = segments[-1]
        url = (f"{SOURCES['SEC_ARCHIVES']}/{int(cik)}/{accession.replace('-', '')}/"
               f"{quote(raw_document, safe='.-_')}")
        filings.append({"form": form, "filed": _date_or_none(filed), "report": _date_or_none(report),
                        "accepted": stamp, "accession": accession, "document": raw_document,
                        "description": label if isinstance(label, str) else "",
                        "items": items if isinstance(items, str) else "", "url": url})
    filings.sort(key=lambda row: (row["accepted"], row["accession"]), reverse=True)
    return filings[:limit], skipped


def _observation(filing, entity_key):
    description = f"{filing['form']} filed {filing['filed'] or filing['accepted'][:10]}"
    if filing["description"]:
        description += f": {filing['description']}"
    if filing["items"]:
        description += f" [8-K items {filing['items']}]"
    evidence = json.dumps({
        "form": filing["form"], "accession": filing["accession"], "filing_date": filing["filed"],
        "report_date": filing["report"], "items": filing["items"],
        "primary_doc_description": filing["description"], "document": filing["document"],
    }, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return {
        "source": SOURCE, "external_id": filing["accession"], "kind": "filing_event",
        "description": description[:4096], "actor": entity_key, "counterparty": "",
        "instrument": entity_key, "action": "", "reason": "", "reason_basis": "unknown",
        "amount": None, "unit": "", "currency": "", "basis": "observed",
        "occurred_at": filing["report"] or filing["filed"], "observed_at": filing["accepted"],
        "available_at": filing["accepted"], "source_url": filing["url"], "evidence": evidence,
    }


def fetch_material(conn, entity_id, limit=MAX_FILINGS_DEFAULT):
    if type(entity_id) is not int or not 1 <= entity_id <= 2**63 - 1:
        raise ValueError("entity id must be a positive SQLite integer")
    if type(limit) is not int or not 1 <= limit <= MAX_FILINGS:
        raise ValueError(f"limit must be an integer from 1 to {MAX_FILINGS}")
    entity = registry.get_entity(conn, entity_id)
    if entity is None:
        raise ValueError(f"entity {entity_id} not found")
    cik = _entity_cik(conn, entity)
    started = clock.now()
    client = HTTPClient(ttl=0, max_bytes=MAX_SUBMISSIONS_BYTES)
    url = f"{SOURCES['SEC_SUBMISSIONS']}/CIK{cik}.json"
    try:
        payload = client.get_json(url)
        filings, malformed = _filings(payload, cik, limit)
    except (SourceError, KeyError, TypeError, ValueError, OverflowError) as exc:
        raise SourceError(f"sec-material: submissions fetch failed; no writes applied. {exc}") from exc
    warnings = list(client.warnings)
    if malformed:
        warnings.append(f"{malformed} material filing row(s) were malformed and skipped")
    rows = [_observation(filing, entity["key"]) for filing in filings]
    result = observations.store_observations(conn, rows)
    finished = clock.now()
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"),
                           allow_nan=False).encode("utf-8")
    registry.record_ingest_run(
        conn, SOURCE, started.isoformat(), finished.isoformat(), query=cik,
        fetched=len(filings), stored=result["imported"], skipped=malformed, pages=1,
        truncated=len(filings) >= limit, request_sha256=hashlib.sha256(canonical).hexdigest(),
        warnings=warnings, coverage="recent submissions only; filing contents are not parsed",
        pages_detail=[{"page": 1, "offset": 0, "rows": len(filings), "selected": len(filings)}])
    return {
        "source": SOURCE, "entity_id": entity_id, "entity_key": entity["key"], "cik": cik,
        "source_url": url, "retrieved_at": client.retrieved_at, "filings": len(filings),
        "observations": result["imported"], "by_kind": result["kinds"],
        "skipped": {"malformed": malformed}, "truncated": len(filings) >= limit, "warnings": warnings,
        "limitations": [
            "Only the issuer's recent submissions metadata is read; the filing contents, exhibits and "
            "amendment text are not downloaded or interpreted.",
            "Each filing is one measurement-optional filing_event observation with its form, dates, "
            "accession, document URL and 8-K item numbers; it carries no money amount.",
            "Amendments (for example 8-K/A, S-1/A) are separate events and are not merged with the "
            "original filing; names or motives are never inferred.",
            "available_at is the filing acceptance time; occurred_at is the report date when present, "
            "otherwise the filing date.",
        ],
    }
