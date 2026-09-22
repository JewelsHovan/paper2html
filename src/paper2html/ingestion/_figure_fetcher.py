"""Bounded figure fetching, original raster preservation and safe SVG conversion."""

from __future__ import annotations

import logging
import base64
import binascii
import re
from io import BytesIO
from PIL import Image
from paper2html.ingestion._limits import LIMITS
from paper2html.ingestion._safety import raster, svg_png

from paper2html.ingestion._client import get_client
from paper2html.ingestion._types import Figure, FigureRef, MathMLNode

logger = logging.getLogger(__name__)


def _is_svg(data: bytes) -> bool:
    """Return True if *data* looks like SVG content."""
    head = data[:512].lstrip()
    return head.startswith((b"<svg", b"<?xml"))


def _data_uri(uri: str, *, allow_svg: bool = False) -> bytes:
    # Bound whitespace as well as base64 characters before splitting/copying.
    encoded_limit = 4 * ((LIMITS.image_bytes + 2) // 3)
    if len(uri) > encoded_limit * 2 + 256:
        raise ValueError("Oversized raster data URI")
    header, separator, payload = uri.partition(",")
    match = re.fullmatch(
        r"data:\s*image/(png|jpeg|gif|webp|svg\+xml)\s*;\s*base64\s*", header, re.I
    )
    if (
        not separator
        or match is None
        or (match[1].lower() == "svg+xml" and not allow_svg)
    ):
        raise ValueError(
            "Malformed or unsupported data URI (base64 raster required; SVG allowed only at figure ingress)"
        )
    payload = re.sub(r"[ \t\r\n\f]", "", payload)
    if len(payload) > encoded_limit:
        raise ValueError("Oversized raster data URI")
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Malformed base64 raster data URI") from exc
    if len(data) > LIMITS.image_bytes:
        raise ValueError("Oversized raster data URI")
    if match[1].lower() == "svg+xml":
        return svg_png(data)
    with Image.open(BytesIO(data)) as image:
        if (image.format or "").lower() != match[1].lower():
            raise ValueError("Data URI MIME type does not match raster")
    return data


def _mime(data: bytes) -> str:
    with Image.open(BytesIO(data)) as image:
        return Image.MIME[image.format]


def build_figures(
    refs: list[FigureRef],
    cache: dict[bytes | str | None, bytes | None] | None = None,
    warnings: list[str] | None = None,
) -> list[Figure]:
    """Preserve validated rasters; collect failures once per unique asset."""
    cache = {} if cache is None else cache
    figures: list[Figure] = []
    for ref in refs:
        key = ref.inline_svg if ref.inline_svg is not None else ref.src_url
        if key not in cache:
            try:
                if ref.inline_svg is not None:
                    raw = ref.inline_svg
                elif ref.src_url and ref.src_url[:5].lower() == "data:":
                    raw = _data_uri(ref.src_url, allow_svg=True)
                elif ref.src_url:
                    response = get_client().get(
                        ref.src_url, max_bytes=LIMITS.image_bytes
                    )
                    response.raise_for_status()
                    raw = response.content
                else:
                    raise ValueError("Missing image source")
                cache[key] = svg_png(raw) if _is_svg(raw) else raster(raw)
            except Exception as exc:
                message = f"Skipping figure {ref.caption!r} ({(ref.src_url or 'inline SVG')[:160]}): {exc}"
                logger.warning("%s", message)
                if warnings is not None:
                    warnings.append(message)
                cache[key] = None
        data = cache[key]
        if data is not None:
            figures.append(
                Figure(
                    caption=ref.caption,
                    asset_bytes=data,
                    mime_type=_mime(data),
                    mathml_nodes=[MathMLNode(raw_mathml=m) for m in ref.caption_mathml],
                    raw_src=ref.raw_src,
                )
            )
    return figures
