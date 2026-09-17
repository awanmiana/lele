import hashlib
import json
import math
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ..core.constants import (
    CACHE_DIR,
    CACHE_TTL_SECONDS,
    HOST_RATE_LIMIT_SECONDS,
    HTTP_MAX_BYTES,
    HTTP_TIMEOUT_SECONDS,
    SOURCES,
    USER_AGENT,
)


class SourceError(Exception):
    pass


class _Retryable(SourceError):
    def __init__(self, message, delay):
        super().__init__(message)
        self.delay = delay


def _retry_after(value):
    try:
        delay = float(value)
    except (ValueError, TypeError):
        try:
            delay = parsedate_to_datetime(value).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return 0
    return min(30, max(0, delay)) if math.isfinite(delay) else 0


def validate_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        host = parts.hostname
        if host is None:
            raise ValueError
        allowed = any(
            parts.hostname == urlsplit(base).hostname
            and (parts.path == urlsplit(base).path
                 or parts.path.startswith(urlsplit(base).path + "/"))
            for base in SOURCES.values()
        )
        if (parts.scheme != "https" or not allowed or parts.username is not None
                or parts.password is not None or parts.port not in (None, 443)
                or parts.fragment or "\\" in url or any(ord(c) < 32 for c in url)
                or any(segment in (".", "..") for segment in unquote(parts.path).split("/"))):
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise SourceError("Only HTTPS URLs under the configured official APIs are allowed.") from exc
    return host


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fp.close()
        raise SourceError("API redirect refused; verify the official endpoint before retrying.")


_lock = threading.Lock()
_last_request: dict[str, float] = {}


def _decode(raw):
    def reject(value):
        raise ValueError("Non-finite JSON number")

    return json.loads(raw.decode("utf-8-sig"), parse_constant=reject)


class HTTPClient:
    def __init__(self, cache_dir=CACHE_DIR, ttl=CACHE_TTL_SECONDS,
                 timeout=HTTP_TIMEOUT_SECONDS, max_bytes=HTTP_MAX_BYTES,
                 rate=HOST_RATE_LIMIT_SECONDS):
        cache_dir = os.environ.get("FINWORLD_CACHE_DIR", cache_dir)
        try:
            ttl = float(os.environ.get("FINWORLD_CACHE_TTL", ttl))
            rate = float(os.environ.get("FINWORLD_RATE_LIMIT", rate))
        except (ValueError, TypeError) as exc:
            raise SourceError("FINWORLD_CACHE_TTL and FINWORLD_RATE_LIMIT must be nonnegative numbers.") from exc
        self.user_agent = os.environ.get("FINWORLD_USER_AGENT", USER_AGENT)
        if not self.user_agent.strip() or any(ord(c) < 32 or ord(c) > 126 for c in self.user_agent):
            raise SourceError("FINWORLD_USER_AGENT must contain printable ASCII text.")
        if (not all(isinstance(v, (int, float)) and math.isfinite(v)
                    for v in (ttl, timeout, rate))
                or ttl < 0 or timeout <= 0 or rate < 0
                or type(max_bytes) is not int or max_bytes <= 0):
            raise SourceError("HTTP timeout/byte limit must be positive; TTL/rate nonnegative.")
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.ttl = ttl
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.rate = rate
        self.warnings = []
        self.retrieved_at = ""
        self.opener = build_opener(_NoRedirect())

    def _warn(self, message):
        if message not in self.warnings:
            self.warnings.append(message)

    def _cache_path(self, url: str) -> Path:
        if self.cache_dir is None:
            raise ValueError("HTTP cache is disabled.")
        slot = int(hashlib.sha256(url.encode()).hexdigest(), 16) % 128
        return self.cache_dir / f"public-api-{slot:03d}.json"

    def _read_cache(self, url):
        if self.cache_dir is None or not self.ttl:
            return None
        try:
            path = self._cache_path(url)
            with path.open("rb") as handle:
                raw = handle.read(self.max_bytes + 4097)
            if len(raw) > self.max_bytes + 4096:
                raise ValueError("oversized cache")
            entry = _decode(raw)
            if not isinstance(entry, dict):
                raise ValueError("invalid cache")
            if entry.get("url") != url:
                return None
            age = time.time() - float(entry["time"])
            if not math.isfinite(age) or age < 0 or age >= self.ttl:
                return None
            payload = entry["payload"]
            if not isinstance(payload, (dict, list)):
                raise ValueError("invalid cached payload")
            self.retrieved_at = datetime.fromtimestamp(entry["time"], timezone.utc).isoformat()
            return payload
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
            self._warn("HTTP cache unreadable or invalid; fetched fresh data instead.")
            return None

    def _write_cache(self, url, payload, fetched_at):
        if self.cache_dir is None or not self.ttl:
            return
        temporary = None
        try:
            raw = json.dumps({"url": url, "time": fetched_at, "payload": payload},
                             ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
            if len(raw) > self.max_bytes + 4096:
                self._warn("HTTP cache entry too large; response was not cached.")
                return
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self.cache_dir, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(raw)
            os.replace(temporary, self._cache_path(url))
        except (OSError, ValueError, RecursionError):
            self._warn("HTTP cache write failed; data fetched but not cached. Check cache permissions.")
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    self._warn("HTTP cache temporary file cleanup failed; check cache permissions.")

    def get_json(self, url: str):
        host = validate_url(url)
        cached = self._read_cache(url)
        if cached is not None:
            return cached
        for attempt in range(3):
            try:
                payload = self._request(url, host)
                break
            except _Retryable as exc:
                if attempt == 2:
                    raise SourceError(str(exc)) from exc
                time.sleep(max(2 ** attempt, exc.delay))
        fetched_at = time.time()
        self.retrieved_at = datetime.fromtimestamp(fetched_at, timezone.utc).isoformat()
        self._write_cache(url, payload, fetched_at)
        return payload

    def _request(self, url, host):
        with _lock:
            delay = self.rate - (time.monotonic() - _last_request.get(host, -math.inf))
            if delay > 0:
                time.sleep(delay)
            _last_request[host] = time.monotonic()
        request = Request(url, headers={"User-Agent": self.user_agent,
                                       "Accept": "application/json",
                                       "Accept-Encoding": "identity"})
        try:
            deadline = time.monotonic() + self.timeout
            with self.opener.open(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise SourceError(f"{host}: unexpected HTTP {response.status}; retry later.")
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise SourceError(f"{host}: compressed response refused; request identity encoding.")
                length = response.headers.get("Content-Length")
                if length is not None:
                    try:
                        size = int(length)
                    except ValueError as exc:
                        raise SourceError(f"{host}: invalid Content-Length.") from exc
                    if size < 0 or size > self.max_bytes:
                        raise SourceError(f"{host}: response exceeds byte limit; lower the fetch limit.")
                chunks = []
                size = 0
                while True:
                    if time.monotonic() > deadline:
                        raise SourceError(f"{host}: response deadline exceeded; retry later.")
                    chunk = response.read1(min(65536, self.max_bytes + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise SourceError(f"{host}: response exceeds byte limit; lower the fetch limit.")
                payload = _decode(b"".join(chunks))
                if not isinstance(payload, (dict, list)):
                    raise ValueError("expected JSON object or array")
        except HTTPError as exc:
            try:
                if exc.code == 429 or 500 <= exc.code <= 599:
                    raise _Retryable(f"{host}: HTTP {exc.code}; retries exhausted, try later.",
                                     _retry_after(exc.headers.get("Retry-After"))) from exc
                raise SourceError(f"{host}: HTTP {exc.code}; verify parameters and API access policy.") from exc
            finally:
                exc.close()
        except (URLError, OSError, HTTPException) as exc:
            raise SourceError(f"{host}: network request failed; check connectivity and retry: {exc}") from exc
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise SourceError(f"{host}: invalid JSON response; retry or verify the official API status.") from exc
        return payload
