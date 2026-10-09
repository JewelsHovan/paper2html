"""Canonical, source-order article reconstruction. Never trusts stored markup/assets."""

from __future__ import annotations

import base64
import hashlib
import re
import warnings
from urllib.parse import urlsplit
from typing import TYPE_CHECKING

from bs4 import BeautifulSoup, Comment, NavigableString, Tag
from markupsafe import Markup, escape

from paper2html._render_safety import raster_mime, sanitize_markup

if TYPE_CHECKING:
    from paper2html.models.document import NormalizedDocument


class _SourceBudgetError(ValueError):
    """A document budget failure is never an individual-image omission."""


_HTML = set(
    "article body section div header footer aside h1 h2 h3 h4 h5 h6 p span a b strong i em u s small sub sup br hr wbr pre code blockquote cite abbr address time figure figcaption table caption thead tbody tfoot tr td th colgroup col ul ol li dl dt dd".split()
)


def asset_key(node: Tag) -> str:
    if node.name == "svg":
        return "inline-svg:" + hashlib.sha256(str(node).encode()).hexdigest()
    return str(node.get("data" if node.name == "object" else "src", ""))


def collect_source_assets(
    article: Tag, supplied: dict[str, bytes], notices: list[str]
) -> dict[str, bytes]:
    """Validate ingress bytes, converting inline SVG without any remote reads.

    External assets must already have traversed guarded ingestion transport.
    Total retained raster bytes are bounded independently of per-image limits.
    """
    nodes = article.find_all(["img", "object", "svg"])
    if not nodes:
        return {}
    from paper2html.ingestion._limits import LIMITS
    from paper2html.ingestion._figure_fetcher import _data_uri
    from paper2html.ingestion._safety import svg_png

    assets: dict[str, bytes] = {}
    total = 0
    for node in nodes:
        if node.find_parent(["svg", "object"]) is not None:
            continue
        key = asset_key(node)
        if key in assets:
            continue
        try:
            if key in supplied:
                data = supplied[key]
            elif node.name == "svg":
                data = (
                    svg_png(str(node).encode(), embedded_images=supplied)
                    if node.find("img") is not None
                    else svg_png(str(node).encode())
                )
            elif key.lower().startswith("data:"):
                data = _data_uri(key, allow_svg=True)
            else:
                raise ValueError("no fetched image bytes")
            raster_mime(data)
            if total + len(data) > LIMITS.source_bytes:
                raise ValueError("aggregate source asset byte limit exceeded")
            assets[key] = data
            total += len(data)
        except Exception as exc:
            notices.append(f"Source image {key[:120]!r} omitted: {exc}")
    # Preserve original nested scientific rasters as well as their panel view.
    # They are offered as inert downloads, not additional article images.
    for node in article.select("svg foreignobject img"):
        key = str(node.get("src", ""))
        if not key or key in assets:
            continue
        raw = supplied.get(key)
        if raw is None and key.lower().startswith("data:"):
            raw = _data_uri(key)
        if raw is None:
            continue
        raster_mime(raw)
        if total + len(raw) > LIMITS.source_bytes:
            raise ValueError("aggregate source asset byte limit exceeded")
        assets[key] = raw
        total += len(raw)
    return assets


def _image(data: bytes, alt: str) -> str:
    mime = raster_mime(data)
    uri = f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
    return f'<img src="{uri}" alt="{escape(alt)}" loading="lazy">'


def _safe_link(href: str) -> bool:
    try:
        return not any(ord(c) < 32 for c in href) and urlsplit(href).scheme.lower() in {
            "http",
            "https",
            "mailto",
        }
    except ValueError:
        return False


def sanitize_article(source: str, assets: dict[str, bytes]) -> Markup:
    """Strict HTML/MathML reconstruction; no source CSS, scripts or resource URLs.

    IDs and fragment links share a source- prefix. MathML integration points
    use the same hardened fragment sanitizer as legacy normalized rendering.
    """
    from paper2html.ingestion._limits import LIMITS

    if len(source) > LIMITS.body_bytes or len(source.encode()) > LIMITS.body_bytes:
        raise ValueError("Source article byte limit exceeded")
    if (
        len(assets) > 20_000
        or sum(len(v) for v in assets.values()) > LIMITS.source_bytes
    ):
        raise ValueError("Source asset budget exceeded")
    soup = BeautifulSoup(source, "html.parser")
    stack = [(soup, 0)]
    count = 0
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > 200_000 or depth > 128:
            raise ValueError("Source article node/depth limit exceeded")
        if isinstance(node, Tag) and node.name != "math":
            stack.extend((c, depth + 1) for c in node.children)

    image_number = 0
    emitted_bytes = 0
    uri_cache: dict[int, tuple[str, str]] = {}

    def image_uri(data: bytes) -> tuple[str, str]:
        nonlocal emitted_bytes
        emitted_bytes += 4 * ((len(data) + 2) // 3) + 64
        if emitted_bytes > LIMITS.source_bytes:
            raise _SourceBudgetError("Source emitted image byte budget exceeded")
        key = id(data)
        if key not in uri_cache:
            mime = raster_mime(data)
            uri_cache[key] = (
                mime,
                f"data:{mime};base64," + base64.b64encode(data).decode("ascii"),
            )
        return uri_cache[key]

    def emit(node: object) -> str:
        nonlocal image_number
        if isinstance(node, Comment):
            return ""
        if isinstance(node, NavigableString):
            return str(escape(str(node)))
        if not isinstance(node, Tag):
            return ""
        name = node.name.lower()
        if name == "math":
            clean = str(sanitize_markup(str(node)))
            if clean and node.get("id"):
                clean = clean.replace(
                    "<math", f'<math id="source-{escape(str(node["id"]))}"', 1
                )
            return clean
        if name in {"img", "object", "svg"}:
            key = asset_key(node)
            try:
                image_number += 1
                image_id = f"reader-image-{image_number}"
                _, picture_uri = image_uri(assets[key])
                picture = f'<img id="{image_id}" src="{picture_uri}" alt="{escape(str(node.get("alt", "Source image")))}" loading="lazy">'
                downloads = []
                if name == "svg":
                    for index, nested in enumerate(node.select("foreignobject img"), 1):
                        raw = assets.get(str(nested.get("src", "")))
                        if raw is None:
                            continue
                        mime, uri = image_uri(raw)
                        extension = mime.split("/", 1)[1]
                        downloads.append(
                            f'<a class="source-image-download" href="{uri}" '
                            f'download="embedded-image-{image_number}-{index}.{extension}" '
                            f'aria-label="Download full-resolution embedded image {index}" '
                            f'title="Download full-resolution embedded image {index}"></a>'
                        )
                return (
                    '<span class="source-image-scroll" role="region" '
                    'aria-label="Source image; click to view original size" tabindex="0">'
                    f'<a href="#{image_id}" title="Click to view original size">{picture}</a>'
                    + "".join(downloads)
                    + "</span>"
                )
            except _SourceBudgetError:
                raise
            except (KeyError, ValueError, OSError, SyntaxError) as exc:
                warnings.warn(
                    f"Source image {key[:120]!r} omitted: {exc}", stacklevel=2
                )
                return '<span class="source-image-missing">[Source image unavailable]</span>'
        if name not in _HTML:
            return ""
        attrs = []
        for key in ("id", "class", "title", "lang", "dir"):
            value = node.get(key)
            if value:
                value = " ".join(value) if isinstance(value, list) else str(value)
                if key == "id":
                    value = "source-" + value
                # Prefix source classes to avoid invoking reading-aid JS handlers.
                if key == "class":
                    value = " ".join("src-" + c for c in value.split())
                attrs.append(f' {key}="{escape(value)}"')
        for key in ("colspan", "rowspan", "span"):
            value = str(node.get(key, ""))
            if name in {"td", "th", "col", "colgroup"} and re.fullmatch(
                r"[0-9]{1,3}", value
            ):
                attrs.append(f' {key}="{value}"')
        if name == "a":
            href = str(node.get("href", "")).strip()
            if href.startswith("#"):
                attrs.append(f' href="#source-{escape(href[1:])}"')
            elif _safe_link(href):
                attrs.append(f' href="{escape(href)}" rel="noopener noreferrer"')
        name = "div" if name == "body" else name
        opening = f"<{name}{''.join(attrs)}>"
        if name in {"br", "hr", "wbr", "col"}:
            return opening
        content = opening + "".join(emit(c) for c in node.children) + f"</{name}>"
        if name == "table" and node.find_parent("table") is None:
            # Span wrapper preserves phrasing contexts without adding an invalid
            # div, while reader CSS makes EVERY outer table locally scrollable.
            phrasing = node.find_parent(
                [
                    "p",
                    "span",
                    "a",
                    "b",
                    "strong",
                    "i",
                    "em",
                    "u",
                    "s",
                    "small",
                    "sub",
                    "sup",
                    "code",
                    "cite",
                    "abbr",
                    "time",
                ]
            )
            wrapper = "span" if phrasing is not None else "div"
            return (
                f'<{wrapper} class="source-table-scroll" role="region" '
                'aria-label="Scrollable table or equation" tabindex="0">'
                + content
                + f"</{wrapper}>"
            )
        return content

    return Markup("".join(emit(c) for c in soup.children))


def render_source(doc: NormalizedDocument) -> Markup | None:
    from paper2html.ingestion._limits import LIMITS

    if doc.original_html:
        return sanitize_article(doc.original_html, doc.source_assets)
    if not doc.page_assets:
        return None
    if (
        len(doc.page_assets) > LIMITS.pages
        or sum(map(len, doc.page_assets)) > LIMITS.source_bytes
    ):
        raise ValueError("PDF facsimile asset budget exceeded")
    parts = [
        '<div class="source-facsimile"><p>PDF page facsimile (including vector figures). Extracted searchable text is provided separately; it is not a semantic reconstruction of mathematics.</p><nav id="source-pages">'
    ]
    parts.extend(
        f'<a href="#source-page-{n}">Page {n}</a> '
        for n in range(1, len(doc.page_assets) + 1)
    )
    parts.append("</nav>")
    for n, data in enumerate(doc.page_assets, 1):
        parts.append(f'<section id="source-page-{n}"><h2>Page {n}</h2>')
        parts.append(_image(data, f"Original PDF page {n}"))
        text = doc.sections[n - 1].text_content if n <= len(doc.sections) else ""
        parts.append(
            f'<details><summary>Searchable page text</summary><pre>{escape(text)}</pre></details><a href="#source-pages">Back to pages</a></section>'
        )
    parts.append("</div>")
    return Markup("".join(parts))
