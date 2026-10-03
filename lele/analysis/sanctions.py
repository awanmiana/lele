import csv
import hashlib
import io
import json
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from ..core import clock
from datetime import datetime

from ..core import importer, registry
from ..core.constants import SANCTIONS_ENDPOINTS
from ..fetchers import prices
from ..fetchers.http import HTTPClient
from . import projection

MAX_FILE_BYTES = projection.MAX_FILE_BYTES
MAX_LISTINGS = 10_000
MAX_SDN_BYTES = 32 * 1024 * 1024
MAX_UN_BYTES = 16 * 1024 * 1024
MAX_UK_BYTES = 32 * 1024 * 1024
MAX_EU_BYTES = 32 * 1024 * 1024
MAX_UN_ALIAS_EVIDENCE = 300
EU_NAMESPACE = "{http://eu.europa.ec/fpi/fsd/export}"
SANCTIONS_SOURCES = {
    "ofac-sdn": "OFAC Specially Designated Nationals (US Treasury, public domain)",
    "un-consolidated": "UN Security Council Consolidated Sanctions List (public)",
    "uk-ofsi": "UK OFSI Consolidated List of Financial Sanctions Targets (public)",
    "eu-consolidated": "EU consolidated list of financial sanctions (public FSF extract)",
}
SOURCE_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2}))?")
LISTING_FIELDS = {"key", "name", "type", "program", "country", "basis", "status",
                  "published_at", "effective_at", "evidence"}
BASES = ("designation", "allegation", "finding", "unknown")
STATUSES = ("active", "delisted")


def _optional_date(value, label):
    if value is None:
        return ""
    importer._text(value, label, 64, True)
    if not DATE_RE.fullmatch(value):
        raise ValueError(f"{label} must be an ISO date or datetime")
    return value


def import_sanctions(conn, source, path, source_url, retrieved_at=None):
    if not isinstance(source, str) or not SOURCE_RE.fullmatch(source):
        raise ValueError("source must be 1..32 lowercase letters, digits or hyphens")
    importer._provenance(source_url, "sanctions source_url")
    retrieved = _optional_date(retrieved_at, "retrieved_at")
    with open(path, "rb") as stream:
        raw = stream.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("sanctions file exceeds 2 MiB")
    try:
        payload = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=importer._pairs,
                             parse_constant=importer._constant)
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("sanctions file is not valid UTF-8 JSON") from exc
    projection._exact_object(payload, {"listings"}, "sanctions file")
    listings = importer._array(payload["listings"], MAX_LISTINGS, "sanctions listings")
    imported = 0
    for item in listings:
        if not isinstance(item, dict) or item.keys() - LISTING_FIELDS:
            raise ValueError("sanctions listing has unsupported fields")
        key = importer._text(item.get("key"), "listing key", 256, True)
        name = importer._text(item.get("name"), "listing name", 512, True)
        entity_type = importer._text(item.get("type"), "listing type", 256) or ""
        program = importer._text(item.get("program"), "listing program", 256) or ""
        country = importer._text(item.get("country"), "listing country", 64) or ""
        basis = importer._text(item.get("basis"), "listing basis", 32) or "unknown"
        status = importer._text(item.get("status"), "listing status", 32) or "active"
        evidence = importer._text(item.get("evidence"), "listing evidence", 2048) or ""
        if basis not in BASES:
            raise ValueError("listing basis must be designation, allegation, finding or unknown")
        if status not in STATUSES:
            raise ValueError("listing status must be active or delisted")
        registry.add_sanctions_listing(
            conn, source=source, listing_key=key, name=name, entity_type=entity_type,
            program=program, country=country, basis=basis, status=status,
            published_at=_optional_date(item.get("published_at"), "published_at"),
            effective_at=_optional_date(item.get("effective_at"), "effective_at"),
            source_url=source_url, retrieved_at=retrieved, evidence=evidence)
        imported += 1
    return {"source": source, "imported": imported, "source_url": source_url,
            "retrieved_at": retrieved or None, "file_sha256": hashlib.sha256(raw).hexdigest(),
            "review": "imported_listings_only_no_entity_matches"}


def _clean(value):
    text = (value or "").strip()
    return "" if text in ("", "-0-") else text


def parse_ofac_sdn(text):
    listings, skipped = [], 0
    for row in csv.reader(io.StringIO(text, newline="")):
        if not row or all(not cell.strip() for cell in row):
            continue
        if len(row) < 4:
            skipped += 1
            continue
        # OFAC SDN CSV has 12 columns:
        # ent_num, SDN_Name, SDN_Type, Program, Title, Call_Sign, Vess_type,
        # Tonnage, Gross_Registered_Tonnage, Vess_flag, Vess_owner, Remarks
        key = row[0].strip()
        name = row[1].strip()
        if not key or not name:
            skipped += 1
            continue
        entity_type = _clean(row[2]) if len(row) > 2 else ""
        program = _clean(row[3]) if len(row) > 3 else ""
        title = _clean(row[4]) if len(row) > 4 else ""
        call_sign = _clean(row[5]) if len(row) > 5 else ""
        vess_type = _clean(row[6]) if len(row) > 6 else ""
        tonnage = _clean(row[7]) if len(row) > 7 else ""
        grt = _clean(row[8]) if len(row) > 8 else ""
        vess_flag = _clean(row[9]) if len(row) > 9 else ""
        vess_owner = _clean(row[10]) if len(row) > 10 else ""
        remarks = _clean(row[11]) if len(row) > 11 else ""
        listings.append({
            "key": key, "name": name, "type": entity_type, "program": program,
            "title": title, "call_sign": call_sign, "vess_type": vess_type,
            "tonnage": tonnage, "grt": grt, "vess_flag": vess_flag,
            "vess_owner": vess_owner, "remarks": remarks,
        })
    return listings, skipped


def _store_ofac_listings(conn, listings, source_url, retrieved_at):
    imported = 0
    for item in listings:
        evidence_parts = [
            f"OFAC SDN ent_num {item['key']}",
            f"type {item['type'] or 'unknown'}",
            f"program {item['program'] or 'unknown'}",
        ]
        if item.get("title"):
            evidence_parts.append(f"title {item['title']}")
        if item.get("call_sign"):
            evidence_parts.append(f"call_sign {item['call_sign']}")
        if item.get("vess_type"):
            evidence_parts.append(f"vess_type {item['vess_type']}")
        if item.get("tonnage"):
            evidence_parts.append(f"tonnage {item['tonnage']}")
        if item.get("grt"):
            evidence_parts.append(f"grt {item['grt']}")
        if item.get("vess_flag"):
            evidence_parts.append(f"vess_flag {item['vess_flag']}")
        if item.get("vess_owner"):
            evidence_parts.append(f"vess_owner {item['vess_owner']}")
        if item.get("remarks"):
            evidence_parts.append(f"remarks {item['remarks']}")

        registry.add_sanctions_listing(
            conn, source="ofac-sdn", listing_key=item["key"], name=item["name"],
            entity_type=item["type"], program=item["program"], basis="designation",
            status="active", source_url=source_url, retrieved_at=retrieved_at,
            evidence="; ".join(evidence_parts))
        imported += 1
    return imported


def fetch_ofac(conn, source="ofac-sdn"):
    if source not in SANCTIONS_SOURCES:
        raise ValueError("unsupported sanctions source; choose ofac-sdn")
    url = SANCTIONS_ENDPOINTS["OFAC_SDN_CSV"]
    started = clock.now()
    registry.record_failures(conn, source=source, started=started)
    descriptor, path = tempfile.mkstemp(suffix=".csv")
    os.close(descriptor)
    try:
        client = HTTPClient(ttl=0)
        client.cache_dir = None
        client.ttl = 0
        written = client.download(url, path, MAX_SDN_BYTES)
        retrieved = prices._retrieved(client.retrieved_at)
        with open(path, "rb") as stream:
            raw = stream.read()
        listings, skipped = parse_ofac_sdn(raw.decode("utf-8-sig"))
        if not listings:
            raise ValueError("no OFAC listings were parsed from the downloaded file")
        imported = _store_ofac_listings(conn, listings, url, retrieved.isoformat())
        delisted = registry.mark_delisted(conn, "ofac-sdn", [item["key"] for item in listings])
        file_sha256 = hashlib.sha256(raw).hexdigest()
        warnings = []
        if delisted:
            warnings.append(f"{delisted} previously active listings are absent from the current "
                            "file and were marked delisted, not deleted.")
        registry.clear_failure_recorder(conn)
        registry.record_ingest_run(
            conn, source, started.isoformat(), clock.now().isoformat(),
            fetched=len(listings), stored=imported, skipped=skipped, pages=1,
            request_sha256=file_sha256,
            records_sha256=hashlib.sha256(json.dumps(
                [item["key"] for item in listings], sort_keys=True).encode("utf-8")).hexdigest(),
            warnings=warnings, coverage=SANCTIONS_SOURCES[source])
        return {"source": source, "published_url": url, "retrieved_at": retrieved.isoformat(),
                "bytes": written, "parsed": len(listings), "imported": imported,
                "delisted": delisted, "skipped": skipped, "file_sha256": file_sha256,
                "review": "imported_listings_only_no_entity_matches"}
    finally:
        os.unlink(path)


def _un_name(record, kind):
    if kind == "Entity":
        return (record.findtext("FIRST_NAME") or "").strip()
    parts = [record.findtext(tag) for tag in ("FIRST_NAME", "SECOND_NAME", "THIRD_NAME",
                                              "FOURTH_NAME")]
    return " ".join(part.strip() for part in parts if part and part.strip())


def _un_country(record, kind):
    if kind == "Entity":
        node = record.find("ENTITY_ADDRESS")
        return (node.findtext("COUNTRY") or "").strip() if node is not None else ""
    node = record.find("NATIONALITY")
    return (node.findtext("VALUE") or "").strip() if node is not None else ""


DTD_MARKER = b"<!DOCTYPE"
ENTITY_MARKER = b"<!ENTITY"


def reject_dtd(raw, label):
    """Refuse an XML document that declares a DTD or an entity.

    Python's expat expands internal entity definitions, so a document declaring
    them can be a "billion laughs" bomb: a few hundred bytes of declarations
    expand to gigabytes and hang the process. External entities are not
    resolved, but the expansion alone is enough to take the tool down, and the
    bytes come from a third party. Every XML path in this package therefore goes
    through this check, and the parsers accept `str` only so a caller cannot
    accidentally hand the pre-checked bytes to a different code path.
    """
    body = raw if isinstance(raw, (bytes, bytearray)) else raw.encode("utf-8", "replace")
    upper = bytes(body).upper()
    if DTD_MARKER in upper:
        raise ValueError(f"{label} contains a DTD, which is not accepted")
    if ENTITY_MARKER in upper:
        raise ValueError(f"{label} declares an XML entity, which is not accepted")
    return body


def parse_un_consolidated(text):
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8-sig", "replace")
    reject_dtd(text, "UN consolidated file")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError("UN consolidated file is not valid XML") from exc
    if root.tag != "CONSOLIDATED_LIST":
        raise ValueError("UN consolidated file has an unexpected root element")
    listings, skipped = [], 0
    for container, kind in (("INDIVIDUALS", "Individual"), ("ENTITIES", "Entity")):
        node = root.find(container)
        if node is None:
            continue
        for record in node:
            name = _un_name(record, kind)
            key = (record.findtext("REFERENCE_NUMBER") or record.findtext("DATAID") or "").strip()
            if not name or not key:
                skipped += 1
                continue
            program = (record.findtext("UN_LIST_TYPE") or "").strip()
            listed_on = (record.findtext("LISTED_ON") or "").strip()
            comments = (record.findtext("COMMENTS1") or "").strip().replace("\n", " ")
            listings.append({"key": key, "name": name, "type": kind, "program": program,
                             "country": _un_country(record, kind), "listed_on": listed_on,
                             "comments": comments[:MAX_UN_ALIAS_EVIDENCE]})
    if len(listings) > MAX_LISTINGS:
        raise ValueError("UN consolidated file exceeds the listing limit")
    return listings, skipped


def _store_un_listings(conn, listings, source_url, retrieved_at):
    imported = 0
    for item in listings:
        evidence = (f"UN consolidated list {item['key']}; type {item['type']}; "
                    f"list {item['program'] or 'unknown'}; listed {item['listed_on'] or 'unknown'}")
        if item["comments"]:
            evidence += f"; {item['comments']}"
        registry.add_sanctions_listing(
            conn, source="un-consolidated", listing_key=item["key"], name=item["name"],
            entity_type=item["type"], program=item["program"], country=item["country"],
            basis="designation", status="active",
            published_at=item["listed_on"] if DATE_RE.fullmatch(item["listed_on"] or "") else "",
            source_url=source_url, retrieved_at=retrieved_at, evidence=evidence)
        imported += 1
    return imported


def fetch_un(conn, source="un-consolidated"):
    if source not in SANCTIONS_SOURCES:
        raise ValueError("unsupported sanctions source")
    url = SANCTIONS_ENDPOINTS["UN_CONSOLIDATED_XML"]
    started = clock.now()
    registry.record_failures(conn, source=source, started=started)
    descriptor, path = tempfile.mkstemp(suffix=".xml")
    os.close(descriptor)
    try:
        client = HTTPClient(ttl=0)
        client.cache_dir = None
        client.ttl = 0
        written = client.download(url, path, MAX_UN_BYTES)
        retrieved = prices._retrieved(client.retrieved_at)
        with open(path, "rb") as stream:
            raw = stream.read()
        reject_dtd(raw, "UN consolidated file")
        listings, skipped = parse_un_consolidated(raw.decode("utf-8-sig"))
        if not listings:
            raise ValueError("no UN listings were parsed from the downloaded file")
        imported = _store_un_listings(conn, listings, url, retrieved.isoformat())
        delisted = registry.mark_delisted(conn, "un-consolidated",
                                          [item["key"] for item in listings])
        file_sha256 = hashlib.sha256(raw).hexdigest()
        warnings = []
        if delisted:
            warnings.append(f"{delisted} previously active listings are absent from the current "
                            "file and were marked delisted, not deleted.")
        registry.clear_failure_recorder(conn)
        registry.record_ingest_run(
            conn, source, started.isoformat(), clock.now().isoformat(),
            fetched=len(listings), stored=imported, skipped=skipped, pages=1,
            request_sha256=file_sha256,
            records_sha256=hashlib.sha256(json.dumps(
                [item["key"] for item in listings], sort_keys=True).encode("utf-8")).hexdigest(),
            warnings=warnings, coverage=SANCTIONS_SOURCES[source])
        return {"source": source, "published_url": url, "retrieved_at": retrieved.isoformat(),
                "bytes": written, "parsed": len(listings), "imported": imported,
                "delisted": delisted, "skipped": skipped, "file_sha256": file_sha256,
                "review": "imported_listings_only_no_entity_matches"}
    finally:
        os.unlink(path)


def fetch_sanctions(conn, source):
    if source == "ofac-sdn":
        return fetch_ofac(conn, source)
    if source == "un-consolidated":
        return fetch_un(conn, source)
    if source == "uk-ofsi":
        return fetch_uk(conn, source)
    if source == "eu-consolidated":
        return fetch_eu(conn, source)
    raise ValueError("unsupported sanctions source; choose ofac-sdn, un-consolidated, uk-ofsi or "
                     "eu-consolidated")


def _uk_date(value):
    text = (value or "").strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


_UK_NAME_PARTS = ("Name 1", "Name 2", "Name 3", "Name 4", "Name 5")


def parse_uk_ofsi(text):
    rows = list(csv.reader(io.StringIO(text, newline="")))
    start = next((index for index, row in enumerate(rows)
                  if row and row[0].strip() == "Name 6"), None)
    if start is None:
        raise ValueError("UK OFSI file does not contain the expected header row")
    index_of = {cell.strip(): position for position, cell in enumerate(rows[start])}
    if not {"Name 6", "Alias Type", "Group Type", "Regime", "Listed On",
            "Group ID"} <= set(index_of):
        raise ValueError("UK OFSI file is missing expected columns")

    def cell(row, name):
        position = index_of[name]
        return row[position].strip() if position < len(row) else ""

    rank_of = {"Primary name": 0, "Primary name variation": 1}
    chosen: dict[str, tuple] = {}
    for row in rows[start + 1:]:
        if not row or all(not value.strip() for value in row):
            continue
        group_id = cell(row, "Group ID")
        if not group_id:
            continue
        rank = rank_of.get(cell(row, "Alias Type"), 2)
        if group_id not in chosen or rank < chosen[group_id][0]:
            chosen[group_id] = (rank, row)
    listings, skipped = [], 0
    for group_id in sorted(chosen):
        row = chosen[group_id][1]
        group_type = cell(row, "Group Type")
        if group_type in ("Entity", "Ship"):
            name = cell(row, "Name 6")
        else:
            parts = [cell(row, part) for part in _UK_NAME_PARTS if cell(row, part)]
            parts.append(cell(row, "Name 6"))
            name = " ".join(part for part in parts if part)
        if not name or not group_type:
            skipped += 1
            continue
        comments = cell(row, "Other Information").replace("\n", " ")
        listings.append({"key": group_id, "name": name, "type": group_type,
                         "program": cell(row, "Regime"), "country": cell(row, "Country"),
                         "listed_on": _uk_date(cell(row, "Listed On")),
                         "comments": comments[:MAX_UN_ALIAS_EVIDENCE]})
    if len(listings) > MAX_LISTINGS:
        raise ValueError("UK OFSI file exceeds the listing limit")
    return listings, skipped


def _store_uk_listings(conn, listings, source_url, retrieved_at):
    imported = 0
    for item in listings:
        evidence = (f"UK OFSI ConList Group {item['key']}; type {item['type']}; "
                    f"regime {item['program'] or 'unknown'}; listed {item['listed_on'] or 'unknown'}")
        if item["comments"]:
            evidence += f"; {item['comments']}"
        registry.add_sanctions_listing(
            conn, source="uk-ofsi", listing_key=item["key"], name=item["name"],
            entity_type=item["type"], program=item["program"], country=item["country"],
            basis="designation", status="active", published_at=item["listed_on"],
            source_url=source_url, retrieved_at=retrieved_at, evidence=evidence)
        imported += 1
    return imported


def fetch_uk(conn, source="uk-ofsi"):
    if source not in SANCTIONS_SOURCES:
        raise ValueError("unsupported sanctions source")
    url = SANCTIONS_ENDPOINTS["UK_OFSI_CONLIST_CSV"]
    started = clock.now()
    registry.record_failures(conn, source=source, started=started)
    descriptor, path = tempfile.mkstemp(suffix=".csv")
    os.close(descriptor)
    try:
        client = HTTPClient(ttl=0)
        client.cache_dir = None
        client.ttl = 0
        written = client.download(url, path, MAX_UK_BYTES)
        retrieved = prices._retrieved(client.retrieved_at)
        with open(path, "rb") as stream:
            raw = stream.read()
        listings, skipped = parse_uk_ofsi(raw.decode("utf-8-sig"))
        if not listings:
            raise ValueError("no UK OFSI listings were parsed from the downloaded file")
        imported = _store_uk_listings(conn, listings, url, retrieved.isoformat())
        delisted = registry.mark_delisted(conn, "uk-ofsi", [item["key"] for item in listings])
        file_sha256 = hashlib.sha256(raw).hexdigest()
        warnings = []
        if delisted:
            warnings.append(f"{delisted} previously active listings are absent from the current "
                            "file and were marked delisted, not deleted.")
        registry.clear_failure_recorder(conn)
        registry.record_ingest_run(
            conn, source, started.isoformat(), clock.now().isoformat(),
            fetched=len(listings), stored=imported, skipped=skipped, pages=1,
            request_sha256=file_sha256,
            records_sha256=hashlib.sha256(json.dumps(
                [item["key"] for item in listings], sort_keys=True).encode("utf-8")).hexdigest(),
            warnings=warnings, coverage=SANCTIONS_SOURCES[source])
        return {"source": source, "published_url": url, "retrieved_at": retrieved.isoformat(),
                "bytes": written, "parsed": len(listings), "imported": imported,
                "delisted": delisted, "skipped": skipped, "file_sha256": file_sha256,
                "review": "imported_listings_only_no_entity_matches"}
    finally:
        os.unlink(path)


def _eu_name(child):
    whole = (child.get("wholeName") or "").strip()
    if whole:
        return whole
    parts = [child.get(part) for part in ("firstName", "middleName", "lastName")]
    return " ".join(part.strip() for part in parts if part and part.strip())


def _eu_subject(child):
    code = (child.get("code") or "").strip()
    if code == "person":
        return "Individual"
    if code == "enterprise":
        return "Entity"
    return code


def parse_eu_consolidated(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    reject_dtd(data, "EU consolidated file")
    listings, skipped = [], 0
    try:
        for event, element in ET.iterparse(io.BytesIO(data), events=("end",)):
            if event != "end" or element.tag != EU_NAMESPACE + "sanctionEntity":
                continue
            key = (element.get("euReferenceNumber") or element.get("logicalId") or "").strip()
            names, subject, program, country, listed, number_title = [], "", "", "", "", ""
            for child in element:
                tag = child.tag
                if tag == EU_NAMESPACE + "nameAlias":
                    name = _eu_name(child)
                    if name:
                        names.append((child.get("strong") == "true",
                                      child.get("nameLanguage") == "en", name))
                elif tag == EU_NAMESPACE + "subjectType" and not subject:
                    subject = _eu_subject(child)
                elif tag == EU_NAMESPACE + "regulation" and not program:
                    program = (child.get("programme") or "").strip()
                    listed = (child.get("entryIntoForceDate")
                              or child.get("publicationDate") or "").strip()
                    number_title = (child.get("numberTitle") or "").strip()
                elif tag in (EU_NAMESPACE + "citizenship", EU_NAMESPACE + "address") and not country:
                    country = (child.get("countryIso2Code") or "").strip()
            name = ""
            if names:
                names.sort(key=lambda item: (not item[0], not item[1]))
                name = names[0][2]
            element.clear()
            if not key or not name:
                skipped += 1
                continue
            listings.append({"key": key, "name": name, "type": subject or "Unknown",
                             "program": program, "country": country, "listed_on": listed,
                             "comments": number_title[:MAX_UN_ALIAS_EVIDENCE]})
    except ET.ParseError as exc:
        raise ValueError("EU consolidated file is not valid XML") from exc
    if len(listings) > MAX_LISTINGS:
        raise ValueError("EU consolidated file exceeds the listing limit")
    return listings, skipped


def _store_eu_listings(conn, listings, source_url, retrieved_at):
    imported = 0
    for item in listings:
        evidence = (f"EU consolidated list {item['key']}; subject {item['type']}; "
                    f"programme {item['program'] or 'unknown'}; "
                    f"listed {item['listed_on'] or 'unknown'}")
        if item["comments"]:
            evidence += f"; regulation {item['comments']}"
        registry.add_sanctions_listing(
            conn, source="eu-consolidated", listing_key=item["key"], name=item["name"],
            entity_type=item["type"], program=item["program"], country=item["country"],
            basis="designation", status="active",
            published_at=item["listed_on"] if DATE_RE.fullmatch(item["listed_on"] or "") else "",
            source_url=source_url, retrieved_at=retrieved_at, evidence=evidence)
        imported += 1
    return imported


def fetch_eu(conn, source="eu-consolidated"):
    if source not in SANCTIONS_SOURCES:
        raise ValueError("unsupported sanctions source")
    url = SANCTIONS_ENDPOINTS["EU_CONSOLIDATED_XML"]
    started = clock.now()
    registry.record_failures(conn, source=source, started=started)
    descriptor, path = tempfile.mkstemp(suffix=".xml")
    os.close(descriptor)
    try:
        client = HTTPClient(ttl=0)
        client.cache_dir = None
        client.ttl = 0
        written = client.download(url, path, MAX_EU_BYTES)
        retrieved = prices._retrieved(client.retrieved_at)
        with open(path, "rb") as stream:
            raw = stream.read()
        reject_dtd(raw, "EU consolidated file")
        listings, skipped = parse_eu_consolidated(raw)
        if not listings:
            raise ValueError("no EU listings were parsed from the downloaded file")
        imported = _store_eu_listings(conn, listings, url, retrieved.isoformat())
        delisted = registry.mark_delisted(conn, "eu-consolidated",
                                          [item["key"] for item in listings])
        file_sha256 = hashlib.sha256(raw).hexdigest()
        warnings = []
        if delisted:
            warnings.append(f"{delisted} previously active listings are absent from the current "
                            "file and were marked delisted, not deleted.")
        registry.clear_failure_recorder(conn)
        registry.record_ingest_run(
            conn, source, started.isoformat(), clock.now().isoformat(),
            fetched=len(listings), stored=imported, skipped=skipped, pages=1,
            request_sha256=file_sha256,
            records_sha256=hashlib.sha256(json.dumps(
                [item["key"] for item in listings], sort_keys=True).encode("utf-8")).hexdigest(),
            warnings=warnings, coverage=SANCTIONS_SOURCES[source])
        return {"source": source, "published_url": url, "retrieved_at": retrieved.isoformat(),
                "bytes": written, "parsed": len(listings), "imported": imported,
                "delisted": delisted, "skipped": skipped, "file_sha256": file_sha256,
                "review": "imported_listings_only_no_entity_matches"}
    finally:
        os.unlink(path)
