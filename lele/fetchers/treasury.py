import hashlib
import json
import re
from ..core import clock
from datetime import date, timedelta
from decimal import InvalidOperation, Decimal
from urllib.parse import urlencode

from ..analysis import observations
from ..core import registry
from .http import HTTPClient, SourceError

MAX_LIMIT_DEFAULT = 25
MAX_LIMIT = 100
SOURCE = "treasury-fiscal-data"
TREASURY_ENDPOINT = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/dts/operating_cash_balance"
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _iso_date(value, label):
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise ValueError(f"{label} must be an ISO date YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
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


def _observation(record, retrieved_at):
    record_date = record.get("record_date")
    if not isinstance(record_date, str) or not DATE_RE.fullmatch(record_date):
        return None
    balance = _decimal(str(record.get("open_today_bal"))) if record.get("open_today_bal") is not None else None
    if balance is None or balance <= 0:
        return None
    description = (f"Treasury Daily Operating Cash Balance: "
                   f"${balance:,.0f} on {record_date}")
    evidence = json.dumps({
        "record_date": record_date,
        "open_today_bal": str(balance),
        "open_month_bal": record.get("open_month_bal"),
        "open_fiscal_year_bal": record.get("open_fiscal_year_bal"),
        "table_nbr": record.get("table_nbr"),
        "table_nm": record.get("table_nm"),
        "record_fiscal_year": record.get("record_fiscal_year"),
        "record_calendar_year": record.get("record_calendar_year"),
        "amount_semantics": "Daily Treasury Statement operating cash balance (Treasury General Account), as published by the U.S. Department of the Treasury",
    }, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return {
        "source": SOURCE, "external_id": f"dts-{record_date}",
        "kind": "macro_release", "description": description[:4096],
        "actor": "", "counterparty": "", "instrument": "",
        "action": "", "reason": "", "reason_basis": "unknown",
        "amount": str(balance), "unit": "currency", "currency": "USD",
        "basis": "observed", "occurred_at": record_date,
        "observed_at": retrieved_at, "available_at": retrieved_at,
        "source_url": f"{TREASURY_ENDPOINT}?record_date={record_date}",
        "evidence": evidence,
    }


def fetch_treasury(conn, limit=MAX_LIMIT_DEFAULT, start=None, end=None):
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be an integer from 1 to {MAX_LIMIT}")
    today = clock.now().date()
    finish = _iso_date(end, "end") if end is not None else today
    begin = _iso_date(start, "start") if start is not None else finish - timedelta(days=365)
    if begin > finish:
        raise ValueError("start must not be after end")
    started = clock.now()
    registry.record_failures(conn, source=SOURCE, started=started, query=f'{begin.isoformat()}..{finish.isoformat()}')
    client = HTTPClient(ttl=0)
    query = urlencode({"sort": "-record_date", "filter[record_date][gte]": begin.isoformat(),
                        "filter[record_date][lte]": finish.isoformat(),
                        "page[size]": limit})
    url = f"{TREASURY_ENDPOINT}?{query}"
    try:
        payload = client.get_json(url)
    except (SourceError, ValueError, TypeError) as exc:
        raise SourceError(f"treasury-fiscal-data: daily cash balance fetch failed; "
                          f"no writes applied. {exc}") from exc
    results = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        raise SourceError("treasury-fiscal-data: unexpected daily cash balance response")
    retrieved = clock.now().replace(microsecond=0).isoformat()
    rows, skipped = [], 0
    truncated = len(results) >= limit
    for record in results:
        if not isinstance(record, dict):
            skipped += 1
            continue
        observation = _observation(record, retrieved)
        if observation is None:
            skipped += 1
            continue
        rows.append(observation)
    stored = observations.store_observations(conn, rows)
    finished = clock.now()
    canonical = json.dumps({"endpoint": "operating_cash_balance",
                             "filters": {"sort": "-record_date",
                                          "record_date[gte]": begin.isoformat(),
                                          "record_date[lte]": finish.isoformat()},
                             "limit": limit},
                            sort_keys=True, ensure_ascii=True,
                            separators=(",", ":"), allow_nan=False).encode("utf-8")
    warnings = list(client.warnings)
    if truncated:
        warnings.append("the page returned the full limit; older data may be absent")
    registry.set_payload_fingerprint(conn, client)
    registry.clear_failure_recorder(conn)
    registry.record_ingest_run(
        conn, SOURCE, started.isoformat(), finished.isoformat(),
        query=f"{begin.isoformat()}..{finish.isoformat()}",
        fetched=len(results), stored=stored["imported"], skipped=skipped,
        pages=1, truncated=truncated, request_sha256=hashlib.sha256(canonical).hexdigest(),
        warnings=warnings,
        coverage=f"operating cash balance {begin.isoformat()}..{finish.isoformat()}")
    return {
        "source": SOURCE, "endpoint": "operating_cash_balance", "source_url": url,
        "retrieved_at": client.retrieved_at,
        "period": {"start": begin.isoformat(), "end": finish.isoformat()},
        "records": len(results), "observations": stored["imported"],
        "skipped": skipped, "by_kind": stored["kinds"], "truncated": truncated,
        "warnings": warnings,
        "limitations": [
            "The Daily Treasury Statement operating cash balance is a macro aggregate "
            "(Treasury General Account) with no counterparty or recipient identity; it "
            "cannot be bound to a specific registry entity or instrument.",
            "Only one bounded page is queried; amounts are reported balances, not "
            "settled cash transfers.",
            "occurred_at is the record date and available_at is the retrieval time; "
            "the Treasury Fiscal Data API does not expose a per-record publication "
            "timestamp.",
        ],
    }
