import hashlib
import json
import os
import re
import tempfile
from ..core import clock
from datetime import datetime
from urllib.parse import quote
from xml.etree import ElementTree

from ..analysis import observations
from ..core import registry
from ..core.constants import SOURCES
from .form4 import (_accepted, _cik_value, _decimal, _entity_cik, _local, _path_text)
from .http import HTTPClient, SourceError

MAX_FILINGS_DEFAULT = 4
MAX_FILINGS = 40
MAX_SUBMISSIONS_BYTES = 8 * 1024 * 1024
MAX_INFO_BYTES = 16 * 1024 * 1024
MAX_COVER_BYTES = 262_144
SOURCE = "sec-13f"
ACCESSION_RE = re.compile(r"[0-9]{10}-[0-9]{2}-[0-9]{6}")


def _filings(payload, cik, limit):
    if not isinstance(payload, dict) or payload.get("error") or payload.get("errors"):
        raise ValueError("expected submissions object")
    if _cik_value(payload.get("cik")) != int(cik):
        raise ValueError("submissions CIK mismatch")
    recent = payload["filings"]["recent"]
    if not isinstance(recent, dict):
        raise ValueError("invalid filings object")
    fields = ("form", "filingDate", "acceptanceDateTime", "accessionNumber")
    columns = [recent.get(field) for field in fields]
    if any(not isinstance(column, list) for column in columns):
        raise ValueError("filing arrays must be arrays")
    arrays = [column for column in columns if isinstance(column, list)]
    if len({len(column) for column in arrays}) != 1:
        raise ValueError("filing arrays must have equal lengths")
    filings, amendments = [], 0
    for form, filed, accepted, accession in zip(*arrays):
        if form == "13F-HR/A":
            amendments += 1
            continue
        if form != "13F-HR":
            continue
        if not isinstance(accession, str) or not ACCESSION_RE.fullmatch(accession):
            raise ValueError("invalid Form 13F filing row")
        stamp = _accepted(accepted)
        if stamp is None:
            raise ValueError("Form 13F filing lacks a usable acceptance time")
        filings.append({"accession": accession, "accepted": stamp, "filed": filed})
    filings.sort(key=lambda row: (row["accepted"], row["accession"]), reverse=True)
    return filings[:limit], amendments


def _period(text):
    if not isinstance(text, str):
        return ""
    for pattern in ("%m-%d-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except (ValueError, TypeError):
            continue
    return ""


def parse_cover(raw):
    if len(raw) > MAX_COVER_BYTES:
        raise ValueError("Form 13F cover exceeds the 256 KiB limit")
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("Form 13F cover declares a DTD or entity; refused")
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise ValueError("Form 13F cover is not valid XML") from exc
    if _local(root.tag) != "edgarSubmission":
        raise ValueError("Form 13F cover root element must be edgarSubmission")
    period = _path_text(root, "formData", "coverPage", "reportCalendarOrQuarter")
    amendment = _path_text(root, "formData", "coverPage", "isAmendment")
    return {"period": _period(period), "amendment": amendment.lower() == "true"}


def parse_thirteenf(raw, accession, accepted, period, source_url, manager_key):
    if len(raw) > MAX_INFO_BYTES:
        raise ValueError("Form 13F information table exceeds the 16 MiB limit")
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("Form 13F information table declares a DTD or entity; refused")
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise ValueError("Form 13F information table is not valid XML") from exc
    if _local(root.tag) != "informationTable":
        raise ValueError("Form 13F information table root element is not informationTable")
    if not ACCESSION_RE.fullmatch(accession):
        raise ValueError("invalid accession number")
    items, skipped = [], {"no_value": 0, "invalid": 0}
    index = 0
    for entry in root:
        if _local(entry.tag) != "infoTable":
            continue
        index += 1
        issuer = _path_text(entry, "nameOfIssuer")
        value = _decimal(_path_text(entry, "value"))
        if not issuer or value is None or value <= 0:
            skipped["no_value"] += 1
            continue
        title = _path_text(entry, "titleOfClass")
        cusip = _path_text(entry, "cusip")
        shares = _decimal(_path_text(entry, "shrsOrPrnAmt", "sshPrnamt"))
        share_type = _path_text(entry, "shrsOrPrnAmt", "sshPrnamtType")
        put_call = _path_text(entry, "putCall")
        discretion = _path_text(entry, "investmentDiscretion")
        other_manager = _path_text(entry, "otherManager")
        voting = {
            "sole": _path_text(entry, "votingAuthority", "Sole"),
            "shared": _path_text(entry, "votingAuthority", "Shared"),
            "none": _path_text(entry, "votingAuthority", "None"),
        }
        evidence = json.dumps({
            "accession": accession, "period": period, "cusip": cusip, "issuer": issuer,
            "title_of_class": title, "shares": str(shares) if shares is not None else None,
            "share_type": share_type, "put_call": put_call, "investment_discretion": discretion,
            "other_manager": other_manager, "voting_authority": voting,
            "value_unit": "USD as reported by the filer",
        }, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        shares_text = str(shares) if shares is not None else "unknown"
        description = (f"Form 13F holding: {issuer} ({title or 'class unknown'}) "
                       f"shares {shares_text}, value {value} USD")
        items.append({
            "source": SOURCE,
            "external_id": f"{accession}:{index:05d}",
            "kind": "holding", "description": description[:4096],
            "actor": manager_key, "counterparty": "", "instrument": "", "action": "",
            "reason": "", "reason_basis": "unknown", "amount": str(value), "unit": "currency",
            "currency": "USD", "basis": "observed", "occurred_at": period or None,
            "observed_at": accepted, "available_at": accepted, "source_url": source_url,
            "evidence": evidence,
        })
    return items, skipped


def _information_table(client, base):
    index = client.get_json(base + "/index.json")
    items = index.get("directory", {}).get("item") if isinstance(index, dict) else None
    if not isinstance(items, list):
        raise ValueError("Form 13F filing index is invalid")
    candidates = []
    for item in items:
        name = item.get("name") if isinstance(item, dict) else None
        if (not isinstance(name, str) or not name.endswith(".xml") or name == "primary_doc.xml"
                or "/" in name or "\\" in name or name in (".", "..")):
            continue
        try:
            size = int(item.get("size", 0))
        except (TypeError, ValueError):
            size = 0
        candidates.append((size, name))
    if not candidates:
        raise ValueError("Form 13F filing has no information table XML")
    candidates.sort(reverse=True)
    return candidates[0][1]


def fetch_thirteenf(conn, entity_id, limit=MAX_FILINGS_DEFAULT):
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
        filings, amendments = _filings(payload, cik, limit)
    except (SourceError, KeyError, TypeError, ValueError, OverflowError) as exc:
        raise SourceError(f"sec-13f: submissions fetch failed; no writes applied. {exc}") from exc
    warnings = list(client.warnings)
    if amendments:
        warnings.append(f"{amendments} Form 13F-HR/A amendment(s) were not applied")
    rows, skipped = [], {"no_value": 0, "invalid": 0}
    for filing in filings:
        base = f"{SOURCES['SEC_ARCHIVES']}/{int(cik)}/{filing['accession'].replace('-', '')}"
        period = ""
        try:
            index_name = _information_table(client, base)
            info_url = f"{base}/{quote(index_name, safe='.-_')}"
            info_path = _download(client, info_url, MAX_INFO_BYTES)
            try:
                with open(info_path, "rb") as stream:
                    raw = stream.read(MAX_INFO_BYTES + 1)
            finally:
                os.unlink(info_path)
            cover_path = None
            try:
                cover_path = _download(client, base + "/primary_doc.xml", MAX_COVER_BYTES)
                with open(cover_path, "rb") as stream:
                    cover = parse_cover(stream.read(MAX_COVER_BYTES + 1))
                period = cover["period"]
                if cover["amendment"]:
                    warnings.append(f"{filing['accession']}: cover marks an amendment")
            except (SourceError, OSError, ValueError) as exc:
                warnings.append(f"{filing['accession']}: cover not applied ({exc})")
            finally:
                if cover_path is not None and os.path.exists(cover_path):
                    os.unlink(cover_path)
            parsed, item_skipped = parse_thirteenf(raw, filing["accession"], filing["accepted"],
                                                   period, info_url, entity["key"])
            for key in skipped:
                skipped[key] += item_skipped[key]
            rows.extend(parsed)
        except (SourceError, OSError, ValueError) as exc:
            skipped["invalid"] += 1
            warnings.append(f"{filing['accession']}: {exc}")
    result = observations.store_observations(conn, rows)
    finished = clock.now()
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"),
                           allow_nan=False).encode("utf-8")
    registry.set_payload_fingerprint(conn, client)
    registry.clear_failure_recorder(conn)
    registry.record_ingest_run(
        conn, SOURCE, started.isoformat(), finished.isoformat(), query=cik,
        fetched=len(filings), stored=result["imported"], skipped=sum(skipped.values()),
        pages=1 + len(filings), truncated=len(filings) >= limit,
        request_sha256=hashlib.sha256(canonical).hexdigest(), warnings=warnings,
        coverage="recent submissions only; Form 13F-HR/A amendments are not applied",
        pages_detail=[{"page": 1, "offset": 0, "rows": len(filings), "selected": len(filings)}])
    return {
        "source": SOURCE, "entity_id": entity_id, "entity_key": entity["key"], "cik": cik,
        "source_url": url, "retrieved_at": client.retrieved_at, "filings": len(filings),
        "observations": result["imported"], "by_kind": result["kinds"], "skipped": skipped,
        "truncated": len(filings) >= limit, "warnings": warnings,
        "limitations": [
            "Only the manager's recent submissions are read; historical submissions files are not "
            "fetched and Form 13F-HR/A amendments are not applied.",
            "Holdings are bound to the filing manager as actor; the issuer is recorded by name and "
            "CUSIP only, so no issuer instrument is inferred or bound.",
            "Reported value is the filer's USD figure under the current schema; it is a position "
            "snapshot, not a money flow, and is not summed across managers or quarters.",
            "available_at is the filing acceptance time; occurred_at is the report period when the "
            "cover page parses.",
            "No net-buy/sell indicator, ownership-delta or motive is asserted here.",
        ],
    }


def _download(client, url, max_bytes):
    descriptor, path = tempfile.mkstemp(suffix=".xml")
    os.close(descriptor)
    try:
        client.download(url, path, max_bytes)
    except BaseException:
        if os.path.exists(path):
            os.unlink(path)
        raise
    return path
