import hashlib
import json
import os
import re
import tempfile
from ..core import clock
from urllib.parse import quote
from xml.etree import ElementTree

from ..analysis import observations
from ..core import registry
from ..core.constants import SOURCES
from .form4 import _accepted, _cik_value, _decimal, _entity_cik, _first, _local, _path_text
from .http import HTTPClient, SourceError

MAX_FILINGS_DEFAULT = 10
MAX_FILINGS = 200
MAX_SUBMISSIONS_BYTES = 8 * 1024 * 1024
MAX_DOC_BYTES = 1_000_000
MAX_RELATED_PERSONS = 50
SOURCE = "sec-formd"
ACCESSION_RE = re.compile(r"[0-9]{10}-[0-9]{2}-[0-9]{6}")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
FILES = ("form", "filingDate", "acceptanceDateTime", "accessionNumber", "primaryDocument")


def _date_or_none(value):
    return value if isinstance(value, str) and DATE_RE.fullmatch(value) else None


def _text_list(node, child):
    if node is None:
        return []
    return [_path_text(item) for item in node if _local(item.tag) == child and
            _path_text(item)]


def _filings(payload, cik, limit):
    if not isinstance(payload, dict) or payload.get("error") or payload.get("errors"):
        raise ValueError("expected submissions object")
    if _cik_value(payload.get("cik")) != int(cik):
        raise ValueError("submissions CIK mismatch")
    recent = payload["filings"]["recent"]
    if not isinstance(recent, dict):
        raise ValueError("invalid filings object")
    columns = [recent.get(field) for field in FILES]
    if any(not isinstance(column, list) for column in columns):
        raise ValueError("filing arrays must be arrays")
    arrays = [column for column in columns if isinstance(column, list)]
    if len({len(column) for column in arrays}) != 1:
        raise ValueError("filing arrays must have equal lengths")
    filings, skipped = [], 0
    for form, filed, accepted, accession, document in zip(*arrays):
        if form not in ("D", "D/A"):
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
        filings.append({"form": form, "filed": _date_or_none(filed), "accepted": stamp,
                        "accession": accession, "document": raw_document, "url": url})
    filings.sort(key=lambda row: (row["accepted"], row["accession"]), reverse=True)
    return filings[:limit], skipped


def parse_formd(raw, accession, accepted, source_url):
    if len(raw) > MAX_DOC_BYTES:
        raise ValueError("Form D document exceeds the 1 MiB limit")
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("Form D document declares a DTD or entity; refused")
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise ValueError("Form D document is not valid XML") from exc
    if _local(root.tag) != "edgarSubmission":
        raise ValueError("Form D root element must be edgarSubmission")
    submission_type = _path_text(root, "submissionType")
    if submission_type not in ("D", "D/A"):
        raise ValueError("document is not Form D")
    if not ACCESSION_RE.fullmatch(accession):
        raise ValueError("invalid accession number")
    issuer = _first(root, "primaryIssuer")
    offering = _first(root, "offeringData")
    if issuer is None or offering is None:
        raise ValueError("Form D is missing primaryIssuer or offeringData")
    total_offering = _decimal(_path_text(offering, "offeringSalesAmounts", "totalOfferingAmount"))
    total_sold = _decimal(_path_text(offering, "offeringSalesAmounts", "totalAmountSold"))
    total_remaining = _decimal(_path_text(offering, "offeringSalesAmounts", "totalRemaining"))
    commissions = _decimal(_path_text(offering, "salesCommissionsFindersFees", "salesCommissions",
                                      "dollarAmount"))
    finders = _decimal(_path_text(offering, "salesCommissionsFindersFees", "findersFees",
                                  "dollarAmount"))
    proceeds_used = _decimal(_path_text(offering, "useOfProceeds", "grossProceedsUsed",
                                        "dollarAmount"))
    related: list[dict] = []
    related_list = _first(root, "relatedPersonsList")
    if related_list is not None:
        for info in related_list:
            if _local(info.tag) != "relatedPersonInfo" or len(related) >= MAX_RELATED_PERSONS:
                continue
            name = " ".join(part for part in (
                _path_text(info, "relatedPersonName", "firstName"),
                _path_text(info, "relatedPersonName", "middleName"),
                _path_text(info, "relatedPersonName", "lastName")) if part)
            relationships = _text_list(_first(info, "relatedPersonRelationshipList"), "relationship")
            if name or relationships:
                related.append({"name": name, "relationships": relationships})
    first_sale = _date_or_none(_path_text(offering, "typeOfFiling", "dateOfFirstSale", "value"))
    signature_date = _date_or_none(_path_text(offering, "signatureBlock", "signature",
                                              "signatureDate"))
    amount = str(total_sold) if total_sold is not None and total_sold > 0 else None
    entity_name = _path_text(issuer, "entityName")
    description = f"Form D {submission_type}: {entity_name or 'issuer'}"
    if amount is not None:
        description += f" sold {amount} USD"
    if total_offering is not None:
        description += f" of {total_offering} USD offered"
    if related:
        description += f" ({len(related)} related person(s))"
    evidence = json.dumps({
        "submission_type": submission_type, "accession": accession,
        "is_amendment": _path_text(offering, "typeOfFiling", "newOrAmendment", "isAmendment"),
        "entity_type": _path_text(issuer, "entityType"),
        "jurisdiction_of_inc": _path_text(issuer, "jurisdictionOfInc"),
        "industry_group": _path_text(offering, "industryGroup", "industryGroupType"),
        "revenue_range": _path_text(offering, "issuerSize", "revenueRange"),
        "exemptions": _text_list(_first(offering, "federalExemptionsExclusions"), "item"),
        "date_of_first_sale": first_sale, "signature_date": signature_date,
        "total_offering_amount_usd": str(total_offering) if total_offering is not None else None,
        "total_amount_sold_usd": amount,
        "total_remaining_usd": str(total_remaining) if total_remaining is not None else None,
        "investors": _path_text(offering, "investors", "totalNumberAlreadyInvested"),
        "sales_commissions_usd": str(commissions) if commissions is not None else None,
        "finders_fees_usd": str(finders) if finders is not None else None,
        "gross_proceeds_used_usd": str(proceeds_used) if proceeds_used is not None else None,
        "related_persons": related,
    }, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return {
        "source": SOURCE, "external_id": accession, "kind": "filing_event",
        "description": description[:4096], "actor": "", "counterparty": "", "instrument": "",
        "action": "", "reason": "", "reason_basis": "unknown", "amount": amount,
        "unit": "currency" if amount is not None else "", "currency": "USD" if amount else "",
        "basis": "observed", "occurred_at": first_sale or signature_date or None,
        "observed_at": accepted, "available_at": accepted, "source_url": source_url,
        "evidence": evidence,
    }


def fetch_formd(conn, entity_id, limit=MAX_FILINGS_DEFAULT):
    if type(entity_id) is not int or not 1 <= entity_id <= 2**63 - 1:
        raise ValueError("entity id must be a positive SQLite integer")
    if type(limit) is not int or not 1 <= limit <= MAX_FILINGS:
        raise ValueError(f"limit must be an integer from 1 to {MAX_FILINGS}")
    entity = registry.get_entity(conn, entity_id)
    if entity is None:
        raise ValueError(f"entity {entity_id} not found")
    cik = _entity_cik(conn, entity)
    started = clock.now()
    registry.record_failures(conn, source=SOURCE, started=started, query=cik)
    client = HTTPClient(ttl=0, max_bytes=MAX_SUBMISSIONS_BYTES)
    url = f"{SOURCES['SEC_SUBMISSIONS']}/CIK{cik}.json"
    try:
        payload = client.get_json(url)
        filings, malformed = _filings(payload, cik, limit)
    except (SourceError, KeyError, TypeError, ValueError, OverflowError) as exc:
        raise SourceError(f"sec-formd: submissions fetch failed; no writes applied. {exc}") from exc
    warnings = list(client.warnings)
    if malformed:
        warnings.append(f"{malformed} Form D filing row(s) were malformed and skipped")
    rows = []
    for filing in filings:
        temporary = None
        try:
            descriptor, path = tempfile.mkstemp(suffix=".xml")
            os.close(descriptor)
            temporary = path
            client.download(filing["url"], path, MAX_DOC_BYTES)
            with open(path, "rb") as stream:
                raw = stream.read(MAX_DOC_BYTES + 1)
            observation = parse_formd(raw, filing["accession"], filing["accepted"], filing["url"])
            observation["actor"] = entity["key"]
            observation["instrument"] = entity["key"]
            rows.append(observation)
        except (SourceError, OSError, ValueError) as exc:
            malformed += 1
            warnings.append(f"{filing['accession']}: {exc}")
        finally:
            if temporary is not None and os.path.exists(temporary):
                os.unlink(temporary)
    result = observations.store_observations(conn, rows)
    finished = clock.now()
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"),
                           allow_nan=False).encode("utf-8")
    registry.set_payload_fingerprint(conn, client)
    registry.clear_failure_recorder(conn)
    registry.record_ingest_run(
        conn, SOURCE, started.isoformat(), finished.isoformat(), query=cik,
        fetched=len(filings), stored=result["imported"], skipped=malformed,
        pages=1 + len(filings), truncated=len(filings) >= limit,
        request_sha256=hashlib.sha256(canonical).hexdigest(), warnings=warnings,
        coverage="recent submissions only; Form D/A amendments are separate events",
        pages_detail=[{"page": 1, "offset": 0, "rows": len(filings), "selected": len(filings)}])
    return {
        "source": SOURCE, "entity_id": entity_id, "entity_key": entity["key"], "cik": cik,
        "source_url": url, "retrieved_at": client.retrieved_at, "filings": len(filings),
        "observations": result["imported"], "by_kind": result["kinds"],
        "skipped": {"malformed": malformed}, "truncated": len(filings) >= limit, "warnings": warnings,
        "limitations": [
            "Only the issuer's recent submissions are read; the Form D XML cover page is parsed but "
            "exhibits, signatures and offering documents are not.",
            "Each filing is one measurement-optional filing_event observation with the reported "
            "total amount sold (USD) when present; Form D/A amendments are separate events and are "
            "not merged, so amounts must not be summed across a filing and its amendments.",
            "The reported amount is the filer's own figure and an offering fact, not a settled money "
            "flow; investors, counterparties and motives are not identified.",
            "available_at is the filing acceptance time; occurred_at is the first-sale date when "
            "present, otherwise the signature date.",
        ],
    }
