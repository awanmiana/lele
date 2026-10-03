"""EIA (Energy Information Administration) energy data fetcher.

Fetches energy data from EIA API (requires free API key).
Maps to measurement-optional `energy_flow` observations with series detail.
"""
import json
import re
from ..core import clock
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from ..analysis import observations
from ..core import registry
from .http import HTTPClient, SourceError

MAX_LIMIT_DEFAULT = 100
MAX_LIMIT = 5000
SOURCE = "eia"
BASE_URL = "https://api.eia.gov/v2"
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


def _observation(record, retrieved_at, series_id, series_name):
    period = record.get("period", "")
    value = record.get("value")
    if not period or value is None:
        return None
    measurement = _decimal(str(value))
    if measurement is None:
        return None
    description = f"EIA: {series_name} ({series_id}) = {value} in {period}"
    evidence = json.dumps({
        "series_id": series_id,
        "series_name": series_name,
        "period": period,
        "value": str(value),
        "unit": record.get("unit", ""),
    }, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    source_url = f"{BASE_URL}/?api_key=*&series_id={series_id}"
    return {
        "source": SOURCE,
        "external_id": f"eia-{series_id}-{period}",
        "kind": "energy_flow",
        "description": description[:4096],
        "actor": f"eia:series:{series_id}",
        "counterparty": "",
        "instrument": "",
        "action": "",
        "reason": "",
        "reason_basis": "unknown",
        "amount": str(measurement),
        "unit": record.get("unit", "value"),
        "currency": "",
        "basis": "observed",
        "occurred_at": f"{period}-01" if len(period) == 7 else period,
        "observed_at": retrieved_at,
        "available_at": retrieved_at,
        "source_url": source_url,
        "evidence": evidence,
    }


def fetch_eia(conn, series_ids, api_key, limit=MAX_LIMIT_DEFAULT, start=None, end=None):
    """Fetch EIA energy data series.

    Args:
        conn: SQLite connection
        series_ids: List of EIA series IDs (e.g., ["PET.RWTC.D", "NG.RNGWHHD.D"])
        api_key: EIA API key (required, free from eia.gov)
        limit: Maximum records per series
        start: Start date YYYY-MM-DD
        end: End date YYYY-MM-DD
    """
    if not isinstance(series_ids, list) or not series_ids:
        raise ValueError("series_ids must be a non-empty list")
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be an integer from 1 to {MAX_LIMIT}")
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("api_key is required (get free key from eia.gov)")

    today = clock.now().date()
    end_date = _iso_date(end, "end") if end else today
    start_date = _iso_date(start, "start") if start else end_date - timedelta(days=365)
    if start_date > end_date:
        raise ValueError("start must not be after end")

    started = clock.now()
    registry.record_failures(conn, source=SOURCE, started=started, query=f"EIA series {','.join(series_ids)} {start_date}..{end_date}")
    client = HTTPClient(ttl=0)

    params = {
        "api_key": api_key,
        "frequency": "daily",
        "data[0]": "value",
        "start": start_date.isoformat(),
        "end": end_date.isoformat(),
        "sort[0][column]": "period",
        "sort[0][direction]": "desc",
        "offset": 0,
        "length": min(limit, 5000),
    }

    retrieved = clock.now().replace(microsecond=0).isoformat()
    all_rows = []

    for series_id in series_ids:
        series_params = params.copy()
        series_params["facets[series][]"] = series_id
        url = f"{BASE_URL}/?{urlencode(series_params)}"
        try:
            payload = client.get_json(url)
        except (SourceError, ValueError, TypeError) as exc:
            raise SourceError(f"eia: fetch failed for series {series_id}; "
                              f"no writes applied. {exc}") from exc

        response = payload.get("response") if isinstance(payload, dict) else None
        if not isinstance(response, dict):
            continue
        data = response.get("data")
        if not isinstance(data, list):
            continue
        series_name = response.get("name", series_id)

        for record in data[:limit]:
            if not isinstance(record, dict):
                continue
            observation = _observation(record, retrieved, series_id, series_name)
            if observation is None:
                continue
            all_rows.append(observation)

    stored = observations.store_observations(conn, all_rows)
    finished = clock.now()

    import hashlib
    canonical = json.dumps({
        "endpoint": "api.eia.gov/v2",
        "series_ids": series_ids, "limit": limit,
        "start": start_date.isoformat(), "end": end_date.isoformat()
    }, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("utf-8")

    warnings = list(client.warnings)
    registry.set_payload_fingerprint(conn, client)
    registry.clear_failure_recorder(conn)
    registry.record_ingest_run(
        conn, SOURCE, started.isoformat(), finished.isoformat(),
        query=f"EIA series {','.join(series_ids)} {start_date}..{end_date}",
        fetched=len(all_rows), stored=stored["imported"], skipped=0,
        pages=len(series_ids), truncated=False,
        request_sha256=hashlib.sha256(canonical).hexdigest(),
        warnings=warnings,
        coverage=f"EIA energy data {start_date}..{end_date}")
    return {
        "source": SOURCE,
        "endpoint": "api.eia.gov/v2",
        "source_url": BASE_URL,
        "retrieved_at": client.retrieved_at,
        "period": {"start": start_date.isoformat(), "end": end_date.isoformat()},
        "records": len(all_rows),
        "observations": stored["imported"],
        "skipped": 0,
        "by_kind": stored["kinds"],
        "truncated": False,
        "warnings": warnings,
        "limitations": [
            "EIA API requires a free API key from https://www.eia.gov/opendata/register.php",
            "Data includes petroleum, natural gas, electricity, coal, renewables, and more.",
            "Frequency can be daily, weekly, monthly, or annual depending on series.",
            "Each series is fetched separately; results are combined.",
            "occurred_at is the period date; available_at is retrieval time.",
            "Rate limits apply; this client respects per-host pacing.",
        ],
    }


def fetch_bls(conn, series_ids, limit=MAX_LIMIT_DEFAULT, start=None, end=None,
              registration_key=None):
    """Fetch BLS (Bureau of Labor Statistics) labor data.

    Args:
        conn: SQLite connection
        series_ids: List of BLS series IDs (e.g., ["LNS14000000", "JTS00000000JOL"])
        limit: Maximum records per series
        start: Start year (YYYY)
        end: End year (YYYY)
        registration_key: Optional BLS registration key for higher rate limits
    """
    if not isinstance(series_ids, list) or not series_ids:
        raise ValueError("series_ids must be a non-empty list")
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be an integer from 1 to {MAX_LIMIT}")

    today = clock.now().date()
    end_year = (int(end) if end.isdigit() else today.year) if end else today.year
    start_year = (int(start) if start.isdigit() else end_year - 1) if start else end_year - 1
    if start_year > end_year:
        raise ValueError("start must not be after end")

    started = clock.now()
    registry.record_failures(conn, source=SOURCE, started=started, query=f"BLS series {','.join(series_ids)} {start_year}..{end_year}")
    client = HTTPClient(ttl=0)

    payload = {
        "seriesid": series_ids,
        "startyear": str(start_year),
        "endyear": str(end_year),
        "catalog": False,
        "calculations": False,
        "annualaverage": False,
        "aspects": False,
    }
    if registration_key:
        payload["registrationkey"] = registration_key

    url = "https://api.bls.gov/publicAPI/v2/timeseries/data"
    try:
        response = client.post_json(url, payload)
    except (SourceError, ValueError, TypeError) as exc:
        raise SourceError(f"bls: fetch failed; no writes applied. {exc}") from exc

    if not isinstance(response, dict) or response.get("status") != "REQUEST_SUCCEEDED":
        msg = response.get("message", ["Unknown error"])[0] if isinstance(response, dict) else "Unknown error"
        raise SourceError(f"bls: API error: {msg}")

    retrieved = clock.now().replace(microsecond=0).isoformat()
    all_rows = []

    for series in response.get("Results", {}).get("series", []):
        series_id = series.get("seriesID", "")
        for item in series.get("data", [])[:limit]:
            try:
                year = item.get("year", "")
                period = item.get("period", "")
                value = item.get("value", "")
                if not year or not period or value == "":
                    continue
                measurement = _decimal(str(value))
                if measurement is None:
                    continue
                period_str = f"{year}-{period}"
                if period.startswith("M"):
                    period_str = f"{year}-{period[1:]:0>2}"
                description = f"BLS: {series_id} = {value} in {period_str}"
                evidence = json.dumps({
                    "series_id": series_id,
                    "year": year,
                    "period": period,
                    "value": str(value),
                    "footnotes": item.get("footnotes", []),
                }, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
                all_rows.append({
                    "source": SOURCE,
                    "external_id": f"bls-{series_id}-{period_str}",
                    "kind": "labor_metric",
                    "description": description[:4096],
                    "actor": f"bls:series:{series_id}",
                    "counterparty": "",
                    "instrument": "",
                    "action": "",
                    "reason": "",
                    "reason_basis": "unknown",
                    "amount": str(measurement),
                    "unit": "value",
                    "currency": "",
                    "basis": "observed",
                    "occurred_at": f"{period_str}-01" if len(period_str) == 7 else period_str,
                    "observed_at": retrieved,
                    "available_at": retrieved,
                    "source_url": url,
                    "evidence": evidence,
                })
            except (TypeError, ValueError, KeyError):
                continue

    stored = observations.store_observations(conn, all_rows)
    finished = clock.now()

    import hashlib
    canonical = json.dumps({
        "endpoint": "api.bls.gov/publicAPI/v2/timeseries/data",
        "series_ids": series_ids, "limit": limit,
        "start_year": start_year, "end_year": end_year
    }, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("utf-8")

    warnings = list(client.warnings)
    registry.set_payload_fingerprint(conn, client)
    registry.clear_failure_recorder(conn)
    registry.record_ingest_run(
        conn, SOURCE, started.isoformat(), finished.isoformat(),
        query=f"BLS series {','.join(series_ids)} {start_year}..{end_year}",
        fetched=len(all_rows), stored=stored["imported"], skipped=0,
        pages=1, truncated=False,
        request_sha256=hashlib.sha256(canonical).hexdigest(),
        warnings=warnings,
        coverage=f"BLS labor data {start_year}..{end_year}")
    return {
        "source": SOURCE,
        "endpoint": "api.bls.gov/publicAPI/v2/timeseries/data",
        "source_url": url,
        "retrieved_at": client.retrieved_at,
        "period": {"start": str(start_year), "end": str(end_year)},
        "records": len(all_rows),
        "observations": stored["imported"],
        "skipped": 0,
        "by_kind": stored["kinds"],
        "truncated": False,
        "warnings": warnings,
        "limitations": [
            "BLS provides labor statistics (employment, wages, JOLTS, etc.) for US.",
            "Series IDs required; common ones: LNS14000000 (unemployment rate), JTS00000000JOL (job openings).",
            "Free API with registration key for higher limits (500 requests/day vs 25 without).",
            "Data is monthly/quarterly/annual depending on series.",
            "occurred_at is the period date; available_at is retrieval time.",
            "This client uses POST as required by BLS API.",
        ],
    }
