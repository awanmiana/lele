"""UN Comtrade trade data fetcher.

Fetches international trade data from UN Comtrade API (free public API).
Maps to measurement-optional `trade_flow` observations with country/HS code detail.
"""
import json
import re
from ..core import clock
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from ..analysis import observations
from ..core import registry
from .http import HTTPClient, SourceError

MAX_LIMIT_DEFAULT = 100
MAX_LIMIT = 1000
SOURCE = "un-comtrade"
BASE_URL = "https://comtradeapi.un.org/public/v1/preview/reporter"
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
HS_CODE_RE = re.compile(r"^[0-9]{2,10}$")


def _iso_date(value, label):
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise ValueError(f"{label} must be an ISO date YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a valid ISO date") from exc


def _validate_hs_code(code):
    if not isinstance(code, str) or not HS_CODE_RE.fullmatch(code):
        raise ValueError("HS code must be 2-10 digits")
    return code


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
    period = record.get("period", "")
    if not isinstance(period, str) or not period:
        return None
    reporter = record.get("reporterCode", "")
    partner = record.get("partnerCode", "")
    hs_code = record.get("cmdCode", "")
    flow = record.get("flowCode", "")
    value = record.get("primaryValue")
    if value is None:
        value = record.get("qty")
    if value is None:
        return None
    try:
        val_str = str(value)
    except (TypeError, ValueError):
        return None
    if not val_str.strip():
        return None
    measurement = _decimal(val_str)
    if measurement is None:
        return None
    flow_desc = {"M": "import", "X": "export", "RX": "re-export", "RM": "re-import"}.get(str(flow), f"flow_{flow}")
    description = f"UN Comtrade: {flow_desc} of HS {hs_code} from {partner} to {reporter} in {period}"
    actor_key = f"un:reporter:{reporter}"
    counterparty_key = f"un:partner:{partner}"
    evidence = json.dumps({
        "period": period,
        "reporter_code": reporter,
        "partner_code": partner,
        "hs_code": hs_code,
        "flow_code": flow,
        "flow_desc": flow_desc,
        "value": val_str,
        "unit": record.get("unitCode", ""),
        "customs_code": record.get("customsCode", ""),
        "motivation": record.get("motivationCode", ""),
    }, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    source_url = f"{BASE_URL}/{reporter}?period={period}&partner={partner}&cmd={hs_code}"
    return {
        "source": SOURCE,
        "external_id": f"comtrade-{reporter}-{partner}-{hs_code}-{period}-{flow}",
        "kind": "trade_flow",
        "description": description[:4096],
        "actor": actor_key,
        "counterparty": counterparty_key,
        "instrument": "",
        "action": "",
        "reason": "",
        "reason_basis": "unknown",
        "amount": str(measurement),
        "unit": record.get("unitCode", "USD"),
        "currency": "USD",
        "basis": "observed",
        "occurred_at": f"{period}-01",
        "observed_at": retrieved_at,
        "available_at": retrieved_at,
        "source_url": source_url,
        "evidence": evidence,
    }


def fetch_comtrade(conn, reporter_code="842", partner_code="0", hs_code="", limit=MAX_LIMIT_DEFAULT,
                   start=None, end=None, freq="M"):
    """Fetch UN Comtrade trade data.

    Args:
        conn: SQLite connection
        reporter_code: Reporter country code (default 842=USA)
        partner_code: Partner country code (default 0=World)
        hs_code: HS commodity code (empty for all)
        limit: Maximum records to fetch
        start: Start period YYYY-MM
        end: End period YYYY-MM
        freq: Frequency (A=annual, M=monthly)
    """
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be an integer from 1 to {MAX_LIMIT}")
    if not str(reporter_code).isdigit():
        raise ValueError("reporter_code must be numeric")
    if not str(partner_code).isdigit():
        raise ValueError("partner_code must be numeric")
    if hs_code:
        _validate_hs_code(hs_code)

    today = clock.now().date()
    end_date = _iso_date(end + "-01", "end") if end else today.replace(day=1)
    start_date = (_iso_date(start + "-01", "start") if start
                  else end_date.replace(year=end_date.year - 1))
    if start_date > end_date:
        raise ValueError("start must not be after end")

    started = clock.now()
    client = HTTPClient(ttl=0)

    params = {
        "reporterCode": reporter_code,
        "partnerCode": partner_code,
        "freq": freq,
        "format": "json",
        "max": limit,
    }
    if hs_code:
        params["cmdCode"] = hs_code

    # Build period range
    periods = []
    curr = start_date
    while curr <= end_date:
        periods.append(curr.strftime("%Y%m"))
        if curr.month == 12:
            curr = curr.replace(year=curr.year + 1, month=1)
        else:
            curr = curr.replace(month=curr.month + 1)

    all_results = []
    for period in periods:
        params["period"] = period
        url = f"{BASE_URL}/{reporter_code}?{urlencode(params)}"
        try:
            payload = client.get_json(url)
        except (SourceError, ValueError, TypeError) as exc:
            raise SourceError(f"un-comtrade: fetch failed for period {period}; "
                              f"no writes applied. {exc}") from exc

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            continue
        all_results.extend(data)
        if len(all_results) >= limit:
            all_results = all_results[:limit]
            break

    retrieved = clock.now().replace(microsecond=0).isoformat()
    rows, skipped = [], 0
    for record in all_results:
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

    canonical = json.dumps({
        "endpoint": "comtradeapi.un.org/public/v1/preview/reporter",
        "filters": params, "limit": limit, "periods": periods
    }, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("utf-8")

    warnings = list(client.warnings)
    truncated = len(all_results) >= limit
    if truncated:
        warnings.append("result hit limit; older periods may be absent")

    import hashlib
    registry.record_ingest_run(
        conn, SOURCE, started.isoformat(), finished.isoformat(),
        query=f"{reporter_code}->{partner_code} HS:{hs_code or 'all'} {start_date}..{end_date}",
        fetched=len(all_results), stored=stored["imported"], skipped=skipped,
        pages=len(periods), truncated=truncated,
        request_sha256=hashlib.sha256(canonical).hexdigest(),
        warnings=warnings,
        coverage=f"UN Comtrade trade flows {start_date}..{end_date}")
    return {
        "source": SOURCE,
        "endpoint": "comtradeapi.un.org/public/v1/preview/reporter",
        "source_url": f"{BASE_URL}/{reporter_code}",
        "retrieved_at": client.retrieved_at,
        "period": {"start": start_date.isoformat(), "end": end_date.isoformat()},
        "records": len(all_results),
        "observations": stored["imported"],
        "skipped": skipped,
        "by_kind": stored["kinds"],
        "truncated": truncated,
        "warnings": warnings,
        "limitations": [
            "UN Comtrade provides monthly/annual trade data by HS code and partner country.",
            "Free tier has rate limits; this client respects per-host pacing.",
            "Data is reported by customs authorities; revisions may occur.",
            "Only one reporter country per call; use multiple calls for other countries.",
            "HS code filtering supported; empty means all commodities.",
            "Amounts are in USD (primaryValue) or quantity (qty) depending on availability.",
            "occurred_at is set to first day of period; available_at is retrieval time.",
        ],
    }


def fetch_census_trade(conn, limit=MAX_LIMIT_DEFAULT, start=None, end=None,
                       commodity="", country=""):
    """Fetch US Census international trade data.

    Args:
        conn: SQLite connection
        limit: Maximum records
        start: Start date YYYY-MM
        end: End date YYYY-MM
        commodity: NAICS or HS commodity code
        country: Country code (ISO)
    """
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be an integer from 1 to {MAX_LIMIT}")

    today = clock.now().date()
    end_date = _iso_date(end + "-01", "end") if end else today.replace(day=1)
    start_date = (_iso_date(start + "-01", "start") if start
                  else end_date.replace(year=end_date.year - 1))
    if start_date > end_date:
        raise ValueError("start must not be after end")

    started = clock.now()
    client = HTTPClient(ttl=0)

    # US Census trade API - use imports/exports endpoints
    base = "https://api.census.gov/data/timeseries/intltrade"
    get_params = ["I_COMMODITY", "I_COUNTRY", "I_VALUE", "I_QTY", "TIME"]
    time_from = start_date.strftime("%Y-%m")
    time_to = end_date.strftime("%Y-%m")

    params = {
        "get": ",".join(get_params),
        "time": f"from {time_from} to {time_to}",
    }
    if commodity:
        params["I_COMMODITY"] = commodity
    if country:
        params["I_COUNTRY"] = country

    url = f"{base}/imports?{urlencode(params)}"
    try:
        payload = client.get_json(url)
    except (SourceError, ValueError, TypeError) as exc:
        raise SourceError(f"us-census-trade: imports fetch failed; "
                          f"no writes applied. {exc}") from exc

    if not isinstance(payload, list) or len(payload) < 2:
        raise SourceError("us-census-trade: unexpected imports response")

    header, *rows = payload
    col_idx = {name: i for i, name in enumerate(header)}

    retrieved = clock.now().replace(microsecond=0).isoformat()
    observations_list = []

    for row in rows[:limit]:
        try:
            period = row[col_idx["TIME"]]
            commodity_code = row[col_idx["I_COMMODITY"]]
            country_code = row[col_idx["I_COUNTRY"]]
            value = row[col_idx["I_VALUE"]]
            qty = row[col_idx.get("I_QTY", 0)]
        except (IndexError, KeyError):
            continue

        measurement = _decimal(str(value))
        if measurement is None:
            continue

        description = f"US Census: import of {commodity_code} from {country_code} in {period}"
        evidence = json.dumps({
            "period": period,
            "commodity": commodity_code,
            "country": country_code,
            "value": str(value),
            "quantity": str(qty),
            "direction": "import",
        }, sort_keys=True, ensure_ascii=True, separators=(",", ":"))

        observations_list.append({
            "source": SOURCE,
            "external_id": f"census-import-{commodity_code}-{country_code}-{period}",
            "kind": "trade_flow",
            "description": description[:4096],
            "actor": "us-census:importer",
            "counterparty": f"us-census:partner:{country_code}",
            "instrument": "",
            "action": "",
            "reason": "",
            "reason_basis": "unknown",
            "amount": str(measurement),
            "unit": "USD",
            "currency": "USD",
            "basis": "observed",
            "occurred_at": f"{period}-01",
            "observed_at": retrieved,
            "available_at": retrieved,
            "source_url": url,
            "evidence": evidence,
        })

    # Also fetch exports
    url = f"{base}/exports?{urlencode(params)}"
    try:
        payload = client.get_json(url)
    except (SourceError, ValueError, TypeError) as exc:
        raise SourceError(f"us-census-trade: exports fetch failed; "
                          f"no writes applied. {exc}") from exc

    if isinstance(payload, list) and len(payload) >= 2:
        _, *exp_rows = payload
        for row in exp_rows[:limit]:
            try:
                period = row[col_idx["TIME"]]
                commodity_code = row[col_idx["I_COMMODITY"]]
                country_code = row[col_idx["I_COUNTRY"]]
                value = row[col_idx["I_VALUE"]]
            except (IndexError, KeyError):
                continue

            measurement = _decimal(str(value))
            if measurement is None:
                continue

            description = f"US Census: export of {commodity_code} to {country_code} in {period}"
            evidence = json.dumps({
                "period": period,
                "commodity": commodity_code,
                "country": country_code,
                "value": str(value),
                "direction": "export",
            }, sort_keys=True, ensure_ascii=True, separators=(",", ":"))

            observations_list.append({
                "source": SOURCE,
                "external_id": f"census-export-{commodity_code}-{country_code}-{period}",
                "kind": "trade_flow",
                "description": description[:4096],
                "actor": "us-census:exporter",
                "counterparty": f"us-census:partner:{country_code}",
                "instrument": "",
                "action": "",
                "reason": "",
                "reason_basis": "unknown",
                "amount": str(measurement),
                "unit": "USD",
                "currency": "USD",
                "basis": "observed",
                "occurred_at": f"{period}-01",
                "observed_at": retrieved,
                "available_at": retrieved,
                "source_url": url,
                "evidence": evidence,
            })

    stored = observations.store_observations(conn, observations_list)
    finished = clock.now()

    import hashlib
    canonical = json.dumps({
        "endpoint": "api.census.gov/data/timeseries/intltrade",
        "filters": params, "limit": limit
    }, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("utf-8")

    warnings = list(client.warnings)
    registry.record_ingest_run(
        conn, SOURCE, started.isoformat(), finished.isoformat(),
        query=f"census trade {start_date}..{end_date} commodity:{commodity} country:{country}",
        fetched=len(observations_list), stored=stored["imported"], skipped=0,
        pages=2, truncated=False,
        request_sha256=hashlib.sha256(canonical).hexdigest(),
        warnings=warnings,
        coverage=f"US Census trade flows {start_date}..{end_date}")
    return {
        "source": SOURCE,
        "endpoint": "api.census.gov/data/timeseries/intltrade",
        "source_url": base,
        "retrieved_at": client.retrieved_at,
        "period": {"start": start_date.isoformat(), "end": end_date.isoformat()},
        "records": len(observations_list),
        "observations": stored["imported"],
        "skipped": 0,
        "by_kind": stored["kinds"],
        "truncated": False,
        "warnings": warnings,
        "limitations": [
            "US Census provides monthly US import/export trade data by commodity and country.",
            "Data covers imports and exports separately; both are fetched.",
            "Commodity codes use NAICS or HS classification depending on dataset.",
            "Amounts are in USD; quantities in various units.",
            "occurred_at is first day of month; available_at is retrieval time.",
            "Free public API; rate limits apply.",
        ],
    }
