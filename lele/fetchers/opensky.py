"""OpenSky Network flight data fetcher.

Fetches flight data from OpenSky Network API (free public API, optional
registration). Maps to measurement-optional `flight_event` observations with
flight detail.
"""
import base64
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from http.client import HTTPException  # noqa: F401 - re-exported for the request path
from urllib.error import URLError  # noqa: F401 - re-exported for the request path
from urllib.parse import urlencode
from urllib.request import Request  # noqa: F401 - re-exported for the request path

from ..analysis import observations
from ..core import clock, registry
from .http import HTTPClient, SourceError

MAX_LIMIT_DEFAULT = 100
MAX_LIMIT = 10000
SOURCE = "opensky"
BASE_URL = "https://opensky-network.org/api"
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _iso_date(value, label):
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise ValueError(f"{label} must be an ISO date YYYY-MM-DD")
    try:
        return datetime.fromisoformat(value).date()
    except ValueError as exc:
        raise ValueError(f"{label} must be a valid ISO date") from exc


def _observation(state, retrieved_at):
    if not isinstance(state, list) or len(state) < 17:
        return None
    icao24 = state[0] or ""
    callsign = (state[1] or "").strip()
    origin_country = state[2] or ""
    time_position = state[3]
    last_contact = state[4]
    longitude = state[5]
    latitude = state[6]
    baro_altitude = state[7]
    on_ground = state[8]
    velocity = state[9]
    true_track = state[10]
    vertical_rate = state[11]
    sensors = state[12]
    geo_altitude = state[13]
    squawk = state[14]
    spi = state[15]
    position_source = state[16]
    if not icao24:
        return None
    evidence = json.dumps({
        "icao24": icao24,
        "callsign": callsign,
        "origin_country": origin_country,
        "time_position": time_position,
        "last_contact": last_contact,
        "longitude": longitude,
        "latitude": latitude,
        "baro_altitude": baro_altitude,
        "on_ground": on_ground,
        "velocity": velocity,
        "true_track": true_track,
        "vertical_rate": vertical_rate,
        "sensors": sensors,
        "geo_altitude": geo_altitude,
        "squawk": squawk,
        "spi": spi,
        "position_source": position_source,
    }, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    ts = time_position or last_contact or 0
    occurred_at = datetime.fromtimestamp(ts, tz=UTC).isoformat() if ts else retrieved_at
    return {
        "source": SOURCE,
        "external_id": f"opensky-{icao24}-{ts}",
        "kind": "flight_event",
        "description": f"OpenSky: flight {callsign or icao24} from {origin_country} at {occurred_at}",
        "actor": f"opensky:aircraft:{icao24}",
        "counterparty": "",
        "instrument": "",
        "action": "",
        "reason": "",
        "reason_basis": "unknown",
        "amount": None,
        "unit": "",
        "currency": "",
        "basis": "observed",
        "occurred_at": occurred_at,
        "observed_at": retrieved_at,
        "available_at": retrieved_at,
        "source_url": BASE_URL,
        "evidence": evidence,
    }


def fetch_opensky_states(conn, limit=MAX_LIMIT_DEFAULT, time=None, icao24="", bbox="",
                         username=None, password=None):
    """Fetch OpenSky Network flight states.

    Args:
        conn: SQLite connection
        limit: Maximum states to store
        time: Unix timestamp for historical query (optional)
        icao24: Filter by aircraft ICAO24 (optional)
        bbox: Bounding box "lamin,lomin,lamax,lomax" (optional)
        username: OpenSky username (optional, for higher limits)
        password: OpenSky password (optional)
    """
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be an integer from 1 to {MAX_LIMIT}")
    started = clock.now()
    registry.record_failures(conn, source=SOURCE, started=started, query=f"OpenSky states time:{time or 'now'} icao24:{icao24 or 'all'}")
    client = HTTPClient(ttl=0)
    params = {}
    if time:
        params["time"] = str(int(time))
    if icao24:
        params["icao24"] = icao24
    if bbox:
        params["bbox"] = bbox
    url = f"{BASE_URL}/states/all?{urlencode(params)}"
    headers = {}
    if username and password:
        auth = base64.b64encode(f"{username}:{password}".encode()).decode()
        headers["Authorization"] = f"Basic {auth}"
    try:
        payload = _fetch_with_auth(client, url, headers)
    except (SourceError, ValueError, TypeError) as exc:
        raise SourceError(f"opensky: fetch failed; no writes applied. {exc}") from exc
    if not isinstance(payload, dict):
        raise SourceError("opensky: unexpected response format")
    retrieved = clock.now().replace(microsecond=0).isoformat()
    states = payload.get("states", [])
    if not isinstance(states, list):
        states = []
    rows = []
    for state in states[:limit]:
        observation = _observation(state, retrieved)
        if observation is None:
            continue
        rows.append(observation)
    stored = observations.store_observations(conn, rows)
    finished = clock.now()
    canonical = json.dumps({
        "endpoint": "opensky-network.org/api/states/all",
        "params": params, "limit": limit
    }, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    warnings = list(client.warnings)
    registry.set_payload_fingerprint(conn, client)
    registry.clear_failure_recorder(conn)
    registry.record_ingest_run(
        conn, SOURCE, started.isoformat(), finished.isoformat(),
        query=f"OpenSky states time:{time or 'now'} icao24:{icao24 or 'all'}",
        fetched=len(states), stored=stored["imported"], skipped=len(states) - stored["imported"],
        pages=1, truncated=len(states) >= limit,
        request_sha256=hashlib.sha256(canonical).hexdigest(),
        warnings=warnings,
        coverage=f"OpenSky flight states {retrieved}")
    return {
        "source": SOURCE,
        "endpoint": "opensky-network.org/api/states/all",
        "source_url": BASE_URL,
        "retrieved_at": client.retrieved_at,
        "period": {"start": retrieved, "end": retrieved},
        "records": len(states),
        "observations": stored["imported"],
        "skipped": len(states) - stored["imported"],
        "by_kind": stored["kinds"],
        "truncated": len(states) >= limit,
        "warnings": warnings,
        "limitations": [
            "OpenSky Network provides real-time and historical ADS-B flight tracking data.",
            "Free anonymous access: 10 requests/minute, limited historical data.",
            "Registered users get higher rate limits and full historical access.",
            "Data includes position, altitude, velocity, and identification for aircraft.",
            "occurred_at is the position timestamp; available_at is retrieval time.",
            "No API key required for basic access; username/password for registered users.",
        ],
    }


def fetch_opensky_flights(conn, limit=MAX_LIMIT_DEFAULT, begin=None, end=None,
                          username=None, password=None):
    """Fetch OpenSky Network flights by interval.

    Args:
        conn: SQLite connection
        limit: Maximum flights to store
        begin: Start datetime ISO or Unix timestamp
        end: End datetime ISO or Unix timestamp
        username: OpenSky username (required for flights endpoint)
        password: OpenSky password (required for flights endpoint)
    """
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be an integer from 1 to {MAX_LIMIT}")
    if not username or not password:
        raise ValueError("username and password required for flights endpoint")
    started = clock.now()
    client = HTTPClient(ttl=0)
    if begin and isinstance(begin, str):
        try:
            begin_dt = datetime.fromisoformat(begin.replace("Z", "+00:00"))
            begin_ts = int(begin_dt.timestamp())
        except ValueError:
            begin_ts = int(begin)
    else:
        begin_ts = int((clock.now() - timedelta(days=1)).timestamp())
    if end and isinstance(end, str):
        try:
            end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
            end_ts = int(end_dt.timestamp())
        except ValueError:
            end_ts = int(end)
    else:
        end_ts = int(clock.now().timestamp())
    # Registered once the window is resolved, which is before the request is built.
    registry.record_failures(conn, source=SOURCE, started=started,
                             query=f"OpenSky flights {begin_ts}..{end_ts}")
    params = {"begin": str(begin_ts), "end": str(end_ts)}
    url = f"{BASE_URL}/flights/all?{urlencode(params)}"
    auth = base64.b64encode(f"{username}:{password}".encode()).decode()
    headers = {"Authorization": f"Basic {auth}"}
    try:
        payload = _fetch_with_auth(client, url, headers)
    except (SourceError, ValueError, TypeError) as exc:
        raise SourceError(f"opensky: flights fetch failed; no writes applied. {exc}") from exc
    if not isinstance(payload, list):
        raise SourceError("opensky: unexpected flights response format")
    retrieved = clock.now().replace(microsecond=0).isoformat()
    rows = []
    for flight in payload[:limit]:
        try:
            icao24 = flight.get("icao24", "")
            callsign = (flight.get("callsign") or "").strip()
            first_seen = flight.get("firstSeen", 0)
            last_seen = flight.get("lastSeen", 0)
            est_departure_airport = flight.get("estDepartureAirport", "")
            est_arrival_airport = flight.get("estArrivalAirport", "")
            if not icao24:
                continue
            evidence = json.dumps({
                "icao24": icao24,
                "callsign": callsign,
                "first_seen": first_seen,
                "last_seen": last_seen,
                "est_departure_airport": est_departure_airport,
                "est_arrival_airport": est_arrival_airport,
            }, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
            occurred_at = datetime.fromtimestamp(first_seen, tz=UTC).isoformat() if first_seen \
                else retrieved
            rows.append({
                "source": SOURCE,
                "external_id": f"opensky-flight-{icao24}-{first_seen}",
                "kind": "flight_event",
                "description": f"OpenSky flight: {callsign or icao24} {est_departure_airport}->{est_arrival_airport}",
                "actor": f"opensky:aircraft:{icao24}",
                "counterparty": "",
                "instrument": "",
                "action": "",
                "reason": "",
                "reason_basis": "unknown",
                "amount": None,
                "unit": "",
                "currency": "",
                "basis": "observed",
                "occurred_at": occurred_at,
                "observed_at": retrieved,
                "available_at": retrieved,
                "source_url": BASE_URL,
                "evidence": evidence,
            })
        except (TypeError, ValueError, KeyError):
            continue
    stored = observations.store_observations(conn, rows)
    finished = clock.now()
    canonical = json.dumps({
        "endpoint": "opensky-network.org/api/flights/all",
        "params": params, "limit": limit
    }, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    warnings = list(client.warnings)
    registry.set_payload_fingerprint(conn, client)
    registry.clear_failure_recorder(conn)
    registry.record_ingest_run(
        conn, SOURCE, started.isoformat(), finished.isoformat(),
        query=f"OpenSky flights {begin_ts}..{end_ts}",
        fetched=len(payload), stored=stored["imported"], skipped=len(payload) - stored["imported"],
        pages=1, truncated=len(payload) >= limit,
        request_sha256=hashlib.sha256(canonical).hexdigest(),
        warnings=warnings,
        coverage=f"OpenSky flights {begin_ts}..{end_ts}")
    return {
        "source": SOURCE,
        "endpoint": "opensky-network.org/api/flights/all",
        "source_url": BASE_URL,
        "retrieved_at": client.retrieved_at,
        "period": {"start": str(begin_ts), "end": str(end_ts)},
        "records": len(payload),
        "observations": stored["imported"],
        "skipped": len(payload) - stored["imported"],
        "by_kind": stored["kinds"],
        "truncated": len(payload) >= limit,
        "warnings": warnings,
        "limitations": [
            "OpenSky flights endpoint requires registration (username/password).",
            "Returns flight summaries with departure/arrival airports and times.",
            "Data covers flights within the specified time interval.",
            "occurred_at is flight first seen time; available_at is retrieval time.",
        ],
    }


def _fetch_with_auth(client, url, headers):
    """Fetch JSON with per-request headers, through the shared client.

    OpenSky's endpoints take HTTP basic auth, which the client's fixed header
    set cannot carry. This used to reach into the client's private pacing state
    and copy its whole read loop, which bypassed the retry schedule, the
    decompression and the deadline, and broke outright whenever those internals
    changed. Every request now goes through one path.
    """
    return client.request_json(url, headers=headers, cache=False)
