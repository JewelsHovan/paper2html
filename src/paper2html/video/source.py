"""Full immutable source acquisition and page evidence catalog."""

from __future__ import annotations

import math
import re
from pathlib import Path
from time import monotonic
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
import httpx
import pymupdf

from paper2html import __version__
from paper2html.ingestion._client import _read_body, validate_url
from paper2html.ingestion._limits import LIMITS, validate_image_dimensions
from .core import Blocked, owned, put, save, VERSION

REV = re.compile(r"(?:https://arxiv\.org/(?:abs|html|pdf)/)?(\d{4}\.\d{4,5}v[1-9]\d*)$")
SOURCE_LIMIT = 60_000_000


def _exact_arxiv_url(url: str, kind: str, revision: str) -> str:
    """Validate a redirect target BEFORE dispatch, not only the final response."""
    try:
        validate_url(url)
        parsed = urlparse(url)
        if (
            parsed.hostname != "arxiv.org"
            or parsed.path != f"/{kind}/{revision}"
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Unpaired revision/host")
    except ValueError:
        raise Blocked(
            "SOURCE_REVISION",
            f"{kind} must stay at explicit {revision}; redirect rejected",
        ) from None
    return url


def _fetch_arxiv(client: httpx.Client, kind: str, revision: str) -> tuple[bytes, dict]:
    url = f"https://arxiv.org/{kind}/{revision}"
    requested = url
    deadline = monotonic() + LIMITS.total_seconds
    headers = {
        "User-Agent": f"paper2html/{__version__} (+https://github.com/JewelsHovan/paper2html)",
        "Accept-Encoding": "identity",
    }
    for _ in range(LIMITS.redirects + 1):
        _exact_arxiv_url(url, kind, revision)
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise Blocked("SOURCE_TIMEOUT", "Source deadline exceeded")
        try:
            with client.stream(
                "GET",
                url,
                headers=headers,
                follow_redirects=False,
                timeout=min(LIMITS.request_seconds, remaining),
            ) as response:
                _exact_arxiv_url(str(response.url), kind, revision)
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        raise Blocked("SOURCE_REVISION", "Redirect without Location")
                    url = _exact_arxiv_url(
                        urljoin(str(response.url), location), kind, revision
                    )
                    continue
                if response.status_code != 200:
                    raise Blocked(
                        "SOURCE_REVISION",
                        f"{kind} returned HTTP {response.status_code}",
                    )
                try:
                    data = _read_body(
                        response, min(SOURCE_LIMIT, LIMITS.source_bytes), deadline
                    )
                except (ValueError, TimeoutError):
                    raise Blocked(
                        "SOURCE_SIZE",
                        "Source encoded/decoded body limit or deadline exceeded",
                    ) from None
                return data, {
                    "requested_url": requested,
                    "final_url": str(response.url),
                }
        except httpx.HTTPError:
            raise Blocked("SOURCE_FETCH", "arXiv source fetch failed") from None
    raise Blocked("SOURCE_REVISION", "Too many arXiv redirects")


def prepare(root: Path, source: str, *, transport=None) -> dict:
    """Require explicit arXiv revision and both source representations; PDF local is PDF-only."""
    if owned(root, "sources/manifest.json").exists():
        raise Blocked(
            "SOURCE_EXISTS", "Sources already prepared; create another project"
        )
    match = REV.fullmatch(source)
    if match:
        revision = match.group(1)
        blobs = {}
        provenance = {}
        with httpx.Client(
            timeout=LIMITS.request_seconds,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        ) as client:
            for kind in ("html", "pdf"):
                blobs[kind], provenance[kind] = _fetch_arxiv(client, kind, revision)
        if b"<html" not in blobs["html"].lower() or not blobs["pdf"].startswith(
            b"%PDF-"
        ):
            raise Blocked("SOURCE_FORMAT", "Expected full HTML and PDF")
        # Never strip the article: keep complete native response byte-for-byte.
        identity = {
            "type": "arxiv",
            "revision": revision,
            "coverage": "full HTML + full PDF",
        }
    else:
        path = Path(source)
        if path.suffix.lower() != ".pdf" or not path.is_file() or path.is_symlink():
            raise Blocked(
                "SOURCE_ID", "Use an explicit arXiv vN revision or a local PDF file"
            )
        if path.stat().st_size > SOURCE_LIMIT:
            raise Blocked("SOURCE_SIZE", "PDF exceeds 60 MB")
        blobs = {"pdf": path.read_bytes()}
        provenance = {"pdf": {"local_path": str(path.resolve())}}
        identity = {
            "type": "local_pdf",
            "revision": None,
            "coverage": "PDF only; no matching HTML claimed",
        }
    if not blobs["pdf"].startswith(b"%PDF-"):
        raise Blocked("SOURCE_FORMAT", "Not a PDF")
    with pymupdf.open(stream=blobs["pdf"], filetype="pdf") as doc:
        if not doc.is_pdf or doc.needs_pass or not 0 < len(doc) <= 200:
            raise Blocked("SOURCE_PDF", "Invalid, encrypted, or oversized PDF")
        pages = len(doc)
        catalog = []
        for index, page in enumerate(doc):
            catalog.append(
                {
                    "id": f"page-{index + 1}",
                    "pdf_page": index + 1,
                    "text": page.get_text(),
                }
            )
    if "html" in blobs:
        soup = BeautifulSoup(blobs["html"], "html.parser")
        sections = [
            {"id": tag["id"], "text": tag.get_text(" ", strip=True)}
            for tag in soup.find_all(id=True)
            if tag.name in ("section", "figure", "table")
        ]
        for tag in soup.find_all(["figure", "table"]):
            if not tag.get("id"):
                raise Blocked(
                    "SOURCE_CATALOG", "Source figure/table missing stable native ID"
                )
        # Scientific projection keeps every body section, figure, table, math and
        # image reference. Only executable/style/navigation machinery is removed.
        # Raw byte-for-byte original remains separately hashed and immutable.
        for tag in soup.find_all(["script", "style"]):
            tag.decompose()
        projection = str(soup.body or soup).encode("utf-8")
        put(root, "sources/projection.html", projection)
    else:
        sections = []
    paths = {kind: f"sources/original.{kind}" for kind in blobs}
    for kind, data in blobs.items():
        put(root, paths[kind], data)
    save(
        root,
        "sources/catalog.json",
        {"version": VERSION, "pages": catalog, "html_elements": sections},
    )
    all_paths = list(paths.values()) + ["sources/catalog.json"]
    if "html" in blobs:
        all_paths.append("sources/projection.html")
    from .core import hashes

    m = {
        "version": VERSION,
        "identity": identity,
        "paths": paths,
        "provenance": provenance,
        "bytes": {k: len(v) for k, v in blobs.items()},
        "pdf_pages": pages,
        "hashes": hashes(root, all_paths),
    }
    save(root, "sources/manifest.json", m)
    return m


def page_asset(root: Path, page: int) -> str:
    """Native original PDF page raster, never synthetic paper imagery."""
    from .core import manifest_source

    m = manifest_source(root)
    if not isinstance(page, int) or not 1 <= page <= m["pdf_pages"]:
        raise Blocked("SOURCE_PAGE", "Page is outside original PDF")
    name = f"assets/pages/page-{page}.png"
    with pymupdf.open(owned(root, m["paths"]["pdf"])) as doc:
        source_page = doc[page - 1]
        rect = source_page.rect
        if (
            not all(
                math.isfinite(value) for value in (rect.x0, rect.y0, rect.x1, rect.y1)
            )
            or rect.width <= 0
            or rect.height <= 0
        ):
            raise Blocked(
                "SOURCE_PAGE_SIZE", "PDF page has invalid or non-finite geometry"
            )
        # Bound native raster allocation before get_pixmap. The +2 allows for
        # integer device-pixel rounding at fractional PDF rectangle origins.
        predicted_width = math.ceil(rect.width * 1.5) + 2
        predicted_height = math.ceil(rect.height * 1.5) + 2
        try:
            validate_image_dimensions(predicted_width, predicted_height, limits=LIMITS)
        except ValueError:
            raise Blocked(
                "SOURCE_PAGE_SIZE",
                "PDF page raster exceeds ingestion dimension/pixel limits",
            ) from None
        pix = source_page.get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False)
        put(root, name, pix.tobytes("png"))
    return name
