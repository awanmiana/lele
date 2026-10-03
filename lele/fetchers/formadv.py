"""SEC Form ADV (Investment Adviser Public Disclosure) fetcher.

Fetches investment adviser data from SEC IAPD API.
Maps to `investment_adviser` observations with firm details, registrations, and disclosures.
"""
import json
import re
from ..core import clock
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from ..analysis import observations
from ..core import registry
from .http import HTTPClient, SourceError
from . import prices

MAX_LIMIT_DEFAULT = 100
MAX_LIMIT = 1000
SOURCE = "sec-formadv"
BASE_URL = "https://api.adviserinfo.sec.gov"
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _iso_date(value, label):
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise ValueError(f"{label} must be an ISO date YYYY-MM-DD")
    try:
        return datetime.fromisoformat(value).date()
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


def _observation(firm, retrieved_at):
    src = firm.get("_source", {})
    firm_source_id = src.get("firm_source_id", "")
    firm_name = src.get("firm_name", "")
    if not firm_source_id or not firm_name:
        return None

    evidence = json.dumps({
        "firm_source_id": firm_source_id,
        "firm_ia_sec_number": src.get("firm_ia_sec_number", ""),
        "firm_ia_full_sec_number": src.get("firm_ia_full_sec_number", ""),
        "firm_name": firm_name,
        "firm_other_names": src.get("firm_other_names", []),
        "firm_ia_scope": src.get("firm_ia_scope", ""),
        "firm_ia_disclosure_fl": src.get("firm_ia_disclosure_fl", ""),
        "firm_branches_count": src.get("firm_branches_count", 0),
        "firm_ia_address_details": src.get("firm_ia_address_details", {}),
    }, sort_keys=True, ensure_ascii=True, separators=(",", ":"))

    return {
        "source": SOURCE,
        "external_id": f"formadv-firm-{firm_source_id}",
        "kind": "investment_adviser",
        "description": f"SEC Form ADV: {firm_name} (IA SEC #{src.get('firm_ia_sec_number', '')})",
        "actor": f"sec-adv:firm:{firm_source_id}",
        "counterparty": "",
        "instrument": "",
        "action": "",
        "reason": "",
        "reason_basis": "unknown",
        "amount": None,
        "unit": "",
        "currency": "",
        "basis": "observed",
        "occurred_at": retrieved_at.date().isoformat(),
        "observed_at": retrieved_at.isoformat(),
        "available_at": retrieved_at.isoformat(),
        "source_url": f"{BASE_URL}/firm/summary/{firm_source_id}",
        "evidence": evidence,
    }


def fetch_formadv(conn, query="", limit=MAX_LIMIT_DEFAULT, start=0):
    """Fetch SEC Form ADV investment adviser firms.

    Args:
        conn: SQLite connection
        query: Search query for firm name (default: empty for all)
        limit: Maximum firms to fetch (1..1000)
        start: Pagination offset (default: 0)
    """
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be an integer from 1 to {MAX_LIMIT}")
    if type(start) is not int or start < 0:
        raise ValueError("start must be a non-negative integer")

    started = clock.now()
    registry.record_failures(conn, source=SOURCE, started=started, query=f"Form ADV search query:{query or 'all'} limit:{limit}")
    client = HTTPClient(ttl=0)

    params = {
        "query": query,
        "start": start,
        "rows": limit,
        "sort": "firm_name",
        "order": "asc",
    }
    url = f"{BASE_URL}/search/firm?{urlencode(params)}"

    try:
        payload = client.get_json(url)
    except (SourceError, ValueError, TypeError) as exc:
        raise SourceError(f"sec-formadv: search failed; no writes applied. {exc}") from exc

    hits = payload.get("hits", {}).get("hits", [])
    total = payload.get("hits", {}).get("total", 0)

    rows = []
    for hit in hits:
        try:
            retrieved_dt = prices._retrieved(client.retrieved_at).replace(microsecond=0)
            obs = _observation(hit, retrieved_dt)
            if obs:
                rows.append(obs)
        except (TypeError, ValueError, KeyError):
            continue

    stored = observations.store_observations(conn, rows)
    finished = clock.now()

    import hashlib
    canonical = json.dumps({
        "endpoint": "api.adviserinfo.sec.gov/search/firm",
        "params": params,
    }, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("utf-8")

    warnings = list(client.warnings)
    registry.set_payload_fingerprint(conn, client)
    registry.clear_failure_recorder(conn)
    registry.record_ingest_run(
        conn, SOURCE, started.isoformat(), finished.isoformat(),
        query=f"Form ADV search query:{query or 'all'} limit:{limit}",
        fetched=len(hits), stored=stored["imported"], skipped=len(hits) - stored["imported"],
        pages=1, truncated=len(hits) >= limit,
        request_sha256=hashlib.sha256(canonical).hexdigest(),
        warnings=warnings,
        coverage=f"SEC Form ADV investment adviser firms {len(hits)}/{total}")
    return {
        "source": SOURCE,
        "endpoint": "api.adviserinfo.sec.gov/search/firm",
        "source_url": BASE_URL,
        "retrieved_at": prices._retrieved(client.retrieved_at).replace(microsecond=0).isoformat(),
        "period": {"start": started.date().isoformat(), "end": finished.date().isoformat()},
        "records": len(hits),
        "total_available": total,
        "observations": stored["imported"],
        "skipped": len(hits) - stored["imported"],
        "by_kind": stored["kinds"],
        "truncated": len(hits) >= limit,
        "warnings": warnings,
        "limitations": [
            "SEC IAPD API provides investment adviser firm and individual registrations.",
            "Data sourced from Form ADV filings (uniform application for investment adviser registration).",
            "Firm search returns basic registration info; individual search available separately.",
            "Disclosure flag (firm_ia_disclosure_fl) indicates regulatory events; details require separate fetch.",
            "occurred_at is retrieval date; available_at is retrieval time.",
            "Free public API; rate limits may apply.",
        ],
    }


def fetch_formadv_individual(conn, query="", limit=MAX_LIMIT_DEFAULT, start=0):
    """Fetch SEC Form ADV investment adviser individuals (representatives).

    Args:
        conn: SQLite connection
        query: Search query for individual name
        limit: Maximum individuals to fetch (1..1000)
        start: Pagination offset
    """
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be an integer from 1 to {MAX_LIMIT}")

    started = clock.now()
    registry.record_failures(conn, source=SOURCE, started=started, query=f'Form ADV individual search query:{query} limit:{limit}')
    client = HTTPClient(ttl=0)

    params = {
        "query": query,
        "start": start,
        "rows": limit,
        "sort": "individual_name",
        "order": "asc",
    }
    url = f"{BASE_URL}/search/individual?{urlencode(params)}"

    try:
        payload = client.get_json(url)
    except (SourceError, ValueError, TypeError) as exc:
        raise SourceError(f"sec-formadv-individual: search failed; {exc}") from exc

    hits = payload.get("hits", {}).get("hits", [])
    total = payload.get("hits", {}).get("total", 0)

    rows = []
    for hit in hits:
        try:
            src = hit.get("_source", {})
            ind_source_id = src.get("ind_source_id", "")
            ind_firstname = src.get("ind_firstname", "")
            ind_middlename = src.get("ind_middlename", "")
            ind_lastname = src.get("ind_lastname", "")
            ind_other_names = src.get("ind_other_names", [])
            ind_name = f"{ind_firstname} {ind_lastname}".strip() or " ".join(ind_other_names)
            if not ind_source_id or not ind_name:
                continue

            evidence = json.dumps({
                "individual_source_id": ind_source_id,
                "individual_name": ind_name,
                "individual_firstname": ind_firstname,
                "individual_middlename": ind_middlename,
                "individual_lastname": ind_lastname,
                "individual_other_names": ind_other_names,
                "individual_bc_scope": src.get("ind_bc_scope", ""),
                "individual_ia_scope": src.get("ind_ia_scope", ""),
                "individual_ia_disclosure_fl": src.get("ind_ia_disclosure_fl", ""),
                "individual_approved_finra_registration_count": src.get("ind_approved_finra_registration_count", 0),
                "individual_employments_count": src.get("ind_employments_count", 0),
            }, sort_keys=True, ensure_ascii=True, separators=(",", ":"))

            rows.append({
                "source": SOURCE,
                "external_id": f"formadv-individual-{ind_source_id}",
                "kind": "investment_adviser",
                "description": f"SEC Form ADV individual: {ind_name}",
                "actor": f"sec-adv:individual:{ind_source_id}",
                "counterparty": "",
                "instrument": "",
                "action": "",
                "reason": "",
                "reason_basis": "unknown",
                "amount": None,
                "unit": "",
                "currency": "",
                "basis": "observed",
                "occurred_at": prices._retrieved(client.retrieved_at).replace(microsecond=0).date().isoformat(),
                "observed_at": prices._retrieved(client.retrieved_at).replace(microsecond=0).isoformat(),
                "available_at": prices._retrieved(client.retrieved_at).replace(microsecond=0).isoformat(),
                "source_url": f"{BASE_URL}/individual/summary/{ind_source_id}",
                "evidence": evidence,
            })
        except (TypeError, ValueError, KeyError):
            continue

    stored = observations.store_observations(conn, rows)
    finished = clock.now()

    import hashlib
    canonical = json.dumps({
        "endpoint": "api.adviserinfo.sec.gov/search/individual",
        "params": params,
    }, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("utf-8")

    warnings = list(client.warnings)
    registry.set_payload_fingerprint(conn, client)
    registry.clear_failure_recorder(conn)
    registry.record_ingest_run(
        conn, SOURCE, started.isoformat(), finished.isoformat(),
        query=f"Form ADV individual search query:{query} limit:{limit}",
        fetched=len(hits), stored=stored["imported"], skipped=len(hits) - stored["imported"],
        pages=1, truncated=len(hits) >= limit,
        request_sha256=hashlib.sha256(canonical).hexdigest(),
        warnings=warnings,
        coverage=f"SEC Form ADV investment adviser individuals {len(hits)}/{total}")
    return {
        "source": SOURCE,
        "endpoint": "api.adviserinfo.sec.gov/search/individual",
        "source_url": BASE_URL,
        "retrieved_at": prices._retrieved(client.retrieved_at).replace(microsecond=0).isoformat(),
        "period": {"start": started.date().isoformat(), "end": finished.date().isoformat()},
        "records": len(hits),
        "total_available": total,
        "observations": stored["imported"],
        "skipped": len(hits) - stored["imported"],
        "by_kind": stored["kinds"],
        "truncated": len(hits) >= limit,
        "warnings": warnings,
        "limitations": [
            "SEC IAPD API provides investment adviser individual (representative) registrations.",
            "Individuals are linked to firms via current_firms and employments history.",
            "CRD number is the Central Registration Depository identifier.",
            "occurred_at is retrieval date; available_at is retrieval time.",
        ],
    }
