"""arXiv metadata fetching and paper-ID normalisation."""

from __future__ import annotations

import logging
import re

from urllib.parse import urlencode
from defusedxml import ElementTree

from paper2html.ingestion._client import get_client
from paper2html.ingestion._types import ArxivMetadata

logger = logging.getLogger(__name__)

# Accepts:
#   optional https?://arxiv.org/(abs|pdf)/ prefix
#   new-style: YYMM.NNNNN  (4- or 5-digit suffix)
#   old-style: category[.XX]/NNNNNNN  (e.g. math/0211159, hep-th/9901001)
#   optional vN version suffix
_ID_PATTERN = re.compile(
    r"(?:https?://arxiv\.org/(?:abs|pdf)/)?"
    r"("
    r"\d{4}\.\d{4,5}"  # new-style
    r"|"
    r"[a-z-]+(?:\.[A-Z]{2})?/\d{7}"  # old-style
    r")"
    r"(?:v\d+)?$"
)


def normalise_id(raw: str) -> str:
    """Strip URL prefix and version suffix, returning a canonical arXiv ID.

    Raises ValueError if *raw* does not look like a valid arXiv identifier.
    """
    raw = raw.strip()
    match = _ID_PATTERN.match(raw)
    if not match:
        raise ValueError(f"Cannot parse arXiv ID from: {raw!r}")
    return match.group(1)


def fetch_metadata(arxiv_id: str) -> ArxivMetadata:
    """Fetch bibliographic metadata for *arxiv_id* (already normalised).

    Uses the bounded common transport (no implicit retries or external XML entities).

    Raises ValueError if no results are returned.
    """
    response = get_client().get(
        "https://export.arxiv.org/api/query?" + urlencode({"id_list": arxiv_id})
    )
    response.raise_for_status()
    root = ElementTree.fromstring(response.content, forbid_dtd=True)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    entry = root.find("a:entry", ns)
    if entry is None:
        raise ValueError(f"No arXiv metadata found for ID: {arxiv_id!r}")
    entry_id = entry.findtext("a:id", "", ns)
    try:
        actual_id = normalise_id(entry_id)
    except ValueError as exc:
        raise ValueError(f"Invalid arXiv metadata entry ID: {entry_id!r}") from exc
    if actual_id != normalise_id(arxiv_id):
        raise ValueError(
            f"arXiv metadata ID mismatch: requested {arxiv_id!r}, got {entry_id!r}"
        )

    def text(name: str) -> str:
        return " ".join(entry.findtext("a:" + name, "", ns).split())

    return ArxivMetadata(
        arxiv_id=arxiv_id,
        title=text("title"),
        abstract=text("summary"),
        authors=[a.findtext("a:name", "", ns) for a in entry.findall("a:author", ns)],
        categories=[a.get("term", "") for a in entry.findall("a:category", ns)],
        published=text("published")[:10],
        updated=text("updated")[:10],
    )
