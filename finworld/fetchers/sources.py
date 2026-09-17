import hashlib
import json
import math
import re
import sqlite3
from datetime import date
from typing import TypedDict
from urllib.parse import quote, urlencode, urlsplit
from uuid import uuid4

from ..core.constants import (
    FETCH_MAX_LIMIT, FETCH_MAX_PAGES, FETCH_PAGE_SIZE, OSFI_DATASET_URL,
    OSFI_RESOURCE_ID, SOURCES,
)
from ..core.registry import add_edge, add_filing, add_metric, normalize_name, set_attr, upsert_entity
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
    ("gleif", "GLEIF legal entities", "Global LEI registrants; FUND category option selects funds, not specifically VC; not a bank classification.", "GLEIF"),
    ("fdic", "FDIC BankFind institutions", "Active FDIC-insured US banks and savings institutions.", "FDIC"),
    ("worldbank", "World Bank indicators", "Country/economy macro observations, including aggregates; not entity fundamentals.", "WB_INDICATORS"),
    ("osfi", "OSFI Canada institutions", "Monthly public Canadian federal institution list and foreign bank representative offices; not all Canadian institutions or historical coverage. stored counts institutions, excluding the regulator.", "OSFI"),
    ("sec", "SEC EDGAR issuers", "Single issuer submissions by 10-digit CIK or previously stored SEC ticker; limit caps recent filings, not issuers. Historical filing files are not fetched. Optional us-gaap USD financials use a 20 MiB cap; other requests retain the 2 MiB cap. stored counts issuers.", "SEC_SUBMISSIONS"),
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


def _request_url(source, query, country, indicator, size, page, offset, category=''):
    if source == "gleif":
        base = SOURCES["GLEIF"]
        params = {"page[size]": size, "page[number]": page, "sort": "lei"}
        if query:
            field = "lei" if re.fullmatch(r"[A-Za-z0-9]{18}[0-9]{2}", query) else "entity.legalName"
            params[f"filter[{field}]"] = query.upper() if field == "lei" else query
        if country:
            params["filter[entity.legalAddress.country]"] = country
        if category:
            params["filter[entity.category]"] = category
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


def _website(value, strip_scheme=False):
    value = _text(value)
    try:
        parts = urlsplit(value)
        if (parts.scheme.lower() not in ("http", "https") or not parts.hostname
                or not re.match(r"https?://", value, re.IGNORECASE)
                or parts.username is not None or parts.password is not None
                or parts.port == 0 or "\\" in value
                or any(c.isspace() or ord(c) < 32 for c in value)):
            return None
    except ValueError:
        return None
    return value.split("://", 1)[1] if strip_scheme else value


def _gleif(row, indicator):
    attrs = _object(_object(row).get("attributes"))
    entity = _object(attrs.get("entity"))
    lei = _text(attrs.get("lei")).upper()
    name = _text(_object(entity.get("legalName")).get("name"))
    if not re.fullmatch(r"[A-Z0-9]{18}[0-9]{2}", lei) or not name:
        raise ValueError("missing legal name or valid LEI")
    registration = _object(attrs.get("registration"))
    return {
        "key": f"lei:{lei}", "kind": "fund" if entity.get("category") == "FUND" else "legal_entity",
        "name": name, "lei": lei, "website": _website(entity.get("website"), strip_scheme=True),
        "country": _text(_object(entity.get("legalAddress")).get("country")),
        "attrs": {"source_id": lei, "record_status": registration.get("status"),
                  "iso_jurisdiction": entity.get("jurisdiction"),
                  "gleif.entity_status": entity.get("status"),
                  "gleif.registration_status": registration.get("status"),
                  "gleif.category": entity.get("category"),
                  "gleif.website": _text(entity.get("website")) or None,
                  "gleif.sub_category": _text(entity.get("subCategory")) or None,
                  "gleif.associated_lei": _text(_object(entity.get("associatedEntity")).get("lei")) or None,
                  "gleif.associated_name": _text(_object(entity.get("associatedEntity")).get("name")) or None,
                  "source_updated_at": registration.get("lastUpdateDate")},
    }


def _fund_manager_record(payload, fund_lei):
    payload = _object(payload)
    row = _object(payload.get("data"))
    attrs = _object(row.get("attributes"))
    identifiers = [value for value in (row.get("id"), attrs.get("lei")) if value is not None]
    if (payload.get("errors") or payload.get("error") or row.get("type") != "lei-records"
            or not identifiers or any(not isinstance(value, str)
                                      or not re.fullmatch(r"[A-Z0-9]{18}[0-9]{2}", value)
                                      for value in identifiers)
            or len(set(identifiers)) != 1 or identifiers[0] == fund_lei):
        raise ValueError("invalid single manager LEI record")
    return _gleif({"attributes": {**attrs, "lei": identifiers[0]}}, "")


def _fund_manager_relationship(payload, fund_lei, manager_lei):
    payload = _object(payload)
    attrs = _object(_object(payload.get("data")).get("attributes"))
    relationship = _object(attrs.get("relationship"))
    if (payload.get("errors") or payload.get("error")
            or relationship.get("type") != "IS_FUND-MANAGED_BY"
            or relationship.get("status") != "ACTIVE"
            or _object(relationship.get("startNode")).get("id") != fund_lei
            or _object(relationship.get("endNode")).get("id") != manager_lei):
        raise ValueError("invalid manager relationship corroboration")
    return attrs


def edge_fund_managers(conn, limit=25, progress=None) -> dict:
    if type(limit) is not int or not 1 <= limit <= FETCH_MAX_LIMIT:
        raise SourceError(f"limit must be an integer from 1 to {FETCH_MAX_LIMIT}.")
    eligible = "e.kind='fund' AND substr(e.key, 1, 4)='lei:'"
    linked = "EXISTS (SELECT 1 FROM edges r WHERE r.src_id=e.id AND r.rel='managed_by')"
    skipped = conn.execute(f"SELECT count(*) FROM entities e WHERE {eligible} AND {linked}").fetchone()[0]
    funds = conn.execute(
        f"SELECT e.id, e.key FROM entities e WHERE {eligible} AND NOT {linked} ORDER BY e.id LIMIT ?",
        (limit,),
    ).fetchall()
    result: dict = {"processed": 0, "linked": 0, "missing": 0, "skipped": skipped, "warnings": [], "edges": []}
    if not funds:
        return result
    client = HTTPClient()
    for fund_id, fund_key in funds:
        fund_lei = fund_key[4:]
        url = f"{SOURCES['GLEIF']}/{fund_lei}/fund-manager"
        result["processed"] += 1
        try:
            if not re.fullmatch(r"[A-Z0-9]{18}[0-9]{2}", fund_lei):
                raise ValueError("invalid local fund LEI")
            record = _fund_manager_record(client.get_json(url), fund_lei)
        except (SourceError, ValueError, TypeError):
            result["missing"] += 1
            result["warnings"].append(f"{fund_key}: manager unavailable or invalid; no edge stored.")
        else:
            observed_at = client.retrieved_at
            attrs = record.pop("attrs")
            attrs.update(source="gleif", source_url=url, retrieved_at=observed_at)
            evidence = f"GLEIF fund-manager subresource {url} publishes manager LEI {record['lei']}"
            relationship_url = url + "-relationship"
            corroboration = None
            try:
                corroboration = _fund_manager_relationship(client.get_json(relationship_url), fund_lei, record["lei"])
            except (SourceError, ValueError, TypeError):
                result["warnings"].append(f"{fund_key}: relationship corroboration unavailable or invalid; using fund-manager record only.")
            if corroboration is not None:
                registration = _object(corroboration.get("registration"))
                status = _text(registration.get("status")) or "unknown"
                updated = _text(registration.get("lastUpdateDate")) or "unknown"
                evidence += (f"; GLEIF fund-manager relationship ACTIVE (registration {status}, lastUpdateDate {updated});"
                             f" type IS_FUND-MANAGED_BY; source_url {relationship_url};"
                             f" relationship IS_FUND-MANAGED_BY ACTIVE as of {updated}")
            savepoint = "edges_" + uuid4().hex
            conn.execute(f"SAVEPOINT {savepoint}")
            try:
                existing = conn.execute("SELECT kind FROM entities WHERE key=?", (record["key"],)).fetchone()
                if existing is not None:
                    record["kind"] = existing[0]
                manager_id = upsert_entity(conn, **record)
                for key, value in attrs.items():
                    set_attr(conn, manager_id, key, value)
                if corroboration is not None:
                    for key, value in {
                        "gleif.fund_manager_relationship": json.dumps(corroboration, ensure_ascii=True, sort_keys=True),
                        "gleif.fund_manager_relationship.source_url": relationship_url,
                        "gleif.fund_manager_relationship.retrieved_at": client.retrieved_at,
                    }.items():
                        set_attr(conn, fund_id, key, value)
                edge = {"src_id": fund_id, "rel": "managed_by", "dst_id": manager_id,
                        "source_url": url, "observed_at": observed_at, "evidence": evidence}
                add_edge(conn, **edge)
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            except Exception as exc:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                if isinstance(exc, sqlite3.Error):
                    raise SourceError("Registry write failed; fund enrichment rolled back. Check database schema/permissions.") from exc
                raise
            result["linked"] += 1
            result["edges"].append(edge)
        if progress is not None:
            progress({key: result[key] for key in ("processed", "linked", "missing", "skipped")})
    result["warnings"].extend(client.warnings)
    return result


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


def _sec_cik(conn, query):
    if re.fullmatch(r"[0-9]{10}", query):
        return query
    matches = set()
    for key, tickers in conn.execute(
        "SELECT e.key, a.v FROM entities e JOIN attributes a ON a.entity_id=e.id"
        " WHERE a.k='sec.tickers' AND e.key LIKE 'cik:%'"
    ):
        if query in tickers.split(",") and re.fullmatch(r"cik:[0-9]{10}", key):
            matches.add(key[4:])
    if len(matches) != 1:
        raise SourceError("SEC ticker is unresolved or ambiguous in the registry; fetch its 10-digit CIK first. No ticker directory is queried.")
    return matches.pop()


def _sec_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("invalid SEC date")
    date.fromisoformat(value)
    return value


def _sec_submissions(payload, cik, query, limit):
    if not isinstance(payload, dict) or payload.get("error") or payload.get("errors"):
        raise ValueError("expected submissions object")
    if _integer(payload.get("cik")) != int(cik):
        raise ValueError("submissions CIK mismatch")
    name, entity_type = _text(payload.get("name")), _text(payload.get("entityType"))
    tickers = payload.get("tickers")
    if (not name or not entity_type or not isinstance(tickers, list)
            or any(not isinstance(t, str) or not t.strip() or "," in t for t in tickers)):
        raise ValueError("missing name, entityType or ticker array")
    if not query.isdigit() and query not in tickers:
        raise ValueError("ticker no longer matches submissions; use the CIK")
    recent = payload["filings"]["recent"]
    files = payload["filings"].get("files", [])
    if not isinstance(recent, dict) or not isinstance(files, list):
        raise ValueError("invalid filings object")
    fields = ("form", "filingDate", "accessionNumber", "primaryDocument")
    columns = [recent[field] for field in fields]
    if any(not isinstance(column, list) for column in columns) or len({len(column) for column in columns}) != 1:
        raise ValueError("filing arrays must have equal lengths")
    filings = []
    for form, filed, accession, document in zip(*columns):
        segments = document.split("/") if isinstance(document, str) else []
        if (not _text(form) or not isinstance(accession, str)
                or not re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", accession)
                or not segments or not all(segments)
                or any(s in (".", "..") for s in segments)
                or "\\" in document or any(ord(c) < 32 for c in document)):
            raise ValueError("invalid filing row")
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{quote(document, safe='.-_/')}"
        filings.append((form, _sec_date(filed), document, url))
    filings.sort(key=lambda row: (row[1], row[3]), reverse=True)
    return {
        "key": f"cik:{cik}", "kind": "company" if entity_type == "operating" else "issuer",
        "name": name, "website": _website(payload.get("website")),
        "attrs": {"source_id": cik, "sec.cik": cik, "sec.entity_type": entity_type,
                  "sec.tickers": ",".join(tickers), "sec.website": _text(payload.get("website")) or None},
        "filings": filings[:limit],
    }, len(filings), bool(files)


def _sec_facts(payload, cik):
    if (not isinstance(payload, dict) or _integer(payload.get("cik")) != int(cik)
            or not isinstance(payload.get("facts"), dict)):
        raise ValueError("invalid companyfacts object or CIK mismatch")
    gaap = payload["facts"].get("us-gaap", {})
    if not isinstance(gaap, dict):
        raise ValueError("invalid us-gaap object")
    tags = {
        "total_assets": ("Assets",),
        "total_liabilities": ("Liabilities",),
        "total_equity": ("StockholdersEquity",),
        "net_income": ("NetIncomeLoss",),
        "revenue": ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"),
        "cash": ("CashAndCashEquivalentsAtCarryingValue",),
    }
    metrics, missing = [], []  # type: list[tuple[str, float, str]], list[str]

    def candidates(tag):
        units = _object(_object(gaap.get(tag)).get("units")).get("USD", [])
        rows = []
        for fact in units if isinstance(units, list) else []:
            try:
                value = fact["val"]
                if type(value) not in (int, float) or not math.isfinite(value):
                    continue
                end, filed = _sec_date(fact["end"]), _sec_date(fact["filed"])
                start = _sec_date(fact["start"]) if fact.get("start") else ""
                rows.append((end, filed, start, json.dumps(fact, sort_keys=True), value))
            except (KeyError, ValueError, TypeError, OverflowError):
                continue
        return rows

    for metric, tag_names in tags.items():
        pool: list[tuple[str, str, str, int, str, float]] = []
        for index, tag in enumerate(tag_names):
            pool.extend((end, filed, start, index, identity, value)
                        for end, filed, start, identity, value in candidates(tag))
        if not pool:
            missing.extend(tag_names)
            continue
        latest = max((row[0], row[1]) for row in pool)
        tied = [row for row in pool if (row[0], row[1]) == latest]
        longest = min(row[2] or "9999-99-99" for row in tied)
        finalists = [row for row in tied if (row[2] or "9999-99-99") == longest]
        priority = min(row[3] for row in finalists)
        finalists = [row for row in finalists if row[3] == priority]
        end, _, _, _, _, value = max(finalists, key=lambda row: row[4])
        metrics.append((metric, value, end))
    return metrics, missing


def _fetch_sec(conn, query, limit, financials, result):
    cik = _sec_cik(conn, query)
    client = HTTPClient()
    url = f"{SOURCES['SEC_SUBMISSIONS']}/CIK{cik}.json"
    try:
        record, total, historical = _sec_submissions(client.get_json(url), cik, query, limit)
        record["attrs"].update({"source": "sec", "source_url": url, "retrieved_at": client.retrieved_at})
        result.update(fetched=1, total=1, pages=1, truncated=total > limit or historical)
        if result["truncated"]:
            result["warnings"].append("SEC filings are partial: only recent submissions filings, capped by limit; historical files are not fetched. Existing filings are retained.")
        if financials:
            facts_url = f"{SOURCES['SEC_FACTS']}/CIK{cik}.json"
            facts_client = HTTPClient(max_bytes=20 * 1024 * 1024)
            try:
                metrics, missing = _sec_facts(facts_client.get_json(facts_url), cik)
            except SourceError as exc:
                raise SourceError(f"SEC companyfacts failed (20 MiB cap; submissions retain the 2 MiB cap). {exc}") from exc
            record["metrics"] = metrics
            record["attrs"].update({"sec.facts_retrieved": 1, "sec.facts_url": facts_url,
                                    "sec.facts_retrieved_at": facts_client.retrieved_at,
                                    "sec.facts_missing": ",".join(missing),
                                    "sec.facts_currency": "USD",
                                    "sec.facts_selection": "max(end, filed), then lexicographically greatest canonical JSON for exact ties; no form or frame preference"})
            result["pages"] += 1
            result["warnings"].extend(facts_client.warnings)
    except (SourceError, KeyError, TypeError, ValueError, OverflowError) as exc:
        raise SourceError(f"sec: fetch failed; no ingestion writes applied. {exc}") from exc
    return _store(conn, "sec", [record], result, client)


def fetch_source(conn, source: str, query: str = '', country: str = '', limit: int = 25,
                 indicator: str = 'NY.GDP.MKTP.CD', category: str = '',
                 financials: bool = False) -> FetchResult:
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
    if not isinstance(category, str) or category not in ('', 'FUND', 'SOLE_PROPRIETOR'):
        raise SourceError("category must be FUND or SOLE_PROPRIETOR, or empty.")
    if category and source != "gleif":
        raise SourceError("category is supported only for gleif.")
    if type(financials) is not bool or (financials and source != "sec"):
        raise SourceError("financials must be boolean and is supported only for sec.")
    if source == "sec":
        query = query.upper()
        if country:
            raise SourceError("SEC country must be empty.")
        if not re.fullmatch(r"(?:[A-Z]{1,10}|[0-9]{10})", query):
            raise SourceError("SEC query must be a ticker (1..10 letters) or a 10-digit CIK.")
    result: FetchResult = {"source": source, "fetched": 0, "stored": 0, "warnings": [],
              "truncated": False, "total": None, "pages": 0,
              "coverage": next(item[2] for item in _CATALOG if item[0] == source)}
    if source == "sec":
        return _fetch_sec(conn, query, limit, financials, result)
    client = HTTPClient()
    size = min(limit, FETCH_PAGE_SIZE)
    mapped = []
    seen_pages = set()
    seen_records = set()
    skipped = 0
    missing = 0
    estimated_total = False
    mapper = {"gleif": _gleif, "fdic": _fdic, "worldbank": _worldbank, "osfi": _osfi}[source]
    for page in range(1, FETCH_MAX_PAGES + 1):
        url = _request_url(source, query, country, indicator, size, page, result["fetched"], category)
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
    return _store(conn, source, mapped, result, client)


def _store(conn, source, mapped, result, client):
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
            metrics = record.pop("metrics", [])
            filings = record.pop("filings", [])
            if source in ("gleif", "sec"):
                kinds = ("legal_entity", "fund") if source == "gleif" else ("company", "issuer")
                conn.execute("UPDATE entities SET kind=? WHERE key=? AND kind IN (?, ?)",
                             (record["kind"], record["key"], *kinds))
            eid = upsert_entity(conn, **record)
            if source in ("gleif", "sec"):
                conn.execute("UPDATE entities SET website=? WHERE id=?", (record["website"], eid))
                conn.execute("DELETE FROM attributes WHERE entity_id=? AND k=?",
                             (eid, source + ".website"))
            for form, filed, title, url in filings:
                add_filing(conn, eid, form, filed, title, url, source="sec")
            for key, value, period in metrics:
                add_metric(conn, eid, key, value, period, source="sec")
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
