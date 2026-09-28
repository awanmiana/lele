"""A second public headline source, so reason coverage does not rest on one index.

The GDELT artlist endpoint throttles, caps articles per request and lags its own
live coverage. This module reads a public multi-publisher news feed instead, so a
window that GDELT could not serve can still be read.

Headlines are still headlines. A publisher's framing is its own, the feed's
timestamps are index timestamps, and nothing here turns coverage into a cause.
"""
import hashlib
import json
import os
import re
import tempfile
import xml.etree.ElementTree as ElementTree
from ..core import clock
from datetime import timedelta, UTC
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode

from ..analysis import events
from ..core import importer
from ..core.constants import SOURCES
from .http import HTTPClient

MAX_BYTES = 2 * 1024 * 1024
MAX_ITEMS = 250
MAX_HOURS = 24 * 7
PLATFORM = "google_news_rss"
MAX_ARTICLES = 250
DANGEROUS = re.compile(r"<!DOCTYPE|<!ENTITY", re.IGNORECASE)
TOPIC_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _\-]{0,63}")


def _now():
    return clock.now()


def request_window(hours, end, now):
    if type(hours) is not int or not 1 <= hours <= MAX_HOURS:
        raise ValueError(f"news feed hours must be an integer from 1 to {MAX_HOURS}")
    if end is None:
        finish = now.astimezone(UTC)
    else:
        from ..analysis import projection
        finish = projection._timestamp(end)
    if finish > now:
        raise ValueError("news feed end must not be in the future")
    return finish - timedelta(hours=hours), finish


def _published(text):
    importer._text(text, "news feed pubDate", 64, True)
    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError) as exc:
        raise ValueError("news feed pubDate is not a valid date") from exc
    if moment is None or moment.tzinfo is None:
        raise ValueError("news feed pubDate must carry a timezone")
    return moment.astimezone(UTC)


def _split_title(title, publisher):
    """Separate the trailing publisher suffix a multi-publisher feed appends."""
    suffix = f" - {publisher}"
    if publisher and title.endswith(suffix):
        return title[: -len(suffix)].strip(), True
    return title.strip(), False


def _parse(raw, start, finish, entity_key):
    if DANGEROUS.search(raw[:4096]):
        raise ValueError("news feed contains a document type declaration or entity")
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise ValueError("news feed is not well-formed XML") from exc
    items = root.iterfind(".//item")
    observations, skipped = [], {"invalid": 0, "outside_window": 0, "duplicate": 0,
                                "non_https": 0}
    seen: set[str] = set()
    for item in items:
        title_node = item.find("title")
        link_node = item.find("link")
        date_node = item.find("pubDate")
        source_node = item.find("source")
        if title_node is None or link_node is None or date_node is None:
            skipped["invalid"] += 1
            continue
        link = (link_node.text or "").strip()
        if not link.startswith("https://"):
            skipped["non_https"] += 1
            continue
        try:
            observed = _published(date_node.text or "")
        except ValueError:
            skipped["invalid"] += 1
            continue
        if not start <= observed <= finish:
            skipped["outside_window"] += 1
            continue
        publisher = (source_node.text or "").strip() if source_node is not None else ""
        publisher_url = source_node.get("url", "") if source_node is not None else ""
        try:
            importer._text(publisher, "news feed publisher", 256, False)
            if publisher_url:
                importer._provenance(publisher_url, "news feed publisher url")
        except (ValueError, TypeError):
            skipped["invalid"] += 1
            continue
        headline, trimmed = _split_title(title_node.text or "", publisher)
        if not headline:
            skipped["invalid"] += 1
            continue
        identifier = "google-news:" + hashlib.sha256(link.encode("utf-8")).hexdigest()[:16]
        if identifier in seen:
            skipped["duplicate"] += 1
            continue
        seen.add(identifier)
        observations.append({
            "id": identifier, "entity_key": entity_key,
            "observed_at": observed.isoformat(),
            "available_at": observed.isoformat(), "kind": "news_event",
            "source_url": link, "description": headline[:1024],
            "platform": publisher or None, "industry": None, "location": None,
            "measurement": None,
            "mapping": None,
        })
        if len(observations) > MAX_ARTICLES:
            break
    observations.sort(key=lambda row: (row["observed_at"], row["id"]))
    return observations, skipped


def _validate(observations, entity_key):
    if len(observations) > MAX_ARTICLES:
        raise ValueError(f"news feed exceeds the {MAX_ARTICLES}-article limit")
    raw = json.dumps({"observations": observations}, ensure_ascii=True, allow_nan=False,
                     separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise ValueError("news feed response exceeds 2 MiB")
    descriptor, path = tempfile.mkstemp(suffix=".json")
    try:
        with open(descriptor, "wb", closefd=True) as stream:
            stream.write(raw)
        events._evidence(path, entity_key)
    finally:
        os.unlink(path)
    return raw


def fetch_news_feed(conn, entity_id, topic, hours=24, limit=100, end=None, language=None,
                    cache_seconds=0):
    """Bounded multi-publisher headlines for one plain keyword phrase.

    `language` and `cache_seconds` exist so this is a drop-in alternative to the
    keyword-index reader inside the same signal detector. The feed serves the
    English market it was queried for, so `language` is recorded and ignored
    rather than silently filtering, and a nonzero cache interval is honoured.
    """
    """Bounded multi-publisher headlines for one plain keyword phrase."""
    if not isinstance(topic, str) or not TOPIC_RE.fullmatch(topic):
        raise ValueError("news feed topic must be 1..64 letters, digits, spaces, "
                         "hyphens or underscores")
    if language is not None and language != "English":
        raise ValueError("the news feed serves the English market it was queried for; "
                         "use no language filter")
    if cache_seconds < 0 or not isinstance(cache_seconds, (int, float)):
        raise ValueError("cache seconds must be a nonnegative number")
    if type(limit) is not int or not 1 <= limit <= MAX_ARTICLES:
        raise ValueError(f"news feed limit must be an integer from 1 to {MAX_ARTICLES}")
    if type(entity_id) is not int or not 1 <= entity_id <= 2**63 - 1:
        raise ValueError("entity id must be a positive SQLite integer")
    from ..core import registry
    entity = registry.get_entity(conn, entity_id)
    if entity is None:
        raise ValueError(f"entity {entity_id} not found")
    importer._text(entity["key"], "entity key", 512, True)
    started = _now()
    start, finish = request_window(hours, end, started)
    entity_key = entity["key"]
    url = SOURCES["GOOGLE_NEWS"] + "?" + urlencode({
        "q": topic, "hl": "en-US", "gl": "US", "ceid": "US:en"})
    client = HTTPClient(ttl=cache_seconds, max_bytes=MAX_BYTES)
    text = client.get_text(url, max_bytes=MAX_BYTES)
    observations, skipped = _parse(text, start, finish, entity_key)
    observations = observations[:limit]
    body = _validate(observations, entity_key)
    report = {
        "source": PLATFORM, "topic": topic, "entity_id": entity_id, "entity_key": entity_key,
        "language_filter": "English market as queried", "cache_ttl_seconds": cache_seconds,
        "source_url": url, "retrieved_at": started.replace(microsecond=0).isoformat(),
        "request_started_at": started.isoformat(),
        "window": {"start_inclusive": start.isoformat(), "end_inclusive": finish.isoformat()},
        "cadence": "continuous multi-publisher news feed",
        "timestamp_semantics": "observed_at is the feed publication timestamp indexed by the "
                               "provider; available_at is set equal to it, which may be "
                               "optimistic for indexing lag",
        "observations": len(observations), "by_kind": {"news_event": len(observations)},
        "skipped": skipped, "output_bytes": len(body),
        "output_sha256": hashlib.sha256(body).hexdigest(),
        "response_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "truth_verified": False, "warnings": list(client.warnings),
        "limitations": [
            "A headline is a publisher's framing of an event, not a verified fact and not a "
            "cause. The feed carries no article body, so nothing beyond the headline is read.",
            "Publication timestamps are the provider's index timestamps and may lag the event, "
            "so point-in-time use can be optimistic.",
            "Near-duplicate and syndicated coverage across publishers is expected and is not "
            "deduplicated beyond identical links.",
            "The feed serves a shorter window than the keyword index it complements, and can "
            "return fewer items than the requested limit on a quiet topic.",
            "The feed is a public aggregator; its terms and any redistribution right are not "
            "asserted here.",
            "No accuracy, calibration or trading claim is made.",
        ],
    }
    return {"observations": observations}, report
