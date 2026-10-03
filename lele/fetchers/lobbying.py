import hashlib
import json
import re
from ..core import clock
from datetime import datetime, UTC
from urllib.parse import urlencode

from ..analysis import observations
from ..core import registry
from ..core.constants import SOURCES
from .form4 import _decimal
from .http import HTTPClient, SourceError

MAX_LIMIT_DEFAULT = 25
MAX_LIMIT = 100
SOURCE = "senate-lda"
QUARTERS = ("Q1", "Q2", "Q3", "Q4")
PERIOD_START = {"first_quarter": (1, 1), "second_quarter": (4, 1), "third_quarter": (7, 1),
                "fourth_quarter": (10, 1)}
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _id(value, label):
    if type(value) is not int or not 1 <= value <= 2**63 - 1:
        raise ValueError(f"{label} must be a positive integer LDA id")
    return value


def _quarter_start(period, year):
    months = PERIOD_START.get(period)
    if months is None:
        return None
    return f"{int(year):04d}-{months[0]:02d}-{months[1]:02d}"


def _posted(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp.astimezone(UTC).replace(microsecond=0).isoformat()


def _party(value):
    if not isinstance(value, dict):
        return None
    identifier = value.get("id")
    name = value.get("name")
    if type(identifier) is not int or not isinstance(name, str) or not name.strip():
        return None
    return {"id": identifier, "name": name.strip()}


def _observation(record, entity_key, binding, retrieved_at):
    income = _decimal(str(record.get("income"))) if record.get("income") is not None else None
    if income is None or income <= 0:
        return None
    client = _party(record.get("client"))
    registrant = _party(record.get("registrant"))
    if client is None or registrant is None:
        return None
    filing_uuid = record.get("filing_uuid")
    if not isinstance(filing_uuid, str) or not filing_uuid.strip():
        return None
    filing_year = record.get("filing_year")
    period = record.get("filing_period")
    occurred = _quarter_start(period, filing_year)
    posted = _posted(record.get("dt_posted")) or retrieved_at
    document = record.get("filing_document_url")
    source_url = document if isinstance(document, str) and document.startswith("https://") else None
    if source_url is None:
        api_url = record.get("url")
        source_url = api_url if isinstance(api_url, str) and api_url.startswith("https://") else None
    if source_url is None:
        return None
    if binding == "client":
        actor_key, counterparty_key = entity_key, f"lda:registrant:{registrant['id']}"
    else:
        actor_key, counterparty_key = f"lda:client:{client['id']}", entity_key
    period_label = record.get("filing_period_display") or period or ""
    description = (f"Senate LDA lobbying income: {client['name']} -> {registrant['name']} "
                   f"{income} USD ({period_label} {filing_year})")
    activities = record.get("lobbying_activities")
    issue_set: set[str] = set()
    if isinstance(activities, list):
        for item in activities:
            code = item.get("general_issue_code") if isinstance(item, dict) else None
            if isinstance(code, str):
                issue_set.add(code)
    issues = sorted(issue_set)
    evidence = json.dumps({
        "filing_uuid": filing_uuid, "filing_type": record.get("filing_type"),
        "filing_type_display": record.get("filing_type_display"), "filing_year": filing_year,
        "filing_period": period, "dt_posted": posted, "income_usd": str(income),
        "expenses": record.get("expenses"), "registrant": registrant, "client": client,
        "bound_party": binding, "issue_codes": issues,
        "amount_semantics": "registrant-reported lobbying income for the period, as filed under LDA",
    }, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return {
        "source": SOURCE, "external_id": filing_uuid, "kind": "fund_flow",
        "description": description[:4096], "actor": actor_key, "counterparty": counterparty_key,
        "instrument": entity_key, "action": "", "reason": "", "reason_basis": "unknown",
        "amount": str(income), "unit": "currency", "currency": "USD", "basis": "observed",
        "occurred_at": occurred, "observed_at": posted, "available_at": posted,
        "source_url": source_url, "evidence": evidence,
    }


def fetch_lobbying(conn, entity_id, client_id=None, registrant_id=None, limit=MAX_LIMIT_DEFAULT,
                   year=None):
    if type(entity_id) is not int or not 1 <= entity_id <= 2**63 - 1:
        raise ValueError("entity id must be a positive SQLite integer")
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be an integer from 1 to {MAX_LIMIT}")
    entity = registry.get_entity(conn, entity_id)
    if entity is None:
        raise ValueError(f"entity {entity_id} not found")
    if (client_id is None) == (registrant_id is None):
        raise ValueError("exactly one of --client-id or --registrant-id is required; LDA identity "
                         "is never inferred from a name")
    binding = "client" if client_id is not None else "registrant"
    party_id = _id(client_id if client_id is not None else registrant_id, binding + "_id")
    now = clock.now()
    if year is None:
        year = now.year
    if type(year) is not int or not 2008 <= year <= now.year + 1:
        raise ValueError(f"filing year must be an integer from 2008 to {now.year + 1}")
    started = now
    registry.record_failures(conn, source=SOURCE, started=started, query=str(party_id))
    client = HTTPClient(ttl=0)
    base = SOURCES["SENATE_LDA_FILINGS"] + "/"
    key = "client_id" if binding == "client" else "registrant_id"
    records: list = []
    payloads: dict = {}
    truncated = False
    for quarter in QUARTERS:
        query = urlencode({"filing_year": year, "filing_type": quarter, key: party_id,
                           "page_size": limit})
        url = base + "?" + query
        try:
            payload = client.get_json(url)
        except (SourceError, ValueError, TypeError) as exc:
            raise SourceError(f"senate-lda: filing search failed; no writes applied. {exc}") from exc
        group = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(group, list):
            raise SourceError("senate-lda: unexpected filing response")
        payloads[quarter] = payload
        truncated = truncated or len(group) >= limit
        records.extend(item for item in group if isinstance(item, dict))
    if client.retrieved_at:
        retrieved = (datetime.fromisoformat(client.retrieved_at.replace("Z", "+00:00"))
                     .astimezone(UTC).replace(microsecond=0).isoformat())
    else:
        retrieved = now.replace(microsecond=0).isoformat()
    rows, skipped = [], 0
    seen = set()
    for record in records:
        identifier = record.get("filing_uuid")
        if not isinstance(identifier, str) or identifier in seen:
            skipped += 1
            continue
        seen.add(identifier)
        observation = _observation(record, entity["key"], binding, retrieved)
        if observation is None:
            skipped += 1
            continue
        rows.append(observation)
    stored = observations.store_observations(conn, rows)
    finished = clock.now()
    canonical = json.dumps(payloads, sort_keys=True, ensure_ascii=True, separators=(",", ":"),
                           allow_nan=False).encode("utf-8")
    warnings = list(client.warnings)
    if truncated:
        warnings.append("a quarterly page returned the full limit; additional filings may exist")
    registry.clear_failure_recorder(conn)
    registry.record_ingest_run(
        conn, SOURCE, started.isoformat(), finished.isoformat(), query=str(party_id),
        fetched=len(records), stored=stored["imported"], skipped=skipped, pages=len(QUARTERS),
        truncated=truncated, request_sha256=hashlib.sha256(canonical).hexdigest(), warnings=warnings,
        coverage=f"{binding} id {party_id}, filing year {year}",
        pages_detail=[{"page": index, "offset": 0, "rows": len(payloads[q].get("results", [])),
                       "selected": 0} for index, q in enumerate(QUARTERS, start=1)])
    return {
        "source": SOURCE, "entity_id": entity_id, "entity_key": entity["key"],
        "bound_party": binding, "party_id": party_id, "filing_year": year, "source_url": base,
        "retrieved_at": client.retrieved_at, "filings": len(records),
        "observations": stored["imported"], "skipped": skipped, "by_kind": stored["kinds"],
        "truncated": truncated, "warnings": warnings,
        "limitations": [
            "Only the selected party's quarterly activity filings (Q1-Q4) for one year are read; "
            "registration, amendment and other filing types are ignored.",
            "Only filings with a positive registrant-reported income are stored; income is the fee "
            "the client paid the registrant for the period, not net of costs or a settled transfer.",
            "The party id is explicit; the other party is recorded as a textual key with its LDA id "
            "and name, and no identity is inferred from a name.",
            "occurred_at is the quarter start date and available_at is the LDA posting time; "
            "amounts are not summed across quarters, clients or registrants.",
        ],
    }
