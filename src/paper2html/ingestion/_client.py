"""Rate-limited HTTP client for arXiv requests.

All arXiv I/O (HTML, PDF, figure assets) MUST go through get_client().
Ticket #004 must also reuse get_client() for any additional PDF-related HTTP.

Rate limit: 1 request per 3 seconds, globally enforced, strictly serial.
"""

from __future__ import annotations

import logging
import threading
import zlib
from time import monotonic, sleep
from typing import Any

import httpx

from paper2html import __version__
from urllib.parse import urlsplit, urljoin
from paper2html.ingestion._limits import LIMITS


def validate_url(url: str) -> str:
    if any(ord(char) <= 32 or ord(char) == 127 for char in url):
        raise ValueError(f"Unsafe ingestion URL: {url!r}")
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname not in {"arxiv.org", "export.arxiv.org"}
        or parts.username is not None
        or parts.password is not None
        or parts.port not in (None, 443)
        or parts.netloc.endswith(":")
        or "\\" in url
    ):
        raise ValueError(f"Unsafe ingestion URL: {url!r}")
    return url


def _read_body(response: httpx.Response, limit: int, deadline: float) -> bytes:
    """Bound encoded bytes and decompressor output before retaining either."""
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in {"identity", "gzip", "deflate"}:
        raise ValueError(f"Unsupported ingestion Content-Encoding: {encoding}")
    length = response.headers.get("content-length")
    if length is not None and (int(length) < 0 or int(length) > limit):
        raise ValueError("HTTP encoded body exceeds ingestion byte limit")
    decoder = (
        None
        if encoding == "identity"
        else zlib.decompressobj(
            16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS
        )
    )
    # MockTransport commonly provides pre-read identity responses. Production
    # streamed responses always take iter_raw, bypassing httpx decompression.
    if response.is_stream_consumed:
        if decoder is not None:
            raise ValueError("Compressed response must be streamed, not pre-decoded")
        chunks = iter((response.content,))
    else:
        chunks = response.iter_raw()
    data = bytearray()
    encoded = 0
    try:
        for chunk in chunks:
            if monotonic() >= deadline:
                raise TimeoutError("Ingestion request deadline exceeded")
            encoded += len(chunk)
            if encoded > limit:
                raise ValueError("HTTP encoded body exceeds ingestion byte limit")
            if decoder is not None:
                chunk = decoder.decompress(chunk, limit - len(data) + 1)
                if decoder.unused_data:
                    raise ValueError(
                        "Trailing/concatenated compressed HTTP data forbidden"
                    )
            if len(data) + len(chunk) > limit:
                raise ValueError("HTTP decoded body exceeds ingestion byte limit")
            data.extend(chunk)
    except zlib.error as exc:
        raise ValueError("Invalid compressed HTTP body") from exc
    if decoder is not None and not decoder.eof:
        raise ValueError("Truncated compressed HTTP body")
    if monotonic() >= deadline:
        raise TimeoutError("Ingestion request deadline exceeded")
    return bytes(data)


logger = logging.getLogger(__name__)

_RATE_LIMIT_SECONDS: float = 3.0

# Module-level singleton state (protected by _init_lock).
_singleton: RateLimitedClient | None = None
_init_lock = threading.Lock()


class RateLimitedClient:
    """An httpx.Client wrapper that enforces a per-request rate limit.

    The lock is held for the full wait + request + timestamp cycle so that
    concurrent callers queue strictly serially rather than racing.
    """

    def __init__(self, http_client: httpx.Client | None = None) -> None:
        self._owns_client = http_client is None
        self._client = (
            http_client
            if http_client is not None
            else httpx.Client(
                follow_redirects=False,
                timeout=LIMITS.request_seconds,
                trust_env=False,
            )
        )
        self._lock = threading.Lock()
        self._last_request_time: float = 0.0

    def close(self) -> None:
        """Release only the HTTP transport owned by this wrapper."""
        with self._lock:
            if self._owns_client:
                self._client.close()

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        """GET *url* through the rate-limited client.

        The lock is held for the entire wait + request + timestamp cycle.
        Stamping AFTER the response means the gap is measured
        request-completion to request-completion.
        """
        limit = kwargs.pop("max_bytes", LIMITS.body_bytes)
        request_headers = httpx.Headers(kwargs.pop("headers", None))
        # Identify this application honestly; arXiv may reject generic HTTP
        # library user agents even when the same public URL is available.
        request_headers.setdefault(
            "User-Agent",
            f"paper2html/{__version__} (+https://github.com/JewelsHovan/paper2html)",
        )
        request_headers["Accept-Encoding"] = "identity"
        deadline = monotonic() + LIMITS.total_seconds
        if not self._lock.acquire(timeout=LIMITS.total_seconds):
            raise TimeoutError("Ingestion request queue deadline exceeded")
        try:
            for hop in range(LIMITS.redirects + 1):
                validate_url(url)
                remaining = deadline - monotonic()
                if remaining <= _RATE_LIMIT_SECONDS:
                    raise TimeoutError("Ingestion request deadline exceeded")
                self._throttle()
                if monotonic() >= deadline:
                    raise TimeoutError("Ingestion request deadline exceeded")
                try:
                    with self._client.stream(
                        "GET",
                        url,
                        follow_redirects=False,
                        timeout=min(LIMITS.request_seconds, deadline - monotonic()),
                        headers=request_headers,
                        **kwargs,
                    ) as response:
                        if monotonic() >= deadline:
                            raise TimeoutError("Ingestion request deadline exceeded")
                        if response.status_code in (301, 302, 303, 307, 308):
                            location = response.headers.get("location")
                            if not location:
                                raise ValueError(
                                    "Ingestion redirect missing Location header"
                                )
                            url = validate_url(urljoin(str(response.url), location))
                            continue
                        data = _read_body(response, limit, deadline)
                        headers = dict(response.headers)
                        headers.pop("content-encoding", None)
                        headers.pop("content-length", None)
                        return httpx.Response(
                            response.status_code,
                            headers=headers,
                            content=data,
                            request=response.request,
                        )
                finally:
                    self._last_request_time = monotonic()
        finally:
            self._lock.release()
        raise ValueError("Ingestion redirect limit exceeded")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _throttle(self) -> None:
        """Sleep until the rate-limit interval has elapsed.

        MUST be called while self._lock is held.
        """
        elapsed = monotonic() - self._last_request_time
        wait = _RATE_LIMIT_SECONDS - elapsed
        if wait > 0:
            logger.debug("Rate limit: sleeping %.2f s", wait)
            sleep(wait)


# ------------------------------------------------------------------
# Module-level singleton
# ------------------------------------------------------------------


def get_client() -> RateLimitedClient:
    """Return the module-level singleton RateLimitedClient (lazy, double-checked)."""
    global _singleton
    if _singleton is None:
        with _init_lock:
            if _singleton is None:
                _singleton = RateLimitedClient()
    return _singleton


def _reset_singleton(client: RateLimitedClient | None = None) -> None:
    """Replace (or clear) the singleton.  Test hook — do not call in production."""
    global _singleton
    with _init_lock:
        _singleton = client


def close_client() -> None:
    """Close and reset the ingestion singleton (injected transports stay open)."""
    global _singleton
    with _init_lock:
        client, _singleton = _singleton, None
    if client is not None:
        client.close()
