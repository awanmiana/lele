import hashlib
import json
import math
import re
import sqlite3
from typing import TypedDict
from urllib.parse import urlencode
from uuid import uuid4

from ..core.constants import (
    FETCH_MAX_LIMIT, FETCH_MAX_PAGES, FETCH_PAGE_SIZE, OSFI_DATASET_URL,
    OSFI_RESOURCE_ID, SOURCES,
)
from ..core.registry import add_edge, add_metric, normalize_name, set_attr, upsert_entity
from .http import HTTPClient, SourceError


class FetchResult(TypedDict):
    source: str
    fetched: int
    stored: int
    warnings: list[str]
    truncated: bool
    total: int | None
    pages: int
    coverage: str


_CATALOG = (
    ("gleif", "GLEIF legal entities", "Global LEI registrants; not a bank classification.", "GLEIF"),
    ("fdic", "FDIC BankFind institutions", "Active FDIC-insured US banks and savings institutions.", "FDIC"),
    ("worldbank", "World Bank indicators", "Country/economy macro observations, including aggregates; not entity fundamentals.", "WB_INDICATORS"),
    ("osfi", "OSFI Canada institutions", "Monthly public Canadian federal institution list and foreign bank representative offices; not all Canadian institutions or historical coverage. stored counts institutions, excluding the regulator.", "OSFI"),
)

_OSFI_FEDERAL = "Federally Regulated Financial Institutions"
_OSFI_REPRESENTATIVE = "Foreign Bank Representative Offices"
_OSFI_KINDS = {
    ("Banks", "Domestic Banks"): "bank",
    ("Banks", "Foreign Banks"): "bank",
    ("Banks", "Foreign Bank Branches - Full Service"): "bank_branch",
    ("Banks", "Foreign Bank Branches - Lending"): "bank_branch",
    ("Trust Companies", "Trust Companies"): "trust_company",
    ("Loan Companies", "Loan Companies"): "loan_company",
    ("Life Insurance Companies", "Canadian Life Insurance Companies"): "insurance_company",
    ("Life Insurance Companies", "Foreign Life Insurance Companies"): "insurance_company",
    ("Property & Casualty Insurance Companies", "Canadian Property & Casualty Insurance Companies"): "insurance_company",
    ("Property & Casualty Insurance Companies", "Foreign Property & Casualty Insurance Companies"): "insurance_company",
    ("Property & Casualty Insurance Companies", "Canadian Mortgage Insurers"): "insurance_company",
    ("Fraternal Benefit Societies", "Canadian Fraternal Benefit Societies"): "fraternal_benefit_society",
    ("Fraternal Benefit Societies", "Foreign Fraternal Benefit Societies"): "fraternal_benefit_society",
}


def list_sources() -> list[dict]:
    return [{"id": sid, "name": name, "coverage": coverage,
             "status": "available", "url": SOURCES[key]}
            for sid, name, coverage, key in _CATALOG]


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _object(value):
    return value if isinstance(value, dict) else {}


def _integer(value):
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value)):
        raise ValueError("invalid nonnegative integer")
    return int(value)


def _page(payload, source):
    try:
        if source == "osfi":
            if not isinstance(payload, dict) or payload.get("success") is not True or payload.get("error"):
                raise ValueError("CKAN success must be true")
            meta = payload["result"]
            if not isinstance(meta, dict) or meta.get("resource_id") != OSFI_RESOURCE_ID:
                raise ValueError("unexpected CKAN resource")
            if type(meta.get("total_was_estimated")) is not bool:
                raise ValueError("invalid CKAN total_was_estimated")
            rows = meta["records"]
            total = _integer(meta["total"])
        elif source == "worldbank":
            if not isinstance(payload, list) or len(payload) != 2:
                raise ValueError("expected metadata and observation array; check indicator/country")
            meta, rows = payload
            total = _integer(meta["total"])
            if rows is None and total == 0:
                rows = []
        else:
            if not isinstance(payload, dict) or payload.get("errors") or payload.get("error"):
                raise ValueError("API error or unexpected response")
            rows = payload["data"]
            meta = payload["meta"]
            total = _integer(meta["pagination"]["total"] if source == "gleif" else meta["total"])
        if not isinstance(rows, list):
            raise ValueError("expected record array")
        return rows, total, meta
    except (KeyError, TypeError, ValueError) as exc:
        raise SourceError(f"{source}: invalid API response ({exc}); verify parameters or retry later.") from exc


def _request_url(source, query, country, indicator, size, page, offset):
    if source == "gleif":
        base = SOURCES["GLEIF"]
        params = {"page[size]": size, "page[number]": page, "sort": "lei"}
        if query:
            field = "lei" if re.fullmatch(r"[A-Za-z0-9]{18}[0-9]{2}", query) else "entity.legalName"
            params[f"filter[{field}]"] = query.upper() if field == "lei" else query
        if country:
            params["filter[entity.legalAddress.country]"] = country
    elif source == "fdic":
        base = SOURCES["FDIC"]
        filters = "ACTIVE:1 AND INSFDIC:1"
        if query:
            if re.fullmatch(r"[0-9]+", query):
                filters += f" AND CERT:{int(query)}"
            else:
                escaped = re.sub(r'([+\-=&|><!(){}\[\]^"~*?:\\/])', r'\\\1', query)
                filters += f' AND NAME:"{escaped}"'
        params = {"filters": filters, "limit": size, "offset": offset,
                  "sort_by": "CERT", "sort_order": "ASC", "format": "json",
                  "fields": "CERT,NAME,ACTIVE,INSFDIC,STALP,CITY,WEBADDR,BKCLASS,REGAGNT,DATEUPDT"}
    elif source == "osfi":
        base = SOURCES["OSFI"]
        params = {"resource_id": OSFI_RESOURCE_ID, "limit": size, "offset": offset,
                  "sort": "_id asc", "include_total": "true", "plain": "true",
                  "fields": "_id,Company Name,FI Type Name,FI Group Name,FI Industry Name,Canadian Trade Company Name,City,Province State"}
        if query:
            params["q"] = query
    else:
        base = f"{SOURCES['WB_INDICATORS']}/country/{country or 'all'}/indicator/{indicator}"
        params = {"format": "json", "per_page": size, "page": page}
    return base + "?" + urlencode(params)


def _gleif(row, indicator):
    attrs = _object(_object(row).get("attributes"))
    entity = _object(attrs.get("entity"))
    lei = _text(attrs.get("lei")).upper()
    name = _text(_object(entity.get("legalName")).get("name"))
    if not re.fullmatch(r"[A-Z0-9]{18}[0-9]{2}", lei) or not name:
        raise ValueError("missing legal name or valid LEI")
    registration = _object(attrs.get("registration"))
    return {
        "key": f"lei:{lei}", "kind": "legal_entity", "name": name, "lei": lei,
        "country": _text(_object(entity.get("legalAddress")).get("country")),
        "attrs": {"source_id": lei, "record_status": registration.get("status"),
                  "iso_jurisdiction": entity.get("jurisdiction"),
                  "gleif.entity_status": entity.get("status"),
                  "gleif.registration_status": registration.get("status"),
                  "gleif.category": entity.get("category"),
                  "source_updated_at": registration.get("lastUpdateDate")},
    }


def _fdic(row, indicator):
    data = _object(_object(row).get("data"))
    cert = _integer(data.get("CERT"))
    name = _text(data.get("NAME"))
    if cert <= 0 or not name:
        raise ValueError("missing certificate or bank name")
    if str(data.get("ACTIVE")) != "1" or str(data.get("INSFDIC")) != "1":
        raise ValueError("institution outside active FDIC-insured coverage")
    return {
        "key": f"fdic:{cert}", "kind": "bank", "name": name, "country": "US",
        "attrs": {"source_id": str(cert), "record_status": "ACTIVE",
                  "fdic.cert": cert, "fdic.active": data.get("ACTIVE"),
                  "fdic.insured": data.get("INSFDIC"), "fdic.state": data.get("STALP"),
                  "fdic.city": data.get("CITY"), "fdic.class": data.get("BKCLASS"),
                  "fdic.regulator": data.get("REGAGNT"),
                  "fdic.reported_website": data.get("WEBADDR"),
                  "source_updated_at": data.get("DATEUPDT")},
    }


def _osfi(row, indicator):
    row = _object(row)
    fields = ("Company Name", "FI Type Name", "FI Group Name", "FI Industry Name")
    values = tuple(_text(row.get(field)) for field in fields)
    if any(not value or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value)
           for value in values):
        raise ValueError("missing or invalid OSFI name or classification")
    name, fi_type, group, industry = values
    if _OSFI_REPRESENTATIVE in (fi_type, group, industry):
        kind = "representative_office"
    elif fi_type == _OSFI_FEDERAL:
        kind = _OSFI_KINDS.get((group, industry), "financial_institution")
    else:
        kind = "financial_institution"
    identity = json.dumps(tuple(normalize_name(value) for value in values),
                          ensure_ascii=True, separators=(",", ":"))
    key = "osfi:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
    row_id = row.get("_id")
    if row_id is not None:
        row_id = _integer(row_id)
        if row_id <= 0:
            raise ValueError("invalid CKAN row id")
    return {
        "key": key, "kind": kind, "name": name, "country": "CA",
        "attrs": {"identity_basis": "sha256 of JSON tuple: normalized company name, FI type, FI group, FI industry; whitespace collapsed and lowercased; renames not resolved",
                  "source_license": "ca-ogl-lgo", "source_dataset": OSFI_DATASET_URL,
                  "source_frequency": "monthly", "osfi.resource_id": OSFI_RESOURCE_ID,
                  "osfi.row_id": row_id, "osfi.fi_type": row["FI Type Name"],
                  "osfi.fi_group": row["FI Group Name"], "osfi.fi_industry": row["FI Industry Name"],
                  "country_basis": "regulatory_jurisdiction", "record_status": "listed",
                  "osfi.trade_name": _text(row.get("Canadian Trade Company Name")),
                  "osfi.city": _text(row.get("City")),
                  "osfi.province_state": _text(row.get("Province State"))},
    }


def _worldbank(row, indicator):
    row = _object(row)
    if row.get("value") is None:
        return None
    value = row["value"]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("invalid numeric observation")
    economy = _object(row.get("country"))
    code = _text(row.get("countryiso3code")).upper()
    name = _text(economy.get("value"))
    period = _text(row.get("date"))
    series = _object(row.get("indicator"))
    if (not re.fullmatch(r"[A-Z0-9]{3}", code) or not name
            or not re.fullmatch(r"[0-9]{4}(?:M[0-9]{2}|Q[1-4])?", period)
            or series.get("id") != indicator):
        raise ValueError("missing economy, period or matching indicator")
    return {
        "key": f"wb:{code}", "kind": "economy", "name": name,
        "country": _text(economy.get("id")),
        "attrs": {"source_id": code, "record_status": row.get("obs_status"),
                  "worldbank.economy_code": code, "data_scope": "macro",
                  f"worldbank.{indicator}.name": series.get("value"),
                  f"worldbank.{indicator}.unit": row.get("unit")},
        "metric": (f"macro:{indicator}", value, period),
    }


def fetch_source(conn, source: str, query: str = '', country: str = '', limit: int = 25,
                 indicator: str = 'NY.GDP.MKTP.CD') -> FetchResult:
    if not all(isinstance(v, str) for v in (source, query, country, indicator)):
        raise SourceError("source, query, country and indicator must be strings.")
    source, query, country, indicator = source.strip().lower(), query.strip(), country.strip().upper(), indicator.strip()
    if source not in {item[0] for item in _CATALOG}:
        raise SourceError("Unknown source; choose " + ", ".join(item[0] for item in _CATALOG) + ".")
    if type(limit) is not int or not 1 <= limit <= FETCH_MAX_LIMIT:
        raise SourceError(f"limit must be an integer from 1 to {FETCH_MAX_LIMIT}.")
    if len(query) > 200 or any(ord(c) < 32 for c in query):
        raise SourceError("query must be at most 200 characters without control characters.")
    if source == "gleif" and country and not re.fullmatch(r"[A-Z]{2}", country):
        raise SourceError("GLEIF country must be a two-letter legal-address country code.")
    if source == "fdic" and country not in ("", "US", "USA"):
        raise SourceError("FDIC coverage is US only; use country='US'.")
    if source == "osfi" and country not in ("", "CA", "CAN"):
        raise SourceError("OSFI coverage is Canada only; use country='CA'.")
    if source == "worldbank":
        if query:
            raise SourceError("World Bank does not accept name queries; use country and indicator.")
        if country and not re.fullmatch(r"[A-Z0-9]{2,3}", country):
            raise SourceError("World Bank country must be a single two/three-character economy code or ALL.")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", indicator):
            raise SourceError("indicator must be a single World Bank indicator code.")
    client = HTTPClient()
    result: FetchResult = {"source": source, "fetched": 0, "stored": 0, "warnings": [],
              "truncated": False, "total": None, "pages": 0,
              "coverage": next(item[2] for item in _CATALOG if item[0] == source)}
    size = min(limit, FETCH_PAGE_SIZE)
    mapped = []
    seen_pages = set()
    seen_records = set()
    skipped = 0
    missing = 0
    estimated_total = False
    mapper = {"gleif": _gleif, "fdic": _fdic, "worldbank": _worldbank, "osfi": _osfi}[source]
    for page in range(1, FETCH_MAX_PAGES + 1):
        url = _request_url(source, query, country, indicator, size, page, result["fetched"])
        try:
            payload = client.get_json(url)
            rows, total, meta = _page(payload, source)
        except SourceError as exc:
            raise SourceError(f"{source}: fetch failed; no ingestion writes applied. {exc}") from exc
        result["pages"] += 1
        if source == "osfi" and meta["total_was_estimated"]:
            if not estimated_total:
                result["warnings"].append("CKAN total was estimated; complete coverage cannot be inferred.")
            estimated_total = True
        if result["total"] is not None and result["total"] != total:
            result["warnings"].append("API total changed during pagination; results are not a snapshot.")
        result["total"] = total
        fingerprint = json.dumps(rows, sort_keys=True)
        if rows and fingerprint in seen_pages:
            result["warnings"].append("API repeated a page; stopped to avoid looping.")
            result["truncated"] = True
            break
        seen_pages.add(fingerprint)
        selected = rows[:limit - result["fetched"]]
        result["fetched"] += len(selected)
        for row in selected:
            try:
                record = mapper(row, indicator)
            except (ValueError, TypeError, OverflowError):
                skipped += 1
                continue
            if record is None:
                missing += 1
                continue
            identity = (record["key"], record.get("metric", ("", "", ""))[2])
            if identity in seen_records:
                skipped += 1
                continue
            seen_records.add(identity)
            record["attrs"].update({"source": source, "source_url": url,
                                    "retrieved_at": client.retrieved_at})
            if source == "worldbank":
                record["attrs"]["source_updated_at"] = meta.get("lastupdated")
            if source == "osfi" and record["kind"] == "financial_institution":
                warning = "Unknown OSFI classification retained as financial_institution, not a bank."
                if warning not in result["warnings"]:
                    result["warnings"].append(warning)
            mapped.append(record)
        if not rows:
            if estimated_total:
                result["truncated"] = True
            elif total > result["fetched"]:
                result["warnings"].append("API returned an empty page before its reported total.")
                result["truncated"] = True
            break
        if result["fetched"] >= limit:
            result["truncated"] = estimated_total or total > result["fetched"] or len(rows) > len(selected)
            break
        if not estimated_total and result["fetched"] >= total:
            break
        if source not in ("fdic", "osfi") and len(rows) < size:
            result["truncated"] = True
            result["warnings"].append("API returned a short numbered page; stopped to avoid skipping records.")
            break
    else:
        result["truncated"] = True
        result["warnings"].append("Page budget reached; narrow the query or country.")
    if skipped:
        result["warnings"].append(f"Skipped {skipped} invalid, duplicate or out-of-coverage records.")
    if missing:
        result["warnings"].append(f"Skipped {missing} null macro observations; these are not zero values.")
    if result["truncated"]:
        result["warnings"].append("Bounded partial coverage; limit counts input records, not successful stores.")
    if source == "osfi":
        result["warnings"].extend([
            "OSFI identity is name/type/group/industry-based; renames are not resolved and CKAN _id is only row provenance.",
            "Representative offices carry no automatic supervision claim; no regulator edge is inferred for them.",
            "stored counts imported institution records, excluding the regulator; absent records are not deleted and historical coverage is not claimed.",
        ])
    result["warnings"].extend(client.warnings)
    savepoint = "fetch_" + uuid4().hex
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        authority = None
        if source == "osfi" and mapped:
            authority = upsert_entity(conn, key="authority:ca:osfi", kind="regulator",
                                      name="Office of the Superintendent of Financial Institutions", country="CA")
            for key, value in {"source": "osfi", "source_url": OSFI_DATASET_URL,
                               "source_dataset": OSFI_DATASET_URL, "source_license": "ca-ogl-lgo",
                               "source_frequency": "monthly", "retrieved_at": client.retrieved_at}.items():
                set_attr(conn, authority, key, value)
        for record in mapped:
            attrs = record.pop("attrs")
            metric = record.pop("metric", None)
            eid = upsert_entity(conn, **record)
            for key, value in attrs.items():
                if value is not None:
                    set_attr(conn, eid, key, str(value))
            if (authority is not None and record["kind"] != "representative_office"
                    and _text(attrs["osfi.fi_type"]) == _OSFI_FEDERAL):
                evidence = "OSFI list entry: " + json.dumps({
                    "FI Type Name": attrs["osfi.fi_type"],
                    "FI Group Name": attrs["osfi.fi_group"],
                    "FI Industry Name": attrs["osfi.fi_industry"],
                }, ensure_ascii=True, sort_keys=True) + "; not universal Canadian institution coverage or historical supervision evidence."
                add_edge(conn, eid, "regulated_by", authority, source_url=OSFI_DATASET_URL,
                         observed_at=attrs["retrieved_at"], evidence=evidence)
            if metric:
                metric_key, metric_value, metric_period = metric
                add_metric(conn, eid, metric_key, metric_value, metric_period, source=source)
            result["stored"] += 1
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception as exc:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        if isinstance(exc, sqlite3.Error):
            raise SourceError("Registry write failed; ingestion rolled back. Check database schema/permissions.") from exc
        raise
    return result
