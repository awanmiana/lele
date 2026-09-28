import hashlib
import json
import re
from ..core import clock
from datetime import date, datetime, timedelta, UTC
from urllib.parse import quote

from ..analysis import observations
from ..core import importer, registry
from ..core.constants import SOURCES
from .form4 import _decimal
from .http import HTTPClient, SourceError

MAX_LIMIT_DEFAULT = 25
MAX_LIMIT = 100
MAX_UEIS = 20
SOURCE = "usaspending"
AWARD_GROUPS = {"contract": ("A", "B", "C", "D"), "grant": ("02", "03", "04", "05")}
AWARD_FIELDS = ["Award ID", "Recipient Name", "Recipient UEI", "Award Amount",
                "Awarding Agency", "Start Date", "End Date"]
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
UEI_RE = re.compile(r"[A-Za-z0-9]{12}")


def _iso_date(value, label):
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise ValueError(f"{label} must be an ISO date YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a valid ISO date") from exc


def _award_kind(generated_id, award_id, hint):
    text = generated_id if isinstance(generated_id, str) else ""
    if text.startswith("CONT_"):
        return "contract"
    if text.startswith("ASST_"):
        return "grant"
    return hint if hint else ("award" if award_id else "unknown")


def _observation(result, entity_key, ueis, search, retrieved_at, hint=""):
    amount = _decimal(str(result.get("Award Amount"))) if result.get("Award Amount") is not None else None
    if amount is None or amount <= 0:
        return None
    award_id = result.get("Award ID") if isinstance(result.get("Award ID"), str) else ""
    generated = (result.get("generated_internal_id")
                 if isinstance(result.get("generated_internal_id"), str) else "")
    agency = result.get("Awarding Agency") if isinstance(result.get("Awarding Agency"), str) else ""
    slug = result.get("agency_slug") if isinstance(result.get("agency_slug"), str) else ""
    start = result.get("Start Date") if isinstance(result.get("Start Date"), str) else ""
    end = result.get("End Date") if isinstance(result.get("End Date"), str) else ""
    recipient_name = (result.get("Recipient Name")
                      if isinstance(result.get("Recipient Name"), str) else "")
    recipient_uei = (result.get("Recipient UEI")
                     if isinstance(result.get("Recipient UEI"), str) else "")
    kind = _award_kind(generated, award_id, hint)
    identifier = generated or award_id
    actor_key = f"usaspending:agency:{slug or result.get('awarding_agency_id') or agency}"
    source_url = f"https://www.usaspending.gov/award/{quote(identifier, safe='_-')}"
    description = (f"USAspending {kind} award {award_id or identifier}: {agency or 'agency'} -> "
                   f"{recipient_name or 'recipient'} {amount} USD")
    if start:
        description += f" (start {start}"
        if end:
            description += f", end {end}"
        description += ")"
    evidence = json.dumps({
        "award_id": award_id, "generated_internal_id": generated, "award_kind": kind,
        "awarding_agency": agency, "awarding_agency_id": result.get("awarding_agency_id"),
        "agency_slug": slug, "recipient_name": recipient_name, "recipient_uei": recipient_uei,
        "matched_ueis": sorted(ueis), "search_text": search,
        "start_date": start, "end_date": end, "amount_usd": str(amount),
        "amount_semantics": "federal award obligation/assistance amount as reported by USAspending",
    }, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return {
        "source": SOURCE, "external_id": identifier, "kind": "fund_flow",
        "description": description[:4096], "actor": actor_key, "counterparty": entity_key,
        "instrument": entity_key, "action": "", "reason": "", "reason_basis": "unknown",
        "amount": str(amount), "unit": "currency", "currency": "USD", "basis": "observed",
        "occurred_at": start or None, "observed_at": retrieved_at, "available_at": retrieved_at,
        "source_url": source_url, "evidence": evidence,
    }


def fetch_awards(conn, entity_id, ueis, limit=MAX_LIMIT_DEFAULT, start=None, end=None, search=None):
    if type(entity_id) is not int or not 1 <= entity_id <= 2**63 - 1:
        raise ValueError("entity id must be a positive SQLite integer")
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be an integer from 1 to {MAX_LIMIT}")
    entity = registry.get_entity(conn, entity_id)
    if entity is None:
        raise ValueError(f"entity {entity_id} not found")
    if isinstance(ueis, str):
        ueis = [ueis]
    if (not isinstance(ueis, (list, tuple)) or not ueis or len(ueis) > MAX_UEIS
            or not all(isinstance(item, str) and UEI_RE.fullmatch(item) for item in ueis)):
        raise ValueError(f"1..{MAX_UEIS} explicit 12-character recipient UEIs are required; "
                         "recipients are never matched by name")
    expected = {item.upper() for item in ueis}
    if search is None:
        search = entity["name"]
    importer._text(search, "search", 256, True)
    today = clock.now().date()
    finish = _iso_date(end, "end") if end is not None else today
    begin = _iso_date(start, "start") if start is not None else finish - timedelta(days=365)
    if begin > finish:
        raise ValueError("start must not be after end")
    started = clock.now()
    client = HTTPClient(ttl=0)
    url = SOURCES["USASPENDING_AWARDS"] + "/"
    periods = [{"start_date": begin.isoformat(), "end_date": finish.isoformat()}]
    results: list[tuple] = []
    by_group: dict = {}
    payloads: dict = {}
    truncated = False
    for group, codes in AWARD_GROUPS.items():
        body = {"filters": {"recipient_search_text": [search], "time_period": periods,
                            "award_type_codes": list(codes)},
                "fields": AWARD_FIELDS, "page": 1, "limit": limit, "sort": "Award Amount",
                "order": "desc"}
        try:
            payload = client.post_json(url, body)
        except (SourceError, ValueError, TypeError) as exc:
            raise SourceError(f"usaspending: award search failed; no writes applied. {exc}") from exc
        group_results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(group_results, list):
            raise SourceError("usaspending: unexpected award-search response")
        payloads[group] = payload
        by_group[group] = len(group_results)
        truncated = truncated or len(group_results) >= limit
        results.extend((group, row) for row in group_results)
    if client.retrieved_at:
        retrieved = (datetime.fromisoformat(client.retrieved_at.replace("Z", "+00:00"))
                     .astimezone(UTC).replace(microsecond=0).isoformat())
    else:
        retrieved = clock.now().replace(microsecond=0).isoformat()
    rows, skipped, unmatched = [], 0, 0
    for group, result in results:
        if not isinstance(result, dict):
            skipped += 1
            continue
        if (result.get("Recipient UEI") or "").upper() not in expected:
            unmatched += 1
            continue
        observation = _observation(result, entity["key"], expected, search, retrieved, group)
        if observation is None:
            skipped += 1
            continue
        rows.append(observation)
    unique = {row["external_id"]: row for row in rows}
    stored = observations.store_observations(conn, list(unique.values()))
    finished = clock.now()
    canonical = json.dumps(payloads, sort_keys=True, ensure_ascii=True, separators=(",", ":"),
                           allow_nan=False).encode("utf-8")
    warnings = list(client.warnings)
    if truncated:
        warnings.append("an award group returned the full page; older awards may be absent")
    if unmatched:
        warnings.append(f"{unmatched} candidate award(s) were excluded because their Recipient UEI "
                        "was not in the explicit set")
    registry.record_ingest_run(
        conn, SOURCE, started.isoformat(), finished.isoformat(), query=",".join(sorted(expected)),
        fetched=len(results), stored=stored["imported"], skipped=skipped, pages=len(AWARD_GROUPS),
        truncated=truncated, request_sha256=hashlib.sha256(canonical).hexdigest(),
        warnings=warnings, coverage=f"awards {begin.isoformat()}..{finish.isoformat()}",
        pages_detail=[{"page": index, "offset": 0, "rows": by_group[group], "selected": 0}
                      for index, group in enumerate(AWARD_GROUPS, start=1)])
    return {
        "source": SOURCE, "entity_id": entity_id, "entity_key": entity["key"],
        "recipient_ueis": sorted(expected), "search_text": search, "source_url": url,
        "retrieved_at": client.retrieved_at,
        "period": {"start": begin.isoformat(), "end": finish.isoformat()},
        "candidates": len(results), "by_award_kind": by_group, "observations": stored["imported"],
        "skipped": {"no_positive_amount": skipped, "uei_not_matched": unmatched},
        "by_kind": stored["kinds"], "truncated": truncated, "warnings": warnings,
        "limitations": [
            "USAspending award amounts are federal obligation/assistance figures, not settled cash "
            "transfers; a negative, zero or missing amount is skipped rather than signed.",
            "USAspending ignores exact recipient-id filters on this endpoint, so candidates come "
            "from a name search and only awards whose Recipient UEI exactly matches an explicitly "
            "supplied UEI are kept; awards under other UEIs or names are not retrieved.",
            "Only one bounded page per award group (contracts, assistance) is queried; contracts "
            "and grants cannot be combined in one API request.",
            "The agency is recorded as a textual origin key, not a verified registry entity; the "
            "recipient is bound to the selected registry entity only through the explicit UEI set.",
            "available_at is the local retrieval time because USAspending exposes no per-award "
            "publication timestamp; occurred_at is the award start date.",
        ],
    }
