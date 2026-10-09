"""Small, offline Chromium renderer for *static* LaTeXML SVG foreignObject labels.

This is deliberately not a general SVG sanitizer. Only reconstructed geometry,
XHTML spans and presentation MathML reach the browser; unknown markup fails closed.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
import math
from html import escape
from io import BytesIO
import re
import warnings
from typing import Any

from PIL import Image, PngImagePlugin
import tinycss2

from paper2html.ingestion._limits import LIMITS

_BROWSER_GUIDANCE = (
    "LaTeXML SVG labels require Playwright Chromium: "
    "uv sync --group browser && uv run playwright install chromium"
)
_SVG = {
    "svg",
    "g",
    "path",
    "rect",
    "circle",
    "ellipse",
    "line",
    "polyline",
    "polygon",
    "foreignobject",
}
_MATH = {
    "math",
    "semantics",
    "mrow",
    "mi",
    "mn",
    "mo",
    "mtext",
    "msup",
    "msub",
    "msubsup",
    "mfrac",
    "msqrt",
    "mroot",
    "mover",
    "munder",
    "munderover",
    "mfenced",
    "mspace",
    "annotation",
}
_GLOBAL = {"id", "class"}
_SVG_ATTR = {
    "svg": {"width", "height", "viewbox", "version", "overflow"},
    "g": {
        "transform",
        "fill",
        "stroke",
        "stroke-width",
        "stroke-dasharray",
        "stroke-dashoffset",
        "stroke-linejoin",
        "opacity",
        "fill-opacity",
    },
    "path": {"d", "fill", "stroke", "stroke-width", "transform", "opacity"},
    "rect": {
        "x",
        "y",
        "width",
        "height",
        "rx",
        "ry",
        "fill",
        "stroke",
        "stroke-width",
        "transform",
    },
    "circle": {"cx", "cy", "r", "fill", "stroke", "stroke-width", "transform"},
    "ellipse": {"cx", "cy", "rx", "ry", "fill", "stroke", "stroke-width", "transform"},
    "line": {"x1", "y1", "x2", "y2", "fill", "stroke", "stroke-width", "transform"},
    "polyline": {"points", "fill", "stroke", "stroke-width", "transform"},
    "polygon": {"points", "fill", "stroke", "stroke-width", "transform"},
    "foreignobject": {"x", "y", "width", "height", "transform", "overflow"},
}
_MATH_ATTR = {
    "math": {"display", "alttext", "intent"},
    "mo": {"mathsize", "stretchy", "form", "mathcolor"},
    "mi": {"mathsize", "mathcolor"},
    "mn": {"mathsize", "mathcolor"},
    "mspace": {"width", "height", "depth"},
    "annotation": {"encoding"},
}
_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_LENGTH = re.compile(rf"(?:{_NUMBER})(?:px|pt|em|%)?\Z")
_NUMBERS = re.compile(rf"[\s,]*(?:{_NUMBER})(?:[\s,]+(?:{_NUMBER}))*[\s,]*\Z")
_PATH = re.compile(r"[MmZzLlHhVvCcSsQqTtAa0-9eE+.,\s-]+\Z")
_TRANSFORM = re.compile(
    rf"\s*(?:(?:matrix|translate|scale|rotate|skewX|skewY)\s*\(\s*(?:{_NUMBER})(?:[\s,]+(?:{_NUMBER}))*\s*\)\s*)+\Z"
)
_COLOR = re.compile(
    r"(?:#[0-9a-fA-F]{3,8}|none|black|white|transparent|currentColor)\Z"
)
_IDENT = re.compile(r"[a-zA-Z_][a-zA-Z0-9_.:-]{0,127}\Z")
_CLASS = re.compile(r"ltx_[a-zA-Z0-9_-]+(?:\s+ltx_[a-zA-Z0-9_-]+)*\Z")


def _styles(text: str, tag: str) -> str:
    if tag not in _SVG | {"span", "mi", "mn", "mo", "img"}:
        raise ValueError("MathML styles unsupported")
    if "\\" in text:
        raise ValueError("CSS escapes forbidden in LaTeXML SVG")
    allowed = (
        {"--ltx-fo-width", "--ltx-fo-height", "--ltx-fo-depth", "font-size"}
        if tag == "foreignobject"
        else {
            "font-size",
            "font-weight",
            "font-style",
            "color",
            "--ltx-fg-color",
            "width",
            "padding",
            "--ltx-enum-leftmargin",
            "list-style-type",
        }
        if tag == "span"
        else {"aspect-ratio"}
        if tag == "img"
        else {"--ltx-fg-color", "color"}
        if tag in {"mi", "mn", "mo"}
        else {
            "--ltx-fill-color",
            "--ltx-stroke-color",
            "fill",
            "stroke",
            "stroke-width",
        }
    )
    parts = []
    for item in tinycss2.parse_declaration_list(
        text, skip_comments=False, skip_whitespace=True
    ):
        if item.type != "declaration" or item.name not in allowed or item.important:
            raise ValueError("Unsupported LaTeXML SVG CSS")
        # Reject CSS escapes, comments, variables, functions, URLs, and token tricks.
        raw = tinycss2.serialize(item.value).strip()
        if not raw or any(
            t.type in {"function", "url", "error", "comment"} for t in item.value
        ):
            raise ValueError("Unsupported LaTeXML SVG CSS value")
        value = raw
        if item.name in {
            "fill",
            "stroke",
            "color",
            "--ltx-fill-color",
            "--ltx-stroke-color",
            "--ltx-fg-color",
        }:
            good = _COLOR.fullmatch(value)
        elif item.name == "font-weight":
            good = value in {"normal", "bold", "400", "700"}
        elif item.name == "font-style":
            good = value in {"normal", "italic"}
        elif item.name == "list-style-type":
            good = value == "none"
        elif item.name == "aspect-ratio":
            good = bool(re.fullmatch(r"[1-9][0-9]{0,4}/[1-9][0-9]{0,4}", value))
        elif item.name == "padding":
            lengths = value.split()
            good = 1 <= len(lengths) <= 4 and all(
                _bounded_length(v) and not v.startswith("-") for v in lengths
            )
        elif item.name in {"width", "--ltx-enum-leftmargin"}:
            good = _bounded_length(value) and not value.startswith("-")
        else:
            good = _bounded_length(value)
        if not good or "\\" in value:
            raise ValueError("Unsupported LaTeXML SVG CSS value")
        parts.append(f"{item.name}:{value}")
    return ";".join(parts)


def _bounded_length(value: str) -> bool:
    match = _LENGTH.fullmatch(value)
    if not match or len(value) > 32:
        return False
    number = re.match(_NUMBER, value)
    if number is None:
        return False
    magnitude = abs(float(number.group()))
    unit = value[number.end() :]
    # Relative units cannot be interpreted as pixels. Restrict their magnitude
    # independently; these are short diagram labels, not general HTML layout.
    if unit == "em":
        return math.isfinite(magnitude) and magnitude <= 64
    if unit == "%":
        return math.isfinite(magnitude) and magnitude <= 1000
    factor = 96 / 72 if unit == "pt" else 1
    return math.isfinite(magnitude) and magnitude * factor <= LIMITS.dimension


def _safe_attr(name: str, value: str, tag: str) -> str:
    if len(value) > 100_000 or ("\\" in value and name != "alttext"):
        raise ValueError("Unsupported LaTeXML SVG attribute")
    if name == "style":
        return _styles(value, tag)
    if name == "id" and _IDENT.fullmatch(value):
        return value
    if name == "class" and _CLASS.fullmatch(value):
        return value
    if tag in _SVG:
        if name not in _SVG_ATTR[tag]:
            raise ValueError("Unsupported LaTeXML SVG attribute")
        if name in {
            "width",
            "height",
            "x",
            "y",
            "rx",
            "ry",
            "cx",
            "cy",
            "r",
            "x1",
            "x2",
            "y1",
            "y2",
            "stroke-width",
            "stroke-dashoffset",
        }:
            good = _bounded_length(value)
        elif name in {"opacity", "fill-opacity"}:
            good = bool(re.fullmatch(_NUMBER, value)) and 0 <= float(value) <= 1
        elif name in {"viewbox", "points", "stroke-dasharray"}:
            good = (name == "stroke-dasharray" and value == "none") or (
                _NUMBERS.fullmatch(value) and len(value) <= 4000
            )
        elif name == "d":
            good = _PATH.fullmatch(value) and len(value) <= 200_000
        elif name == "transform":
            good = _TRANSFORM.fullmatch(value) and len(value) <= 4000
        elif name in {"fill", "stroke"}:
            good = _COLOR.fullmatch(value)
        elif name == "overflow":
            good = value in {"visible", "hidden"}
        elif name == "stroke-linejoin":
            good = value in {"round", "miter", "bevel"}
        else:
            good = value == "1.1"
    elif tag == "span":
        raise ValueError("Unsupported LaTeXML span attribute")
    else:
        if name not in _MATH_ATTR.get(tag, set()):
            raise ValueError("Unsupported MathML attribute")
        if name in {"mathsize", "width", "height", "depth"}:
            good = _bounded_length(value)
        elif name == "mathcolor":
            good = _COLOR.fullmatch(value)
        else:
            good = (
                (name == "display" and value == "inline")
                or (name == "intent" and value == ":literal")
                or (name == "encoding" and value == "application/x-tex")
                or (
                    name == "alttext"
                    and len(value) <= 500
                    and not any(c in value for c in "<>&")
                )
                or (
                    name in {"stretchy", "form"}
                    and value in {"true", "false", "prefix", "infix", "postfix"}
                )
            )
    if not good:
        raise ValueError("Unsupported LaTeXML SVG attribute value")
    return value


# The reflow branch removes the SVG, so it may run only when every discarded
# graphic is a verified inert background frame. Never infer this from color or
# path count alone: an otherwise matching diagram can contain meaningful ink.
_FRAME_TOKENS = re.compile(rf"[MLHVCZ]|{_NUMBER}")
_FRAME_PAINTS = ("#A6A6A6", "#FFFFFF", "#F2F2F2")


def _frame_box(d: str) -> tuple[float, float, float, float] | None:
    tokens = _FRAME_TOKENS.findall(d)
    if not tokens or "".join(tokens) != re.sub(r"[\s,]", "", d):
        return None
    segments = []
    pos = (0.0, 0.0)
    start = None
    i = 0
    try:
        while i < len(tokens):
            command = tokens[i]
            i += 1
            count = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "Z": 0}[command]
            nums = [float(v) for v in tokens[i : i + count]]
            if len(nums) != count or not all(math.isfinite(n) for n in nums):
                return None
            i += count
            if command == "M":
                if start is not None:
                    return None
                start = (nums[0], nums[1])
                pos = start
            elif command == "Z":
                if i != len(tokens) or start is None:
                    return None
                if pos != start:
                    segments.append(("L", pos, start, []))
                    pos = start
            else:
                if start is None:
                    return None
                end = (
                    (nums[0], nums[1])
                    if command == "L"
                    else (nums[0], pos[1])
                    if command == "H"
                    else (pos[0], nums[0])
                    if command == "V"
                    else (nums[4], nums[5])
                )
                segments.append((command, pos, end, nums))
                pos = end
    except (KeyError, ValueError, IndexError):
        return None
    if not tokens or tokens[-1] != "Z" or not 4 <= len(segments) <= 8:
        return None
    xs = [v[0] for _, start, end, _ in segments for v in (start, end)]
    ys = [v[1] for _, start, end, _ in segments for v in (start, end)]
    left, top, right, bottom = min(xs), min(ys), max(xs), max(ys)
    if right - left < 20 or bottom - top < 8:
        return None
    seen = set()
    eps = 0.025

    def close(a: float, b: float) -> bool:
        return abs(a - b) <= eps

    for command, start, end, nums in segments:
        if command == "C":
            p1, p2 = (nums[0], nums[1]), (nums[2], nums[3])
            if not all(
                left - eps <= x <= right + eps and top - eps <= y <= bottom + eps
                for x, y in (p1, p2)
            ):
                return None
            # One quarter-corner only: edge-tangent control points, no loops,
            # diagonal arcs, arbitrary gray curves or hidden inner subpaths.
            corners = [(x, y) for x in (left, right) for y in (top, bottom)]
            matching = [
                corner
                for corner in corners
                if (
                    close(start[0], corner[0])
                    and close(end[1], corner[1])
                    and close(p1[0], start[0])
                    and close(p2[1], end[1])
                )
                or (
                    close(start[1], corner[1])
                    and close(end[0], corner[0])
                    and close(p1[1], start[1])
                    and close(p2[0], end[0])
                )
            ]
            if (
                len(matching) != 1
                or any(
                    not min(a, b) - eps <= c <= max(a, b) + eps
                    for a, b, c in zip(start, end, p1)
                )
                or any(
                    not min(a, b) - eps <= c <= max(a, b) + eps
                    for a, b, c in zip(start, end, p2)
                )
                or max(abs(start[0] - end[0]), abs(start[1] - end[1])) > 10
            ):
                return None
            seen.add(matching[0])
        elif close(start[0], end[0]) and (
            close(start[0], left) or close(start[0], right)
        ):
            pass
        elif close(start[1], end[1]) and (
            close(start[1], top) or close(start[1], bottom)
        ):
            pass
        else:
            return None
        for x, y in (start, end) if command != "C" else ():
            if any(
                close(x, cx) and close(y, cy)
                for cx, cy in (
                    (left, top),
                    (left, bottom),
                    (right, top),
                    (right, bottom),
                )
            ):
                seen.add(
                    (
                        left if close(x, left) else right,
                        top if close(y, top) else bottom,
                    )
                )
    if len(seen) != 4:
        return None
    return left, top, right, bottom


def _gray_card_profile(root: Any) -> bool:
    """Positive, complete geometry classification before allowing svg.remove()."""
    try:
        width, height = float(root.get("width")), float(root.get("height"))
        viewbox = root.get("viewBox", root.get("viewbox"))
        if viewbox is not None:
            values = [float(value) for value in re.split(r"[\s,]+", viewbox.strip())]
            if len(values) != 4 or any(
                abs(a - b) > 0.03 for a, b in zip(values, (0, 0, width, height))
            ):
                return False
        if root.get("overflow") not in (None, "visible") or root.get("version") not in (
            None,
            "1.1",
        ):
            return False
        paths = []
        panels = []

        def walk(el: Any, paint: str | None = None, depth: int = 0) -> None:
            if depth > 100:
                raise ValueError("deep frame")
            tag = el.tag.split("}")[-1].lower()
            if tag == "foreignobject":
                panels.append(el)
                return  # XHTML label content is retained, not discarded.
            if tag not in {"svg", "g", "path"}:
                raise ValueError("unclassified SVG geometry")
            if (tag == "svg" and depth != 0) or (tag == "path" and list(el)):
                raise ValueError("unexpected frame structure")
            attrs = el.attrib
            if tag == "svg" and set(attrs) - {
                "id",
                "class",
                "width",
                "height",
                "viewBox",
                "viewbox",
                "version",
                "overflow",
            }:
                raise ValueError("unclassified root paint")
            style = dict(
                part.split(":", 1)
                for part in _styles(attrs.get("style", ""), tag).split(";")
                if part
            )
            if tag == "path":
                # Explicit stroke:none must override inherited source strokes.
                if (
                    (attrs.get("stroke") != "none" and style.get("stroke") != "none")
                    or ("stroke" in style and style["stroke"] != "none")
                    or ("stroke" in attrs and attrs["stroke"] != "none")
                    or attrs.get("transform")
                    or set(style) - {"stroke"}
                    or set(attrs) - {"id", "class", "d", "style", "stroke"}
                ):
                    raise ValueError("painted or transformed path")
                paths.append((paint, _frame_box(attrs.get("d", ""))))
            else:
                if tag == "g" and (
                    set(attrs)
                    - {
                        "id",
                        "class",
                        "fill",
                        "stroke",
                        "stroke-width",
                        "fill-opacity",
                        "transform",
                        "style",
                    }
                    or attrs.get("fill-opacity") not in (None, "1", "1.0")
                ):
                    raise ValueError("unclassified group attributes")
                # The source outer group inherits black stroke; every path
                # overrides it with stroke:none. No other painted group qualifies.
                if tag == "g" and (
                    (
                        attrs.get("stroke") not in (None, "none")
                        and not (
                            depth == 1
                            and attrs.get("stroke") == "#000000"
                            and attrs.get("stroke-width") == "0.4pt"
                        )
                    )
                    or (
                        attrs.get("stroke-width") not in (None, "0")
                        and not (
                            depth == 1
                            and attrs.get("stroke-width") == "0.4pt"
                            and attrs.get("stroke") == "#000000"
                        )
                    )
                ):
                    raise ValueError("non-inert group stroke")
                if (
                    tag == "g"
                    and depth > 1
                    and attrs.get("transform")
                    and not (
                        list(el)
                        and all(
                            child.tag.split("}")[-1].lower() == "foreignobject"
                            for child in el
                        )
                    )
                ):
                    raise ValueError("transformed frame")
                if tag == "svg" and attrs.get("transform"):
                    raise ValueError("transformed SVG")
                if (
                    tag == "g"
                    and depth == 1
                    and attrs.get("transform")
                    and not re.fullmatch(
                        rf"translate\(0,{_NUMBER}\) matrix\(1 0 0 -1 0 0\)",
                        attrs["transform"],
                    )
                ):
                    raise ValueError("unknown group transform")
                if (
                    tag == "g"
                    and depth == 1
                    and attrs.get("transform")
                    and not math.isclose(
                        float(attrs["transform"].split(",", 1)[1].split(")", 1)[0]),
                        height,
                        abs_tol=0.03,
                    )
                ):
                    raise ValueError("unmatched flip")
                if tag == "g" and (
                    set(style) - {"--ltx-fill-color", "--ltx-stroke-color"}
                    or style.get("--ltx-fill-color", attrs.get("fill"))
                    != attrs.get("fill")
                    or (
                        "--ltx-stroke-color" in style
                        and style["--ltx-stroke-color"] != attrs.get("stroke")
                    )
                ):
                    raise ValueError("unclassified group style")
                paint = attrs.get("fill", paint)
                for child in el:
                    walk(child, paint, depth + 1)

        walk(root)

        def complete_label(panel: Any) -> bool:
            # The browser moves only the first content node before svg.remove().
            # Require it to be the sole visible subtree of this foreignObject;
            # otherwise a sibling header/second content/direct text would vanish.
            container_children = list(panel)
            if len(container_children) != 1 or (panel.text or "").strip():
                return False
            container = container_children[0]
            content_children = list(container)
            if (
                container.tag.split("}")[-1].lower() != "span"
                or "ltx_foreignobject_container"
                not in container.get("class", "").split()
                or len(content_children) != 1
                or (container.text or "").strip()
                or (container.tail or "").strip()
            ):
                return False
            content = content_children[0]
            return (
                content.tag.split("}")[-1].lower() == "span"
                and "ltx_foreignobject_content" in content.get("class", "").split()
                and not (content.tail or "").strip()
                and sum(
                    "ltx_foreignobject_content" in el.get("class", "").split()
                    for el in panel.iter()
                )
                == 1
                and any(
                    "ltx_minipage" in el.get("class", "").split()
                    for el in content.iter()
                )
            )

        if (
            not 1 <= len(panels) <= 2
            or not all(complete_label(panel) for panel in panels)
            or len(paths) not in (2, 3)
        ):
            return False
        if any(box is None for _, box in paths):
            return False
        colors = [color for color, _ in paths]
        if colors not in (
            ["#A6A6A6", "#FFFFFF"],
            ["#A6A6A6", "#F9F9F9"],
            list(_FRAME_PAINTS),
        ):
            return False
        outer = paths[0][1]
        if any(abs(a - b) > 0.03 for a, b in zip(outer, (0, 0, width, height))):
            return False
        for index, (_, (left, top, right, bottom)) in enumerate(paths[1:], 1):
            if not (
                0 < left <= 2
                and 0 < top <= height - 8
                and width - 2 <= right < width
                and (index != len(paths) - 1 or height - 2 <= bottom <= height - 0.1)
            ):
                return False
        if len(paths) == 3:
            upper, lower = paths[1][1], paths[2][1]
            if not (
                abs(upper[0] - lower[0]) <= 0.03
                and abs(upper[2] - lower[2]) <= 0.03
                and 0.1 <= lower[1] - upper[3] <= 2
                and 8 <= lower[3] - lower[1] <= 30
            ):
                return False
        return True
    except (ValueError, TypeError, OverflowError, KeyError):
        return False


def project_listing_downloads(root: Any) -> None:
    """Project one inert LaTeXML listing affordance, never a general hyperlink.

    The discarded data payload is bounded and decoded only to validate its type;
    it is never fetched, executed, saved, or forwarded to Chromium.
    """

    def walk(
        el: Any, in_label: bool = False, parent: Any = None, depth: int = 0
    ) -> None:
        if depth > 100:
            raise ValueError("SVG label depth limit exceeded")
        tag = el.tag.split("}")[-1].lower()
        in_label |= tag == "foreignobject"
        if tag == "a" and in_label:
            if (
                parent is None
                or parent.tag.split("}")[-1].lower() != "span"
                or "ltx_listing_data" not in parent.get("class", "").split()
                or set(el.attrib) != {"download", "href"}
                or el.get("download") != ""
                or list(el)
            ):
                raise ValueError("Unsupported LaTeXML listing download")
            href = el.get("href", "")
            prefix = "data:text/plain;base64,"
            if not href.startswith(prefix) or len(href) > LIMITS.image_bytes:
                raise ValueError("Unsupported LaTeXML listing download payload")
            try:
                base64.b64decode(href[len(prefix) :], validate=True).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError) as exc:
                raise ValueError("Invalid LaTeXML listing download payload") from exc
            el.tag = "span"
            el.attrib.clear()
        for child in el:
            walk(child, in_label, el, depth + 1)

    walk(root)


def _serialize(
    el: Any,
    parent: str | None = None,
    image_assets: dict[str, bytes] | None = None,
    image_uris: dict[str, str] | None = None,
) -> tuple[str, bool]:
    image_uris = {} if image_uris is None else image_uris
    tag = el.tag.split("}")[-1].lower()
    if tag == "img":
        return _label_image(el, parent, image_assets or {}, image_uris), True
    if tag not in _SVG | _MATH | {"span"}:
        raise ValueError("Unsupported LaTeXML SVG element")
    if parent is None and tag != "svg":
        raise ValueError("Invalid SVG root")
    if parent is not None and tag == "svg":
        raise ValueError("Nested SVG unsupported")
    if parent == "foreignobject" and tag != "span":
        raise ValueError("foreignObject requires a static XHTML span")
    if parent == "span" and tag not in {"span", "math"}:
        raise ValueError("Unsupported XHTML label content")
    if parent in _MATH and tag not in _MATH:
        raise ValueError("Unsupported MathML content")
    if parent in _SVG - {"foreignobject"} and tag not in _SVG:
        raise ValueError("Unsupported SVG child")
    if tag == "foreignobject" and parent not in {"g", "svg"}:
        raise ValueError("Unsupported foreignObject placement")
    if tag == "annotation":
        # Validate metadata even though it is not sent to the browser.
        if parent != "semantics" or list(el) or set(el.attrib) != {"encoding"}:
            raise ValueError("Unsupported MathML annotation")
        _safe_attr("encoding", el.attrib["encoding"], tag)
        return "", False
    if tag == "math" and parent != "span":
        raise ValueError("MathML outside label")
    if tag == "span" and parent not in {"span", "foreignobject"}:
        raise ValueError("XHTML span outside label")
    attrs = []
    for key, value in el.attrib.items():
        # An SVG parsed through HTML may use viewbox/foreignobject spelling.
        name = key.split("}")[-1]
        name = "viewbox" if name == "viewBox" else name
        if ":" in name or key.startswith("{"):
            raise ValueError("Namespaced SVG attribute forbidden")
        if (
            name not in _GLOBAL
            and name != "style"
            and name
            not in (
                _SVG_ATTR.get(tag, set()) if tag in _SVG else _MATH_ATTR.get(tag, set())
            )
        ):
            raise ValueError("Unsupported LaTeXML SVG attribute")
        attrs.append(
            f' {"viewBox" if name == "viewbox" else name}="{escape(_safe_attr(name, value, tag), quote=True)}"'
        )
    # LaTeXML may put its paint colors only in custom properties. Promote
    # those already-sanitized hex colors to SVG presentation attributes; an
    # explicit SVG attribute or ordinary fill/stroke declaration takes priority.
    if tag in _SVG and "style" in el.attrib:
        declarations = dict(
            part.split(":", 1)
            for part in _styles(el.attrib["style"], tag).split(";")
            if part
        )
        for paint in ("fill", "stroke"):
            color = declarations.get(f"--ltx-{paint}-color")
            if color and paint not in el.attrib and paint not in declarations:
                attrs.append(f' {paint}="{escape(color, quote=True)}"')
    if tag == "svg":
        attrs.append(' xmlns="http://www.w3.org/2000/svg"')
    if tag == "span" and parent == "foreignobject":
        attrs.append(' xmlns="http://www.w3.org/1999/xhtml"')
    if tag == "math":
        attrs.append(' xmlns="http://www.w3.org/1998/Math/MathML"')
    text = el.text or ""
    if tag in _SVG and text.strip():
        raise ValueError("Text outside LaTeXML label")
    children = []
    # Preserve literal spaces/tabs, including whitespace-only text and tails.
    # Only an exact terminal newline from LaTeXML pretty-printing is discarded.
    listing_line = tag == "span" and "ltx_listingline" in el.get("class", "").split()
    if listing_line and not len(el) and text == "\n":
        text = ""
    visible = bool(text.strip()) if tag in _MATH | {"span"} else False
    for child in el:
        html, child_visible = _serialize(child, tag, image_assets, image_uris)
        children.append(html)
        visible |= child_visible
        tail = child.tail or ""
        if listing_line and child is el[-1] and tail == "\n":
            tail = ""
        if tag in _SVG and tail.strip():
            raise ValueError("Text outside LaTeXML label")
        children.append(escape(tail))
        if tag in _MATH | {"span"}:
            visible |= bool(tail.strip())
    if tag == "foreignobject" and not visible:
        raise ValueError("Empty foreignObject label")
    return f"<{tag}{''.join(attrs)}>{escape(text)}{''.join(children)}</{tag}>", visible


def _label_image(
    el: Any, parent: str | None, assets: dict[str, bytes], image_uris: dict[str, str]
) -> str:
    """Only supplied validated pixels reach Chromium, never the source URL."""
    from paper2html.ingestion._safety import raster
    from paper2html.ingestion._figure_fetcher import _data_uri

    allowed = {"src", "alt", "width", "height", "style", "id", "class"}
    if (
        parent != "span"
        or set(el.attrib) - allowed
        or list(el)
        or (el.text or "").strip()
    ):
        raise ValueError("Unsupported LaTeXML label image")
    source = el.get("src", "")
    if source not in image_uris:
        raw = assets.get(source)
        if raw is None and source.lower().startswith("data:"):
            raw = _data_uri(source)
        if raw is None:
            raise ValueError("LaTeXML label image missing supplied raster bytes")
        raw = raster(raw)
        with Image.open(BytesIO(raw)) as image:
            mime = Image.MIME[image.format]
        image_uris[source] = f"data:{mime};base64," + base64.b64encode(raw).decode(
            "ascii"
        )
    attrs = []
    for name, value in el.attrib.items():
        if name == "src":
            continue
        if name in {"width", "height"}:
            good = (
                bool(re.fullmatch(_NUMBER, value))
                and 1 <= float(value) <= LIMITS.dimension
            )
            if not good:
                raise ValueError("Invalid LaTeXML label image dimensions")
        elif name == "alt":
            if len(value) > 500:
                raise ValueError("LaTeXML label image alt text limit exceeded")
        else:
            value = _safe_attr(name, value, "img")
        attrs.append(f' {name}="{escape(value, quote=True)}"')
    return f'<img src="{image_uris[source]}"{"".join(attrs)}>'


async def _screenshot(markup: str, width: int, height: int) -> bytes:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise ValueError(_BROWSER_GUIDANCE) from exc
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(timeout=10_000)
            try:
                context = await browser.new_context(
                    viewport={"width": width, "height": height},
                    device_scale_factor=1,
                    java_script_enabled=False,
                    offline=True,
                    service_workers="block",
                    accept_downloads=False,
                )
                try:
                    await context.route("**/*", lambda route: route.abort())
                    page = await context.new_page()
                    page.set_default_timeout(10_000)
                    await page.set_content(markup, wait_until="load", timeout=10_000)
                    await page.wait_for_function(
                        "Array.from(document.images).every(i => i.complete && i.naturalWidth > 0)"
                    )
                    # Fit static minipage panels to their already bounded source boxes.
                    # This is renderer-owned measurement, not publisher JavaScript.
                    reflowed = await page.evaluate("""() => {
                        let reflowed = false;
                        const measure = content => {
                            const origin = content.getBoundingClientRect();
                            const bounds = {left: origin.left, top: origin.top, right: origin.left, bottom: origin.top};
                            const include = rect => {
                                if (!rect.width || !rect.height) return;
                                bounds.left = Math.min(bounds.left, rect.left);
                                bounds.top = Math.min(bounds.top, rect.top);
                                bounds.right = Math.max(bounds.right, rect.right);
                                bounds.bottom = Math.max(bounds.bottom, rect.bottom);
                            };
                            const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT);
                            while (walker.nextNode()) {
                                const node = walker.currentNode;
                                if (!node.textContent.trim()) continue;
                                const range = document.createRange();
                                range.selectNodeContents(node);
                                for (const rect of range.getClientRects()) include(rect);
                            }
                            for (const img of content.querySelectorAll('img')) include(img.getBoundingClientRect());
                            return bounds;
                        };
                        const svg = document.querySelector('svg');
                        const panels = Array.from(document.querySelectorAll('foreignObject'));
                        for (const fo of panels) {
                            const content = fo.querySelector('.ltx_foreignobject_content');
                            if (!content || !content.querySelector('.ltx_minipage')) continue;
                            const foBox = fo.getBoundingClientRect(), canvas = svg.getBoundingClientRect();
                            const box = {left: Math.max(foBox.left, canvas.left), top: Math.max(foBox.top, canvas.top),
                                right: Math.min(foBox.right, canvas.right), bottom: Math.min(foBox.bottom, canvas.bottom)};
                            const origin = content.getBoundingClientRect(), bounds = measure(content);
                            const scale = Math.min(1, (box.right - box.left - 1) / Math.max(1, bounds.right - bounds.left),
                                (box.bottom - box.top - 1) / Math.max(1, bounds.bottom - bounds.top));
                            if (!Number.isFinite(scale) || scale <= 0) throw new Error('Invalid panel fit');
                            // Some LaTeXML listings have a 14px box around thousands
                            // of characters. Never "preserve" them as unreadable dots.
                            // Reflow only one/two minipages inside a narrowly matched gray
                            // frame profile, never arbitrary positioned-label diagrams.
                            // Marker is renderer-owned, emitted only after complete
                            // source geometry has been classified in Python.
                            const grayCard = svg.hasAttribute('data-paper2html-gray-card') &&
                                panels.length <= 2 && panels.every(p => p.querySelector('.ltx_minipage'));
                            if (scale < .65 && grayCard) {
                                const card = document.createElement('div');
                                card.className = 'reflowed-panel';
                                card.style.width = `${svg.getBoundingClientRect().width}px`;
                                const notice = document.createElement('div');
                                notice.className = 'source-panel-notice';
                                notice.textContent = 'Source panel reflowed for readability';
                                card.append(notice);
                                for (const panel of panels) {
                                    const label = panel.querySelector('.ltx_foreignobject_content');
                                    label.style.transform = '';
                                    label.style.transformOrigin = '';
                                    card.append(label);
                                }
                                svg.remove();
                                document.body.append(card);
                                const used = measure(card), initial = card.getBoundingClientRect();
                                if (used.right > initial.right) {
                                    card.style.width = `${Math.ceil(used.right - initial.left + 16)}px`;
                                }
                                reflowed = true;
                                break;
                            } else {
                                if (scale < .65) throw new Error('Panel too small for readable content');
                                const x = box.left + .5 - origin.left - scale * (bounds.left - origin.left);
                                const y = box.top + .5 - origin.top - scale * (bounds.top - origin.top);
                                content.style.transformOrigin = 'top left';
                                content.style.transform = `translate(${x}px,${y}px) scale(${scale})`;
                                const fitted = measure(content);
                                if (fitted.left < box.left - .75 || fitted.top < box.top - .75 ||
                                        fitted.right > box.right + .75 || fitted.bottom > box.bottom + .75) {
                                    throw new Error('Panel text/image containment failed');
                                }
                            }
                        }
                        if (reflowed) {
                            const card = document.querySelector('.reflowed-panel');
                            const box = card.getBoundingClientRect(), bounds = measure(card);
                            if (bounds.left < box.left - .75 || bounds.top < box.top - .75 ||
                                    bounds.right > box.right + .75 || bounds.bottom > box.bottom + .75) {
                                throw new Error('Reflowed panel text/image containment failed');
                            }
                        }
                        return reflowed;
                    }""")
                    if reflowed:
                        size = await page.locator(".reflowed-panel").bounding_box()
                        target_height = math.ceil(size["height"]) if size else 0
                        target_width = math.ceil(size["width"]) if size else 0
                        if (
                            min(target_width, target_height) < 1
                            or max(target_width, target_height) > LIMITS.dimension
                            or target_height * target_width > LIMITS.pixels
                        ):
                            raise ValueError("Reflowed panel pixel limit exceeded")
                        await page.set_viewport_size(
                            {"width": target_width, "height": target_height}
                        )
                        warnings.warn(
                            "Source LaTeXML panel reflowed for readability; original HTML label geometry is too small",
                            stacklevel=2,
                        )
                        raw = await page.locator(".reflowed-panel").screenshot(
                            timeout=10_000, animations="disabled"
                        )
                        # Only this renderer-owned branch can change logical height.
                        output = BytesIO()
                        info = PngImagePlugin.PngInfo()
                        info.add_text("paper2html-reflow", "1")
                        with Image.open(BytesIO(raw)) as image:
                            image.save(output, format="PNG", pnginfo=info)
                        return output.getvalue()
                    return await page.locator("svg").screenshot(
                        timeout=10_000, animations="disabled"
                    )
                finally:
                    await context.close()
            finally:
                await browser.close()
    except Exception as exc:
        raise ValueError(
            f"LaTeXML SVG Chromium rendering failed ({_BROWSER_GUIDANCE}): {exc}"
        ) from exc


def render_foreign_objects(
    root: Any, width: int, height: int, image_assets: dict[str, bytes] | None = None
) -> bytes:
    # Weight encoded bytes and decoded pixels by OCCURRENCE, not unique URL.
    # Validation/encoding is cached, but each emitted URI still consumes budget.
    used = Counter(
        el.get("src", "")
        for el in root.iter()
        if el.tag.split("}")[-1].lower() == "img"
    )
    from paper2html.ingestion._safety import raster
    from paper2html.ingestion._figure_fetcher import _data_uri

    prepared = {}
    image_uris = {}
    pixels = total_bytes = 0
    for key, occurrences in used.items():
        raw = (image_assets or {}).get(key)
        if raw is None and key.lower().startswith("data:"):
            raw = _data_uri(key)
        if raw is None:
            raise ValueError("LaTeXML label image missing supplied raster bytes")
        raw = raster(raw)
        # Check base64 expansion BEFORE allocating it, including every copy.
        total_bytes += occurrences * (4 * ((len(raw) + 2) // 3) + 64)
        with Image.open(BytesIO(raw)) as image:
            pixels += occurrences * image.width * image.height
            mime = Image.MIME[image.format]
        if total_bytes > LIMITS.image_bytes or pixels > LIMITS.pixels:
            raise ValueError("LaTeXML label image aggregate limit exceeded")
        prepared[key] = raw
        image_uris[key] = f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")
    svg, _ = _serialize(root, image_assets=prepared, image_uris=image_uris)
    if _gray_card_profile(root):
        # No untrusted source attribute can mint this marker (_serialize rejects it).
        svg = svg.replace("<svg", '<svg data-paper2html-gray-card="1"', 1)
    if len(svg.encode()) > LIMITS.image_bytes:
        raise ValueError("LaTeXML serialized SVG byte limit exceeded")
    # Never use publisher CSS. The validated LaTeXML width is inherited by the
    # label container; the foreignObject's dimensions and transforms position it.
    markup = (
        '<!doctype html><html><head><meta http-equiv="Content-Security-Policy" '
        "content=\"default-src 'none'; img-src data:; font-src 'none'; connect-src 'none'; "
        "style-src 'unsafe-inline'; script-src 'none'; base-uri 'none'; form-action 'none'\">"
        f"<style>html,body{{margin:0;padding:0;background:white}}svg{{display:block;width:{width}px;height:{height}px}}"
        "foreignObject{font-family:'Times New Roman',serif;color:#000}"
        ".ltx_foreignobject_container{display:flex;align-items:flex-end;height:100%;max-width:var(--ltx-fo-width);white-space:nowrap}"
        ".ltx_foreignobject_container:has(>.ltx_foreignobject_content>math:only-child){align-items:center}"
        ".ltx_foreignobject_content{display:block;font-size:1em;white-space:nowrap}"
        "foreignObject[style*='font-size'] .ltx_foreignobject_content{line-height:1}\n"
        "foreignObject[style*='font-size'] .ltx_foreignobject_content *{line-height:1.2}"
        ".ltx_font_bold{font-weight:bold}.ltx_font_italic{font-style:italic}"
        ".ltx_font_typewriter{font-family:monospace}"
        ".ltx_foreignobject_container:has(.ltx_minipage){align-items:flex-start}"
        ".ltx_minipage{display:inline-block;white-space:normal;vertical-align:top}"
        ".ltx_p,.ltx_para{display:block}.ltx_p{margin:0 0 .3em}"
        ".ltx_listing{display:block;white-space:normal;font-size:.85em}"
        ".ltx_listingline{display:block;white-space:pre}.ltx_listing_data{display:none}"
        ".ltx_tabular{display:inline-table;border-collapse:collapse;white-space:normal}"
        ".ltx_tbody{display:table-row-group}.ltx_thead{display:table-header-group}"
        ".ltx_tr{display:table-row}.ltx_td,.ltx_th{display:table-cell}"
        ".ltx_th{font-weight:bold}.ltx_border_t{border-top:1px solid black}"
        ".ltx_border_bb{border-bottom:2px solid black}.ltx_border_tt{border-top:2px solid black}"
        ".ltx_align_left{text-align:left}.ltx_align_middle{vertical-align:middle}"
        ".ltx_align_bottom{vertical-align:bottom}.ltx_align_top{vertical-align:top}"
        ".ltx_itemize{display:block;padding-left:1em}.ltx_item{display:block}"
        ".ltx_tag_item{float:left;margin-left:-1em}.ltx_inline-block{display:inline-block}"
        ".ltx_text[style*='--ltx-fg-color']{color:var(--ltx-fg-color)}"
        ".reflowed-panel{box-sizing:border-box;padding:12px 15px;border:1px solid #aaa;border-radius:7px;background:white;font-family:'Times New Roman',serif;font-size:14px}"
        ".source-panel-notice{font:11px sans-serif;color:#555;margin-bottom:10px}"
        ".reflowed-panel .ltx_foreignobject_content{white-space:normal;line-height:1.2}"
        ".reflowed-panel .ltx_minipage{width:100%!important}"
        ".reflowed-panel .ltx_listingline{white-space:pre-wrap;overflow-wrap:anywhere}"
        "</style></head><body>" + svg + "</body></html>"
    )
    if len(markup.encode()) > LIMITS.image_bytes:
        raise ValueError("LaTeXML browser markup byte limit exceeded")
    # Integer dimensions are already bounded by the shared SVG limits.
    if width * height > LIMITS.pixels:
        raise ValueError("SVG pixel limit exceeded")
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        png = asyncio.run(_screenshot(markup, width, height))
    else:
        # Keep the browser in its own event loop. Per-operation Playwright
        # timeouts apply; this is not a hard wall-clock/CPU sandbox.
        with ThreadPoolExecutor(max_workers=1) as pool:
            png = pool.submit(
                lambda: asyncio.run(_screenshot(markup, width, height))
            ).result()
    if not png.startswith(b"\x89PNG\r\n\x1a\n") or len(png) > LIMITS.image_bytes:
        raise ValueError("Invalid LaTeXML SVG screenshot")
    try:
        with Image.open(BytesIO(png)) as image:
            if (
                image.format != "PNG"
                or (
                    image.size != (width, height)
                    and image.info.get("paper2html-reflow") != "1"
                )
                or min(image.size) < 1
                or max(image.size) > LIMITS.dimension
                or image.width * image.height > LIMITS.pixels
            ):
                raise ValueError("Invalid LaTeXML SVG screenshot dimensions")
            image.verify()
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError("Invalid LaTeXML SVG screenshot") from exc
    return png
