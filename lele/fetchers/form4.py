import hashlib
import json
import os
import re
import tempfile
from ..core import clock
from datetime import datetime, UTC
from decimal import Decimal, InvalidOperation
from urllib.parse import quote
from xml.etree import ElementTree

from ..analysis import observations
from ..core import importer, registry
from ..core.constants import SOURCES
from .http import HTTPClient, SourceError

MAX_FILINGS_DEFAULT = 25
MAX_FILINGS = 200
MAX_XML_BYTES = 1_000_000
ACCESSION_RE = re.compile(r"[0-9]{10}-[0-9]{2}-[0-9]{6}")
ACCEPTED_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})")
SOURCE = "sec-form4"
CODE_LABELS = {
    "P": "open-market purchase", "S": "open-market sale", "A": "grant or award",
    "M": "option exercise", "F": "tax withholding", "G": "bona fide gift",
    "C": "conversion", "D": "disposition", "X": "option exercise",
}


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _first(node, name):
    if node is None:
        return None
    for child in node:
        if _local(child.tag) == name:
            return child
    return None


def _path_text(node, *names):
    current = node
    for name in names:
        current = _first(current, name)
    if current is None or not isinstance(current.text, str):
        return ""
    return current.text.strip()


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


def _cik_value(value):
    if type(value) is int:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,10}", value.strip()):
        return int(value.strip())
    return None


def _accepted(value):
    if not isinstance(value, str) or not ACCEPTED_RE.fullmatch(value):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp.astimezone(UTC).replace(microsecond=0).isoformat()


def _entity_cik(conn, entity):
    key = entity["key"]
    if re.fullmatch(r"cik:[0-9]{10}", key):
        return key[4:]
    row = conn.execute("SELECT v FROM attributes WHERE entity_id=? AND k='sec.cik'",
                       (entity["id"],)).fetchone()
    if row is not None and re.fullmatch(r"[0-9]{10}", row[0] or ""):
        return row[0]
    raise ValueError("entity is not an SEC issuer with a 10-digit CIK; fetch it from sec first")


def _filings(payload, cik, limit):
    if not isinstance(payload, dict) or payload.get("error") or payload.get("errors"):
        raise ValueError("expected submissions object")
    if _cik_value(payload.get("cik")) != int(cik):
        raise ValueError("submissions CIK mismatch")
    recent = payload["filings"]["recent"]
    if not isinstance(recent, dict):
        raise ValueError("invalid filings object")
    fields = ("form", "filingDate", "acceptanceDateTime", "accessionNumber", "primaryDocument")
    columns = [recent.get(field) for field in fields]
    if any(not isinstance(column, list) for column in columns):
        raise ValueError("filing arrays must be arrays")
    arrays = [column for column in columns if isinstance(column, list)]
    if len({len(column) for column in arrays}) != 1:
        raise ValueError("filing arrays must have equal lengths")
    filings = []
    for form, filed, accepted, accession, document in zip(*arrays):
        if form != "4":
            continue
        segments = document.split("/") if isinstance(document, str) else []
        if (not isinstance(accession, str) or not ACCESSION_RE.fullmatch(accession)
                or not segments or not all(segments)
                or any(segment in (".", "..") for segment in segments)
                or "\\" in document or any(ord(c) < 32 for c in document)):
            raise ValueError("invalid Form 4 filing row")
        stamp = _accepted(accepted)
        if stamp is None:
            raise ValueError("Form 4 filing lacks a usable acceptance time")
        raw_document = segments[-1]
        url = (f"{SOURCES['SEC_ARCHIVES']}/{int(cik)}/{accession.replace('-', '')}/"
               f"{quote(raw_document, safe='.-_')}")
        filings.append({"form": form, "accession": accession, "document": raw_document,
                        "url": url, "accepted": stamp, "filed": filed})
    filings.sort(key=lambda row: (row["accepted"], row["accession"]), reverse=True)
    return filings[:limit]


def parse_form4(raw, accession, accepted, source_url):
    if not isinstance(raw, bytes):
        raise ValueError("Form 4 document must be bytes")
    if len(raw) > MAX_XML_BYTES:
        raise ValueError("Form 4 document exceeds the 1 MiB limit")
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("Form 4 document declares a DTD or entity; refused")
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise ValueError("Form 4 document is not valid XML") from exc
    if _local(root.tag) != "ownershipDocument":
        raise ValueError("Form 4 root element must be ownershipDocument")
    document_type = _path_text(root, "documentType")
    if document_type not in ("4", "4/A"):
        raise ValueError("document is not Form 4")
    if not ACCESSION_RE.fullmatch(accession):
        raise ValueError("invalid accession number")
    importer._provenance(source_url, "Form 4 source_url")
    period = _path_text(root, "periodOfReport")
    owner_cik = _path_text(root, "reportingOwner", "reportingOwnerId", "rptOwnerCik")
    owner_name = _path_text(root, "reportingOwner", "reportingOwnerId", "rptOwnerName")
    relationship = _first(_first(root, "reportingOwner"), "reportingOwnerRelationship")
    flags = {
        "is_director": _path_text(relationship, "isDirector"),
        "is_officer": _path_text(relationship, "isOfficer"),
        "officer_title": _path_text(relationship, "officerTitle"),
        "is_ten_percent_owner": _path_text(relationship, "isTenPercentOwner"),
        "is_other": _path_text(relationship, "isOther"),
    }
    if owner_cik and not re.fullmatch(r"[0-9]{1,10}", owner_cik):
        owner_cik = ""
    actor_key = f"sec-owner:{int(owner_cik):010d}" if owner_cik else ""
    items, skipped = [], {"no_shares": 0, "no_direction": 0, "invalid": 0}
    index = 0
    for table in ("nonDerivativeTable", "derivativeTable"):
        container = _first(root, table)
        if container is None:
            continue
        for transaction in container:
            if _local(transaction.tag) not in ("nonDerivativeTransaction", "derivativeTransaction"):
                continue
            index += 1
            table_name = "nonDerivative" if table.startswith("non") else "derivative"
            shares = _decimal(_path_text(transaction, "transactionAmounts", "transactionShares",
                                         "value"))
            direction = _path_text(transaction, "transactionAmounts",
                                   "transactionAcquiredDisposedCode", "value").upper()
            if shares is None or shares <= 0:
                skipped["no_shares"] += 1
                continue
            if direction not in ("A", "D"):
                skipped["no_direction"] += 1
                continue
            code = _path_text(transaction, "transactionCoding", "transactionCode").upper()
            amount = shares if direction == "A" else -shares
            transaction_date = _path_text(transaction, "transactionDate", "value")
            security = _path_text(transaction, "securityTitle", "value")
            price = _decimal(_path_text(transaction, "transactionAmounts",
                                        "transactionPricePerShare", "value"))
            owned_after = _decimal(_path_text(transaction, "postTransactionAmounts",
                                              "sharesOwnedFollowingTransaction", "value"))
            ownership = _path_text(transaction, "ownershipNature",
                                   "directOrIndirectOwnership", "value")
            evidence = json.dumps({
                "accession": accession, "form": document_type, "table": table_name,
                "transaction_code": code, "price_per_share": str(price) if price is not None else None,
                "shares_owned_after": str(owned_after) if owned_after is not None else None,
                "ownership": ownership, "period_of_report": period,
                "transaction_date": transaction_date, **flags,
            }, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
            label = CODE_LABELS.get(code, f"transaction {code}" if code else "transaction")
            description = (f"{owner_name or 'Reporting owner'} {label}: {shares} {security or 'shares'}"
                           ).strip()
            items.append({
                "source": SOURCE,
                "external_id": f"{accession}:{owner_cik or 'unknown'}:{table_name}:{index}",
                "kind": "insider_trade", "description": description,
                "actor": actor_key, "counterparty": "", "instrument": "", "action": "",
                "reason": "", "reason_basis": "unknown", "amount": str(amount), "unit": "shares",
                "currency": "", "basis": "observed",
                "occurred_at": transaction_date or None, "observed_at": accepted,
                "available_at": accepted, "source_url": source_url, "evidence": evidence,
            })
    return items, skipped


def fetch_form4(conn, entity_id, limit=MAX_FILINGS_DEFAULT):
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
    client = HTTPClient(ttl=0)
    url = f"{SOURCES['SEC_SUBMISSIONS']}/CIK{cik}.json"
    try:
        payload = client.get_json(url)
        filings = _filings(payload, cik, limit)
    except (SourceError, KeyError, TypeError, ValueError, OverflowError) as exc:
        raise SourceError(f"sec-form4: submissions fetch failed; no writes applied. {exc}") from exc
    warnings = list(client.warnings)
    rows, skipped = [], {"no_shares": 0, "no_direction": 0, "invalid": 0}
    for filing in filings:
        temporary = None
        try:
            descriptor, path = tempfile.mkstemp(suffix=".xml")
            os.close(descriptor)
            temporary = path
            client.download(filing["url"], path, MAX_XML_BYTES)
            with open(path, "rb") as stream:
                raw = stream.read(MAX_XML_BYTES + 1)
            parsed, item_skipped = parse_form4(raw, filing["accession"], filing["accepted"],
                                               filing["url"])
            for key in skipped:
                skipped[key] += item_skipped[key]
            rows.extend(parsed)
        except (SourceError, OSError, ValueError) as exc:
            skipped["invalid"] += 1
            warnings.append(f"{filing['accession']}: {exc}")
        finally:
            if temporary is not None and os.path.exists(temporary):
                os.unlink(temporary)
    for row in rows:
        row["instrument"] = entity["key"]
    result = observations.store_observations(conn, rows)
    finished = clock.now()
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"),
                           allow_nan=False).encode("utf-8")
    registry.clear_failure_recorder(conn)
    registry.record_ingest_run(
        conn, SOURCE, started.isoformat(), finished.isoformat(), query=cik,
        fetched=len(filings), stored=result["imported"], skipped=sum(skipped.values()),
        pages=1 + len(filings), total=None, truncated=len(filings) >= limit,
        request_sha256=hashlib.sha256(canonical).hexdigest(),
        warnings=warnings, coverage="recent submissions only; Form 4 amendments are not applied",
        pages_detail=[{"page": 1, "offset": 0, "rows": len(filings), "selected": len(filings)}])
    return {
        "source": SOURCE, "entity_id": entity_id, "entity_key": entity["key"], "cik": cik,
        "source_url": url, "retrieved_at": client.retrieved_at,
        "filings": len(filings), "observations": result["imported"], "by_kind": result["kinds"],
        "skipped": skipped, "truncated": len(filings) >= limit, "warnings": warnings,
        "limitations": [
            "Only the issuer's recent submissions are read; historical submissions files are not "
            "fetched, so older Form 4 filings may be absent.",
            "Only original Form 4 filings are read; Form 4/A amendments are ignored and may correct "
            "or restate a transaction.",
            "Transaction direction is the filed acquired/disposed code (A positive, D negative) in "
            "shares. Codes are preserved in evidence; direction is an assumption, not a forecast.",
            "Reporting owners are recorded as sec-owner:<CIK> keys without creating person entities; "
            "unknown or missing identifiers stay empty.",
            "available_at is the filing acceptance time; transaction occurred_at is the trade date.",
            "No counterparty, motive or aggregated net buy/sell ratio is asserted here.",
        ],
    }
