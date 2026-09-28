"""The only outbound network path in lele.

Every provider call goes through `HTTPClient`, so this module is where the
guarantees live: a fixed allowlist of official HTTPS endpoints, no redirects
unless the target is itself allowlisted, a byte ceiling, a read deadline, a
per-host minimum interval, bounded retries with a growing backoff, and an
optional on-disk cache.

Three things were wrong here and are fixed rather than worked around.

* The cache file name was the URL's hash modulo 128, so unrelated requests
  overwrote each other's entries and most cache hits became misses. The name is
  now derived from the whole URL.
* Pacing held one process-wide lock while it slept, so a five-second wait for
  a slow provider stalled every other request in the process. Each host now has
  its own lock and its own timestamp.
* A non-JSON read stamped no retrieval time at all, so provenance recorded from
  a feed was the time of some earlier, unrelated request. Every response path
  stamps now.
"""
import hashlib
import json
import math
import os
import tempfile
import threading
import time
import zlib
from datetime import datetime, UTC
from email.utils import parsedate_to_datetime
from collections.abc import Callable
from typing import Any
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ..core.constants import (
    CACHE_DIR,
    CACHE_TTL_SECONDS,
    EVIDENCE_ENDPOINTS,
    HOST_MIN_RATES,
    HOST_RATE_LIMIT_SECONDS,
    HTTP_MAX_BYTES,
    HTTP_TIMEOUT_SECONDS,
    PRICE_ENDPOINTS,
    REDIRECT_ENDPOINTS,
    SANCTIONS_ENDPOINTS,
    SOURCES,
    USER_AGENT,
    env,
)

ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 1.0
CHUNK = 65536
DOWNLOAD_TIMEOUT_SECONDS = 120


class SourceError(Exception):
    pass


class _Retryable(SourceError):
    def __init__(self, message: str, delay: float = 0.0) -> None:
        super().__init__(message)
        self.delay = delay


def _retry_after(value: str | None) -> float:
    if not value:
        return 0
    try:
        delay = float(value)
    except (ValueError, TypeError):
        try:
            delay = parsedate_to_datetime(value).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return 0
    return min(30, max(0, delay)) if math.isfinite(delay) else 0


def _decompress(raw: bytes, encoding: str, host: str) -> bytes:
    """Undo a content encoding, refusing anything that is not one we can bound.

    `Accept-Encoding: identity` is requested, so this only fires when a provider
    ignores that. Decompressing a valid response is better than refusing it, and
    the output is capped, so a compression bomb cannot make this allocate
    without limit.
    """
    name = (encoding or "identity").strip().lower()
    if name in ("", "identity"):
        return raw
    try:
        if name == "gzip":
            stream = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif name == "deflate":
            stream = zlib.decompressobj()
        else:
            raise SourceError(f"{host}: unsupported content encoding {name!r}.")
        out = stream.decompress(raw, HTTP_MAX_BYTES + 1)
        if len(out) > HTTP_MAX_BYTES:
            raise SourceError(f"{host}: decompressed response exceeds the byte limit.")
        return out
    except SourceError:
        raise
    except (OSError, EOFError, ValueError, zlib.error) as exc:
        raise SourceError(f"{host}: could not decode a {name} response.") from exc


def validate_url(url: str) -> str:
    """Accept only an official HTTPS endpoint this build was configured with."""
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
        ) or any(
            parts.hostname == urlsplit(base).hostname and parts.path == urlsplit(base).path
            for base in PRICE_ENDPOINTS.values()
        ) or any(
            parts.hostname == urlsplit(base).hostname and parts.path == urlsplit(base).path
            for base in EVIDENCE_ENDPOINTS.values()
        ) or any(
            parts.hostname == urlsplit(base).hostname and parts.path == urlsplit(base).path
            for base in SANCTIONS_ENDPOINTS.values()
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
    def redirect_request(self, req: Request, fp: Any, code: int, msg: str,
                        headers: Any, newurl: str) -> None:
        # The body must be closed here: urllib hands ownership of it to this
        # hook, and a refused redirect that leaves it open leaks a socket.
        fp.close()
        raise SourceError("API redirect refused; verify the official endpoint before retrying.")


def validate_redirect_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        host = parts.hostname
        if host is None:
            raise ValueError
        allowed = any(
            parts.hostname == urlsplit(base).hostname
            and parts.path.startswith(urlsplit(base).path)
            for base in REDIRECT_ENDPOINTS.values()
        )
        if (parts.scheme != "https" or not allowed or parts.username is not None
                or parts.password is not None or parts.port not in (None, 443)
                or parts.fragment or "\\" in url or any(ord(c) < 32 for c in url)):
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise SourceError("Only allowlisted HTTPS redirect targets are accepted.") from exc
    return host


class _ValidatedRedirect(HTTPRedirectHandler):
    max_redirections = 3

    def redirect_request(self, req: Request, fp: Any, code: int, msg: str,
                        headers: Any, newurl: str) -> Request | None:
        try:
            validate_redirect_url(newurl)
        except SourceError:
            fp.close()
            raise
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_pace_lock = threading.Lock()
_pace_locks: dict[str, threading.Lock] = {}
_last_request: dict[str, float] = {}


def _host_lock(host: str) -> threading.Lock:
    with _pace_lock:
        lock = _pace_locks.get(host)
        if lock is None:
            lock = _pace_locks[host] = threading.Lock()
        return lock


def _pace(host: str, rate: float) -> None:
    """Wait out the per-host minimum interval, then record the request time."""
    with _host_lock(host):
        delay = max(rate, HOST_MIN_RATES.get(host, 0.0)) - (
            time.monotonic() - _last_request.get(host, -math.inf))
        if delay > 0:
            time.sleep(delay)
        _last_request[host] = time.monotonic()


def _decode(raw: bytes) -> Any:
    def reject(value: str) -> None:
        raise ValueError("Non-finite JSON number")

    return json.loads(raw.decode("utf-8-sig"), parse_constant=reject)


class HTTPClient:
    def __init__(self, cache_dir: str | Path | None = CACHE_DIR, ttl: float = CACHE_TTL_SECONDS,
                 timeout: float = HTTP_TIMEOUT_SECONDS, max_bytes: int = HTTP_MAX_BYTES,
                 rate: float = HOST_RATE_LIMIT_SECONDS) -> None:
        # Both the current LELE_ prefix and the previous FINWORLD_ one are read,
        # current first, so an existing configuration keeps working after the
        # rename without being edited.
        cache_dir = env("CACHE_DIR", cache_dir)
        try:
            ttl = float(env("CACHE_TTL", ttl))
            rate = float(env("RATE_LIMIT", rate))
        except (ValueError, TypeError) as exc:
            raise SourceError("LELE_CACHE_TTL and LELE_RATE_LIMIT (or the previous "
                              "FINWORLD_ names) must be nonnegative numbers.") from exc
        self.user_agent = env("USER_AGENT", USER_AGENT)
        if not self.user_agent.strip() or any(ord(c) < 32 or ord(c) > 126 for c in self.user_agent):
            raise SourceError("LELE_USER_AGENT must contain printable ASCII text.")
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
        self.warnings: list[str] = []
        self.retrieved_at = ""
        self.retrieved_epoch = 0.0
        self.opener = build_opener(_NoRedirect())

    def _warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def _stamp(self, when: float) -> None:
        self.retrieved_epoch = when
        self.retrieved_at = datetime.fromtimestamp(when, UTC).isoformat()

    def _cache_path(self, url: str) -> Path:
        if self.cache_dir is None:
            raise ValueError("HTTP cache is disabled.")
        """A filename derived from the whole URL.

        The name used to be the URL's hash modulo 128, so every request landing
        in the same slot wrote the same file and threw the other one's entry
        away. With a dozen endpoints and their page parameters that turned most
        cache hits into misses and made repeat runs re-download the same series.
        One directory per hash prefix keeps any single directory small without
        reintroducing a shared slot.
        """
        digest = hashlib.sha256(url.encode()).hexdigest()
        return self.cache_dir / digest[:2] / f"{digest}.json"

    def _read_cache(self, url: str) -> Any:
        if self.cache_dir is None or not self.ttl:
            return None
        try:
            with self._cache_path(url).open("rb") as handle:
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
            self._stamp(float(entry["time"]))
            return payload
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
            self._warn("HTTP cache unreadable or invalid; fetched fresh data instead.")
            return None

    def _write_cache(self, url: str, payload: Any, fetched_at: float) -> None:
        if self.cache_dir is None or not self.ttl:
            return
        temporary = None
        try:
            raw = json.dumps({"url": url, "time": fetched_at, "payload": payload},
                             ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
            if len(raw) > self.max_bytes + 4096:
                self._warn("HTTP cache entry too large; response was not cached.")
                return
            path = self._cache_path(url)
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(raw)
            os.replace(temporary, path)
        except (OSError, ValueError, RecursionError):
            self._warn("HTTP cache write failed; data fetched but not cached. "
                       "Check cache permissions.")
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    self._warn("HTTP cache temporary file cleanup failed; "
                               "check cache permissions.")

    def _with_retries(self, call: Callable[[], Any], host: str) -> Any:
        """Run one request under a bounded, growing retry schedule.

        The wait doubles and is never shorter than the host's own minimum
        interval, so a provider that answered "slow down" is answered "slowly
        down" rather than hammered three times in three seconds. Every attempt
        is paced, including the first, so a loop of calls cannot outrun the
        interval by keeping the timestamp in a place it never looks.
        """
        errors: list[str] = []
        for attempt in range(1, ATTEMPTS + 1):
            _pace(host, self.rate)
            try:
                return call()
            except _Retryable as exc:
                errors.append(f"attempt {attempt}: {exc}")
                if attempt >= ATTEMPTS:
                    break
                time.sleep(max(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), exc.delay,
                               HOST_MIN_RATES.get(host, 0.0) + 1))
        raise SourceError(f"{host}: request failed after {ATTEMPTS} attempts: "
                          f"{errors[-1] if errors else 'no attempt was made'}")

    def get_json(self, url: str) -> Any:
        host = validate_url(url)
        cached = self._read_cache(url)
        if cached is not None:
            return cached
        payload = self._with_retries(lambda: self._request(url, host), host)
        fetched_at = time.time()
        self._stamp(fetched_at)
        self._write_cache(url, payload, fetched_at)
        return payload

    def request_json(self, url: str, headers: dict | None = None, cache: bool = True) -> Any:
        """GET a JSON document with extra request headers.

        A provider that authenticates per request (OpenSky's basic auth) cannot
        be served by `get_json` with its fixed header set, and the previous
        answer to that was for the caller to reach into this module's private
        pacing state and copy the whole read loop. That bypassed the retry
        schedule, the decompression and the deadline, and it broke outright when
        the pacing internals changed. This keeps every request on one path.
        """
        host = validate_url(url)
        if not cache:
            result = self._with_retries(
                lambda: self._request(url, host, headers=headers), host)
            self._stamp(time.time())
            return result
        return self.get_json(url)

    def post_json(self, url: str, payload: Any) -> Any:
        host = validate_url(url)
        body = json.dumps(payload, ensure_ascii=True, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
        result = self._with_retries(lambda: self._request(url, host, "POST", body), host)
        self._stamp(time.time())
        return result

    def get_text(self, url: str, max_bytes: int | None = None) -> str:
        """Fetch a non-JSON document, sharing pacing, retries and the byte ceiling.

        A feed or document endpoint is not JSON, so this exists rather than
        making a caller reach into the opener and lose the host pacing. The
        retrieval time is stamped like every other response, so provenance
        recorded from this read is the time of this read.
        """
        host = validate_url(url)
        limit = self.max_bytes if max_bytes is None else max_bytes
        if type(limit) is not int or limit <= 0:
            raise SourceError("text response byte limit must be a positive integer.")
        headers = {"User-Agent": self.user_agent,
                   "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, "
                             "text/html;q=0.8, */*;q=0.1",
                   "Accept-Encoding": "identity"}

        def read() -> str:
            request = Request(url, headers=headers, method="GET")
            with self.opener.open(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise SourceError(f"{host}: unexpected HTTP {response.status}; retry later.")
                return self._read_body(response, host, limit, "text").decode("utf-8", "replace")

        text = self._with_retries(read, host)
        self._stamp(time.time())
        return text

    def download(self, url: str, target_path: str | Path, max_bytes: int) -> int:
        """Download a document to `target_path` atomically.

        A partial write is worse than no write for a sanctions list or a filing
        index, because the next run reads the truncated file as if it were the
        whole document. The bytes go to a temporary file in the target's own
        directory and are renamed into place only after the full read succeeds.
        """
        host = validate_url(url)
        if type(max_bytes) is not int or max_bytes <= 0:
            raise SourceError("download byte limit must be a positive integer.")
        target = Path(target_path)
        opener = build_opener(_ValidatedRedirect())
        headers = {"User-Agent": self.user_agent,
                   "Accept": "text/csv, application/octet-stream;q=0.9, */*;q=0.1",
                   "Accept-Encoding": "identity"}

        def read() -> int:
            request = Request(url, headers=headers, method="GET")
            with opener.open(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
                if response.status != 200:
                    raise SourceError(f"{host}: unexpected HTTP {response.status}; retry later.")
                length = response.headers.get("Content-Length")
                if length is not None:
                    try:
                        size = int(length)
                    except ValueError as exc:
                        raise SourceError(f"{host}: invalid Content-Length.") from exc
                    if size < 0 or size > max_bytes:
                        raise SourceError(f"{host}: response exceeds the download byte limit.")
                deadline = time.monotonic() + DOWNLOAD_TIMEOUT_SECONDS
                written = 0
                with tempfile.NamedTemporaryFile(dir=str(target.parent) or ".",
                                                 prefix=".lele-download-",
                                                 delete=False) as stream:
                    temporary = Path(stream.name)
                    try:
                        while True:
                            if time.monotonic() > deadline:
                                raise _Retryable(f"{host}: response deadline exceeded; "
                                                 "retry later.", 1)
                            chunk = response.read1(min(CHUNK, max_bytes + 1 - written))
                            if not chunk:
                                break
                            stream.write(chunk)
                            written += len(chunk)
                            if written > max_bytes:
                                raise SourceError(
                                    f"{host}: response exceeds the download byte limit.")
                        stream.flush()
                        os.fsync(stream.fileno())
                    except BaseException:
                        temporary.unlink(missing_ok=True)
                        raise
                os.replace(temporary, target)
                return written

        written = self._with_retries(read, host)
        self._stamp(time.time())
        return written

    def _read_body(self, response: Any, host: str, limit: int, label: str) -> bytes:
        """Drain a response under a byte ceiling and a wall-clock deadline."""
        length = response.headers.get("Content-Length")
        if length is not None:
            try:
                size = int(length)
            except ValueError as exc:
                raise SourceError(f"{host}: invalid Content-Length.") from exc
            if size < 0 or size > limit:
                raise SourceError(f"{host}: response exceeds the {label} byte limit.")
        deadline = time.monotonic() + self.timeout
        chunks: list[bytes] = []
        size = 0
        while True:
            if time.monotonic() > deadline:
                raise _Retryable(f"{host}: response deadline exceeded; retry later.", 1)
            chunk = response.read1(min(CHUNK, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                raise SourceError(f"{host}: response exceeds the {label} byte limit.")
        return _decompress(b"".join(chunks), response.headers.get("Content-Encoding", ""), host)

    def _request(self, url: str, host: str, method: str = "GET", body: bytes | None = None,
                 headers: dict | None = None) -> Any:
        merged = {"User-Agent": self.user_agent, "Accept": "application/json",
                  "Accept-Encoding": "identity"}
        if headers:
            merged.update(headers)
        if body is not None:
            merged["Content-Type"] = "application/json"
        request = Request(url, data=body, headers=merged, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if response.status == 204:
                    return []
                if response.status != 200:
                    raise SourceError(f"{host}: unexpected HTTP {response.status}; retry later.")
                raw = self._read_body(response, host, self.max_bytes, "response")
                try:
                    payload = _decode(raw)
                except (ValueError, UnicodeError, RecursionError) as exc:
                    if b"limit requests" in raw[:512].lower():
                        raise _Retryable(
                            f"{host}: provider rate limit reached; retry later.",
                            HOST_MIN_RATES.get(host, 0) + 1) from exc
                    raise SourceError(f"{host}: invalid JSON response; retry or verify the "
                                      "official API status.") from exc
                if not isinstance(payload, (dict, list)):
                    raise SourceError(f"{host}: invalid JSON response; expected an object or array.")
        except HTTPError as exc:
            try:
                if exc.code == 429 or 500 <= exc.code <= 599:
                    raise _Retryable(f"{host}: HTTP {exc.code}; retry later.",
                                     _retry_after(exc.headers.get("Retry-After"))) from exc
                raise SourceError(f"{host}: HTTP {exc.code}; verify parameters and API "
                                  "access policy.") from exc
            finally:
                exc.close()
        except (URLError, OSError, HTTPException) as exc:
            raise _Retryable(f"{host}: network request failed: {exc}", 0) from exc
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise SourceError(f"{host}: invalid JSON response; retry or verify the official "
                              "API status.") from exc
        return payload
