"""SVG validation and rendering with no resource fetches (including local files)."""

from __future__ import annotations

import math
import base64
from io import BytesIO
from PIL import Image
import re
from typing import Any

from defusedxml import ElementTree
import tinycss2

from paper2html.ingestion._limits import LIMITS


# Only SVG presentation attributes are CSS values. Prose/data/geometry attributes
# (e.g. aria-label="Bob's plot") must not be interpreted by a CSS tokenizer.
_PRESENTATION = frozenset(
    """
    alignment-baseline baseline-shift clip clip-path clip-rule color
    color-interpolation color-interpolation-filters color-profile color-rendering
    cursor direction display dominant-baseline enable-background fill fill-opacity
    fill-rule filter flood-color flood-opacity font-family font-size font-size-adjust
    font-stretch font-style font-variant font-weight glyph-orientation-horizontal
    glyph-orientation-vertical image-rendering kerning letter-spacing lighting-color
    marker marker-start marker-mid marker-end mask opacity overflow paint-order
    pointer-events shape-rendering stop-color stop-opacity stroke stroke-dasharray
    stroke-dashoffset stroke-linecap stroke-linejoin stroke-miterlimit stroke-opacity
    stroke-width text-anchor text-decoration text-rendering unicode-bidi vector-effect
    visibility word-spacing writing-mode
""".split()
)
_CAIRO_GUIDANCE = (
    "SVG rendering unavailable: install Cairo (brew install cairo / apt install libcairo2). "
    "For Homebrew loader issues, set DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib."
)


class _SvgParser(ElementTree.DefusedXMLParser):
    """Allow the standard inert SVG 1.1 declaration, never internal subsets.

    Expat does not load external DTDs; defusedxml's external-entity handler
    remains enabled as a second guard. Matplotlib emits this declaration.
    """

    def defused_start_doctype_decl(
        self,
        name: str,
        sysid: str | None,
        pubid: str | None,
        has_internal_subset: bool,
    ) -> None:
        if (
            name != "svg"
            or has_internal_subset
            or pubid != "-//W3C//DTD SVG 1.1//EN"
            or sysid
            not in {
                "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd",
                "https://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd",
            }
        ):
            raise ValueError("SVG DTD/entities are forbidden")


def _fragment(value: str) -> str:
    value = value.strip()
    if not value.startswith("#") or len(value) == 1 or any(c.isspace() for c in value):
        raise ValueError("SVG references must be local fragments")
    return value[1:]


def _tokens(tokens: list[Any], references: set[str]) -> None:
    for token in tokens:
        if token.type == "error":
            raise ValueError("Malformed SVG CSS")
        if token.type == "url":
            references.add(_fragment(token.value))
        elif token.type == "function":
            if token.lower_name == "url":
                args = [
                    t
                    for t in token.arguments
                    if t.type not in {"whitespace", "comment"}
                ]
                if len(args) != 1 or args[0].type != "string":
                    raise ValueError("Malformed SVG CSS URL")
                references.add(_fragment(args[0].value))
            else:
                _tokens(token.arguments, references)
        elif hasattr(token, "content"):
            _tokens(token.content, references)
        elif token.type == "at-keyword":
            raise ValueError("SVG CSS at-rules are forbidden")


def _declarations(content: Any, references: set[str]) -> None:
    for item in tinycss2.parse_declaration_list(
        content, skip_comments=True, skip_whitespace=True
    ):
        if item.type != "declaration":
            raise ValueError("Only SVG CSS declarations are allowed")
        _tokens(item.value, references)


def _stylesheet(text: str, references: set[str]) -> None:
    for rule in tinycss2.parse_stylesheet(
        text, skip_comments=True, skip_whitespace=True
    ):
        if rule.type != "qualified-rule":
            raise ValueError("SVG CSS imports/at-rules are forbidden")
        _tokens(rule.prelude, references)
        _declarations(rule.content, references)


def _dimension(value: str) -> float:
    match = re.fullmatch(
        r"\s*(\d+(?:\.\d*)?|\.\d+)\s*(px|pt|pc|mm|cm|in)?\s*", value, re.I
    )
    if match is None:
        raise ValueError("SVG dimensions require absolute units or a bounded viewBox")
    units = {
        "px": 1,
        "pt": 96 / 72,
        "pc": 16,
        "mm": 96 / 25.4,
        "cm": 96 / 2.54,
        "in": 96,
    }
    return float(match[1]) * units[(match[2] or "px").lower()]


def _deny_fetch(url: str, resource_type: str) -> bytes:
    raise ValueError(f"External SVG resource forbidden: {url}")


def _convert(
    data: bytes,
    width: int,
    height: int,
    resources: dict[str, bytes] | None = None,
) -> bytes:
    try:
        from cairosvg.surface import PNGSurface
    except (ImportError, OSError) as exc:
        raise ValueError(_CAIRO_GUIDANCE) from exc

    def fetch(url: str, resource_type: str) -> bytes:
        if resources is not None and url in resources:
            return resources[url]
        return _deny_fetch(url, resource_type)

    return PNGSurface.convert(
        bytestring=data,
        output_width=width,
        output_height=height,
        unsafe=False,
        url_fetcher=fetch if resources else _deny_fetch,
    )


def render_svg(
    data: bytes, *, embedded_images: dict[str, bytes] | None = None
) -> bytes:
    if len(data) > LIMITS.image_bytes:
        raise ValueError("SVG byte limit exceeded")
    parser = _SvgParser(forbid_dtd=True, forbid_entities=True, forbid_external=True)
    parser.feed(data)
    root = parser.close()
    if root.tag.split("}")[-1] != "svg":
        raise ValueError("Not an SVG document")
    # HTML parsers lowercase viewBox; Cairo must see the same viewport that
    # we use for dimension validation (otherwise it can silently paint blank).
    normalized_viewbox = "viewbox" in root.attrib
    if normalized_viewbox:
        root.set("viewBox", root.get("viewBox", root.attrib["viewbox"]))
        del root.attrib["viewbox"]
    elements = list(root.iter())
    if len(elements) > 20_000:
        raise ValueError("SVG element complexity limit exceeded")
    if any(el.tag.split("}")[-1].lower() == "foreignobject" for el in elements):
        from paper2html.ingestion._svg_browser import project_listing_downloads

        project_listing_downloads(root)
    references: set[str] = set()
    resources: dict[str, bytes] = {}
    foreign_objects = []
    embedded_bytes = embedded_pixels = 0
    ids = {el.get("id"): el for el in elements if el.get("id")}
    # Reject cycles and cap reference expansion, including references inherited
    # from descendants of a referenced group. CSS references are checked too.
    edges: dict[int, list[Any]] = {}
    for el in elements:
        tag = el.tag.split("}")[-1].lower()
        if tag in {"script", "animate", "set", "animatetransform"}:
            raise ValueError("Unsafe SVG element")
        if tag == "foreignobject":
            foreign_objects.append(el)
        local: set[str] = set()
        if tag == "style":
            _stylesheet(el.text or "", local)
        for key, value in el.attrib.items():
            name = key.split("}")[-1].lower()
            if name.startswith("on") or name == "base":
                raise ValueError("Active SVG content or base URL forbidden")
            if name == "href":
                if tag == "image":
                    # Never delegate arbitrary data URIs to CairoSVG: decode,
                    # validate, then rewrite to one exact inert raster URI.
                    from paper2html.ingestion._figure_fetcher import _data_uri
                    from paper2html.ingestion._safety import raster

                    raw = raster(_data_uri(value))
                    with Image.open(BytesIO(raw)) as image:
                        mime = Image.MIME[image.format]
                        pixels = image.width * image.height
                    canonical = f"data:{mime};base64," + base64.b64encode(raw).decode(
                        "ascii"
                    )
                    if canonical not in resources:
                        embedded_bytes += len(raw)
                        embedded_pixels += pixels
                        if (
                            embedded_bytes > LIMITS.image_bytes
                            or embedded_pixels > LIMITS.pixels
                        ):
                            raise ValueError(
                                "SVG embedded raster aggregate limit exceeded"
                            )
                        resources[canonical] = raw
                    el.set(key, canonical)
                else:
                    local.add(_fragment(value))
            elif name == "style":
                _declarations(value, local)
            elif name in _PRESENTATION:
                _tokens(tinycss2.parse_component_value_list(value), local)
        references.update(local)
        edges[id(el)] = list(el) + [ids[name] for name in local if name in ids]
    if references - ids.keys():
        raise ValueError("Unresolved SVG fragment reference")
    active: set[int] = set()
    sizes: dict[int, int] = {}

    def expansion(el: Any, depth: int = 0) -> int:
        key = id(el)
        if key in active or depth > 100:
            raise ValueError("SVG reference cycle/depth limit exceeded")
        if key in sizes:
            return sizes[key]
        active.add(key)
        size = 1 + sum(expansion(child, depth + 1) for child in edges[key])
        active.remove(key)
        if size > 100_000:
            raise ValueError("SVG reference expansion limit exceeded")
        sizes[key] = size
        return size

    expansion(root)
    viewbox = root.get("viewBox", "")
    fallback: tuple[float, float] | None = None
    if viewbox:
        values = [float(v) for v in re.split(r"[\s,]+", viewbox.strip())]
        if (
            len(values) != 4
            or not all(math.isfinite(v) for v in values)
            or min(values[2:]) <= 0
        ):
            raise ValueError("Invalid SVG viewBox")
        fallback = (values[2], values[3])
    dimensions = []
    for index, name in enumerate(("width", "height")):
        value = root.get(name)
        if value is not None and value.strip() == "100%":
            # No external viewport exists: only 100% has a defined policy,
            # mapping to the corresponding bounded viewBox extent.
            if fallback is None:
                raise ValueError("SVG 100% dimensions require a viewBox")
            dimensions.append(fallback[index])
        elif value is not None:
            dimensions.append(_dimension(value))
        elif fallback is not None:
            dimensions.append(fallback[index])
        else:
            raise ValueError("SVG requires dimensions or viewBox")
    width, height = dimensions
    if (
        not all(math.isfinite(v) and v >= 1 for v in dimensions)
        or max(dimensions) > LIMITS.dimension
        or width * height > LIMITS.pixels
    ):
        raise ValueError("SVG pixel limit exceeded")
    width, height = math.ceil(width), math.ceil(height)
    if width * height > LIMITS.pixels:
        raise ValueError("SVG pixel limit exceeded")
    if foreign_objects:
        from paper2html.ingestion._svg_browser import render_foreign_objects

        # The browser only sees a newly constructed, strictly allowlisted tree.
        # In particular never forward the original SVG or a Cairo resource URI.
        return render_foreign_objects(root, width, height, embedded_images)
    # PNGSurface.convert forwards url_fetcher to every Tree, including use/defs.
    # Unlike the svg2png convenience wrapper it exposes this explicit seam.
    if resources or normalized_viewbox:
        data = ElementTree.tostring(root, encoding="utf-8")
    if resources:
        return _convert(data, width, height, resources)
    return _convert(data, width, height)
