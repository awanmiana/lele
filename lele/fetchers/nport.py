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
from .form4 import _accepted, _cik_value, _decimal, _entity_cik, _local
from .http import HTTPClient, SourceError

MAX_FILINGS_DEFAULT = 1
MAX_FILINGS = 20
MAX_SUBMISSIONS_BYTES = 32 * 1024 * 1024
MAX_DOC_BYTES = 32 * 1024 * 1024
MAX_HOLDINGS = 5000
SOURCE = "sec-nport"
ACCESSION_RE = re.compile(r"[0-9]{10}-[0-9]{2}-[0-9]{6}")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
FILES = ("form", "filingDate", "acceptanceDateTime", "accessionNumber", "primaryDocument")


def _iter_local(node, name):
    for child in node.iter():
        if _local(child.tag) == name:
            yield child


def _find_text(root, name):
    for child in _iter_local(root, name):
        if isinstance(child.text, str) and child.text.strip():
            return child.text.strip()
    return ""


def _find_attr(root, name, attribute):
    for child in _iter_local(root, name):
        value = child.get(attribute)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


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
    columns = [recent.get(field) for field in FILES]
    if any(not isinstance(column, list) for column in columns):
        raise ValueError("filing arrays must be arrays")
    arrays = [column for column in columns if isinstance(column, list)]
    if len({len(column) for column in arrays}) != 1:
        raise ValueError("filing arrays must have equal lengths")
    filings, skipped = [], 0
    for form, filed, accepted, accession, document in zip(*arrays):
        if form == "NPORT-P/A":
            skipped += 1
            continue
        if form != "NPORT-P":
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


def parse_nport(raw, accession, accepted, source_url):
    if len(raw) > MAX_DOC_BYTES:
        raise ValueError("N-PORT document exceeds the 32 MiB limit")
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("N-PORT document declares a DTD or entity; refused")
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise ValueError("N-PORT document is not valid XML") from exc
    if _local(root.tag) != "edgarSubmission":
        raise ValueError("N-PORT root element must be edgarSubmission")
    if _find_text(root, "submissionType") not in ("NPORT-P", "NPORT-P/A"):
        raise ValueError("document is not Form N-PORT")
    if not ACCESSION_RE.fullmatch(accession):
        raise ValueError("invalid accession number")
    reg_name = _find_text(root, "regName")
    series_name = _find_text(root, "seriesName")
    series_lei = _find_text(root, "seriesLei")
    period = _date_or_none(_find_text(root, "repPdDate"))
    totals = {
        "tot_assets": _find_text(root, "totAssets"),
        "tot_liabs": _find_text(root, "totLiabs"),
        "net_assets": _find_text(root, "netAssets"),
    }
    context = {"accession": accession, "period": period, "reg_name": reg_name,
               "series_name": series_name, "series_lei": series_lei, **totals}
    items: list[dict] = []
    skipped, truncated = 0, False
    for index, sec in enumerate(_iter_local(root, "invstOrSec"), start=1):
        if len(items) >= MAX_HOLDINGS:
            truncated = True
            break
        name = _find_text(sec, "name")
        value = _decimal(_find_text(sec, "valUSD"))
        if not name and value is None:
            skipped += 1
            continue
        cusip = _find_text(sec, "cusip")
        isin = _find_attr(sec, "isin", "value")
        lei = _find_text(sec, "lei")
        title = _find_text(sec, "title")
        balance = _decimal(_find_text(sec, "balance"))
        units = _find_text(sec, "units")
        currency = _find_text(sec, "curCd") or "USD"
        pct_val = _find_text(sec, "pctVal")
        evidence = json.dumps({
            "fund": context, "name": name, "title": title, "lei": lei, "cusip": cusip,
            "isin": isin, "balance": str(balance) if balance is not None else None, "units": units,
            "value_usd": str(value) if value is not None else None, "pct_val": pct_val,
            "payoff_profile": _find_text(sec, "payoffProfile"),
            "asset_cat": _find_text(sec, "assetCat"),
            "issuer_cat": _find_text(sec, "issuerCat"),
            "country": _find_text(sec, "invCountry"),
            "fair_val_level": _find_text(sec, "fairValLevel"),
        }, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        amount = str(value) if value is not None else None
        description = (f"N-PORT holding: {name or cusip or 'security'} ({title or 'class unknown'}) "
                       f"balance {balance if balance is not None else 'unknown'} {units or ''}")
        if amount is not None:
            description += f", value {amount} USD"
        items.append({
            "source": SOURCE, "external_id": f"{accession}:{index:05d}", "kind": "holding",
            "description": description[:4096], "actor": "", "counterparty": "", "instrument": "",
            "action": "", "reason": "", "reason_basis": "unknown", "amount": amount,
            "unit": "currency" if amount is not None else "",
            "currency": currency if amount is not None else "", "basis": "observed",
            "occurred_at": period, "observed_at": accepted, "available_at": accepted,
            "source_url": source_url, "evidence": evidence,
        })
    return items, context, {"skipped": skipped, "truncated": truncated}


def fetch_nport(conn, entity_id, limit=MAX_FILINGS_DEFAULT):
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
        raise SourceError(f"sec-nport: submissions fetch failed; no writes applied. {exc}") from exc
    warnings = list(client.warnings)
    if malformed:
        warnings.append(f"{malformed} N-PORT filing row(s) were skipped (amendments or malformed)")
    rows, truncated = [], False
    for filing in filings:
        temporary = None
        try:
            descriptor, path = tempfile.mkstemp(suffix=".xml")
            os.close(descriptor)
            temporary = path
            client.download(filing["url"], path, MAX_DOC_BYTES)
            with open(path, "rb") as stream:
                raw = stream.read(MAX_DOC_BYTES + 1)
            items, context, item_skipped = parse_nport(raw, filing["accession"],
                                                       filing["accepted"], filing["url"])
            for item in items:
                item["actor"] = entity["key"]
            rows.extend(items)
            truncated = truncated or item_skipped["truncated"]
            if item_skipped["skipped"]:
                warnings.append(f"{filing['accession']}: {item_skipped['skipped']} holding(s) "
                                "lacked a name and value and were skipped")
            if context["period"] is None:
                warnings.append(f"{filing['accession']}: no report period parsed")
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
    registry.record_ingest_run(
        conn, SOURCE, started.isoformat(), finished.isoformat(), query=cik,
        fetched=len(filings), stored=result["imported"], skipped=malformed,
        pages=1 + len(filings), truncated=truncated or len(filings) >= limit,
        request_sha256=hashlib.sha256(canonical).hexdigest(), warnings=warnings,
        coverage="recent public NPORT-P filings only; NPORT-P/A amendments are not applied",
        pages_detail=[{"page": 1, "offset": 0, "rows": len(filings), "selected": len(filings)}])
    return {
        "source": SOURCE, "entity_id": entity_id, "entity_key": entity["key"], "cik": cik,
        "source_url": url, "retrieved_at": client.retrieved_at, "filings": len(filings),
        "observations": result["imported"], "by_kind": result["kinds"],
        "skipped": {"filings": malformed}, "truncated": truncated or len(filings) >= limit,
        "warnings": warnings,
        "limitations": [
            "Only the fund's recent public NPORT-P filings are read; confidential NPORT-NP data and "
            "NPORT-P/A amendments are not applied.",
            "Each holding is a position snapshot bound to the filing fund as actor; the security is "
            "recorded by name/LEI/CUSIP/ISIN only, so no issuer instrument is inferred or bound.",
            "Reported value and percentage are the filer's figures; positions are not money flows and "
            "are not summed across funds or periods.",
            "available_at is the filing acceptance time; occurred_at is the report period date. No "
            "ownership-delta, net-flow or motive is computed here.",
        ],
    }
