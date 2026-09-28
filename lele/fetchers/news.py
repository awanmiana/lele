import hashlib
import json
import os
import re
import tempfile
from ..core import clock
from datetime import datetime, timedelta, UTC
from urllib.parse import urlencode

from ..analysis import events, projection
from ..core import importer, registry
from ..core.constants import EVIDENCE_ENDPOINTS
from . import prices
from .http import HTTPClient

MAX_BYTES = 2 * 1024 * 1024
MAX_ARTICLES = 250
MAX_HOURS = 24 * 90
PLATFORM = "gdelt"
TOPIC_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _\-]{0,63}")
LANGUAGES = ("English",)


def _now():
    return clock.now()


def request_news_window(hours, end, now):
    if type(hours) is not int or not 1 <= hours <= MAX_HOURS:
        raise ValueError(f"news hours must be an integer from 1 to {MAX_HOURS}")
    finish = now.astimezone(UTC) if end is None else projection._timestamp(end)
    if finish > now:
        raise ValueError("news end must not be in the future")
    return finish - timedelta(hours=hours), finish


def _seendate(text):
    importer._text(text, "GDELT seendate", 32, True)
    try:
        return datetime.strptime(text, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ValueError("GDELT seendate must be YYYYMMDDTHHMMSSZ") from exc


def _observations(payload, entity_key, start, finish):
    if not isinstance(payload, dict):
        raise ValueError("GDELT response requires an object")
    articles = payload.get("articles", [])
    if not isinstance(articles, list):
        raise ValueError("GDELT articles must be an array")
    observations: list[dict] = []
    skipped = {"non_https": 0, "invalid": 0, "outside_window": 0, "duplicate": 0}
    seen: set[str] = set()
    for article in articles:
        if not isinstance(article, dict):
            skipped["invalid"] += 1
            continue
        try:
            url = importer._provenance(article.get("url"), "article url")
        except (ValueError, TypeError):
            skipped["non_https"] += 1
            continue
        if not url.startswith("https://"):
            skipped["non_https"] += 1
            continue
        try:
            observed = _seendate(article.get("seendate"))
        except (ValueError, TypeError):
            skipped["invalid"] += 1
            continue
        if not start <= observed <= finish:
            skipped["outside_window"] += 1
            continue
        identifier = "gdelt:news_event:" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        if identifier in seen:
            skipped["duplicate"] += 1
            continue
        try:
            title = importer._text(article.get("title"), "article title", 1024, True)
            domain = article.get("domain")
            if domain is not None:
                importer._text(domain, "article domain", 256, True)
            country = article.get("sourcecountry")
            if country is not None:
                importer._text(country, "article source country", 256, True)
        except ValueError:
            skipped["invalid"] += 1
            continue
        seen.add(identifier)
        observations.append({
            "id": identifier, "entity_key": entity_key, "observed_at": observed.isoformat(),
            "available_at": observed.isoformat(), "kind": "news_event", "source_url": url,
            "description": title, "platform": domain, "industry": None, "location": country,
            "measurement": None, "mapping": None,
        })
    return observations, skipped


def _validate(observations, entity_key):
    if len(observations) > MAX_ARTICLES:
        raise ValueError(f"news exceeds the {MAX_ARTICLES}-article limit")
    raw = json.dumps({"observations": observations}, ensure_ascii=True, allow_nan=False,
                     separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise ValueError("news response exceeds 2 MiB")
    descriptor, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
        events._evidence(path, entity_key)
    finally:
        os.unlink(path)
    return raw


def fetch_news(conn, entity_id, topic, hours=24, limit=100, end=None, language=None,
               cache_seconds=0):
    if not isinstance(topic, str) or not TOPIC_RE.fullmatch(topic):
        raise ValueError("news topic must be 1..64 letters, digits, spaces, hyphens or underscores")
    if language is not None and language not in LANGUAGES:
        raise ValueError(f"news language must be one of {', '.join(LANGUAGES)}")
    if cache_seconds < 0 or not isinstance(cache_seconds, (int, float)):
        raise ValueError("cache seconds must be a nonnegative number")
    if type(limit) is not int or not 1 <= limit <= MAX_ARTICLES:
        raise ValueError(f"news limit must be an integer from 1 to {MAX_ARTICLES}")
    if type(entity_id) is not int or not 1 <= entity_id <= 2**63 - 1:
        raise ValueError("entity id must be a positive SQLite integer")
    entity = registry.get_entity(conn, entity_id)
    if entity is None:
        raise ValueError(f"entity {entity_id} not found")
    importer._text(entity["key"], "entity key", 512, True)
    started = _now()
    start, finish = request_news_window(hours, end, started)
    url = EVIDENCE_ENDPOINTS["GDELT"] + "?" + urlencode({
        "query": topic, "mode": "artlist", "maxrecords": limit,
        "startdatetime": start.strftime("%Y%m%d%H%M%S"),
        "enddatetime": finish.strftime("%Y%m%d%H%M%S"),
        "sort": "datedesc", "format": "json", **({"language": language} if language else {})})
    client = HTTPClient(ttl=cache_seconds, max_bytes=MAX_BYTES)
    if not cache_seconds:
        client.cache_dir = None
        client.ttl = 0
    payload = client.get_json(url)
    retrieved = prices._retrieved(client.retrieved_at)
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"),
                           allow_nan=False).encode("utf-8")
    if len(canonical) > MAX_BYTES:
        raise ValueError("decoded evidence response exceeds 2 MiB")
    entity_key = entity["key"]
    observations, skipped = _observations(payload, entity_key, start, finish)
    observations.sort(key=lambda row: (row["observed_at"], row["id"]))
    raw = _validate(observations, entity_key)
    report = {
        "source": "gdelt", "topic": topic, "entity_id": entity_id, "entity_key": entity_key,
        "language_filter": language or "none", "cache_ttl_seconds": cache_seconds,
        "source_url": url, "retrieved_at": retrieved.isoformat(),
        "request_started_at": started.isoformat(),
        "window": {"start_inclusive": start.isoformat(), "end_inclusive": finish.isoformat()},
        "cadence": "continuous news index (GDELT DOC 2.0 artlist); article see dates",
        "timestamp_semantics": "observed_at is the article see date; available_at is set equal to "
                               "it, which may be optimistic for indexing lag",
        "observations": len(observations), "by_kind": {"news_event": len(observations)},
        "skipped": skipped,
        "output_bytes": len(raw), "output_sha256": hashlib.sha256(raw).hexdigest(),
        "response_sha256": hashlib.sha256(canonical).hexdigest(),
        "response_hash_encoding": "parsed JSON, sorted keys, ASCII, compact separators; not wire bytes",
        "submitted": True, "truth_verified": False, "warnings": list(client.warnings),
        "limitations": [
            "Keyword relevance and deduplication are not verified; GDELT may return syndicated or "
            "near-duplicate coverage.",
            "observed_at and available_at are the article see date, so point-in-time use may be "
            "optimistic relative to indexing lag.",
            "Only the headline, source domain and country are recorded; no tone, body or event code "
            "is extracted.",
            "GDELT limits clients to about one request per five seconds; the client paces this host "
            "and treats its plain-text throttle response as retryable.",
            "GDELT is a public research index; its terms and any redistribution right are not "
            "asserted here. No accuracy or trading claim.",
            "One keyword phrase and one provider; empty results mean unknown coverage, not the "
            "absence of news.",
        ],
    }
    return {"observations": observations}, report
