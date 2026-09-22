"""Allowlist reconstruction for untrusted document markup and raster validation."""

from io import BytesIO
import warnings

from bs4 import BeautifulSoup, Comment, NavigableString, Tag
from markupsafe import Markup, escape
from PIL import Image

_MATH = set(
    "math semantics mrow mi mn mo ms mtext mspace mfrac msqrt mroot msub msup msubsup munder mover munderover mmultiscripts mprescripts none mtable mtr mtd menclose mphantom mpadded mstyle".split()
)
_TABLE = set(
    "figure figcaption table caption thead tbody tfoot tr td th colgroup col".split()
)
_INLINE = set("span b strong i em sub sup br".split())
_MATH_ATTRS = set(
    "display mathvariant mathsize mathcolor mathbackground stretchy symmetric largeop movablelimits fence separator accent accentunder form lspace rspace linethickness bevelled notation columnalign rowalign columnspacing rowspacing columnlines rowlines equalrows equalcolumns width height depth voffset scriptlevel displaystyle".split()
)


# Bound parsing allocations and recursive reconstruction independently. Typical
# corpus math depth is ~11; 64 leaves ample room for legitimate nested formulas.
MAX_MARKUP_BYTES = 2 * 1024 * 1024
MAX_MARKUP_NODES = 20_000
MAX_MARKUP_DEPTH = 64


def _markup_limit(reason: str) -> Markup:
    warnings.warn(
        f"Unsafe or oversized markup omitted: {reason} limit exceeded",
        UserWarning,
        stacklevel=3,
    )
    return Markup("")


def sanitize_markup(source: str, *, table: bool = False) -> Markup:
    """Rebuild only known tags; unknown subtrees (including annotations) vanish.

    MathML text integration points permit text only, never nested HTML. No URL,
    style, event, namespace, or document-ID attributes survive reconstruction.
    """
    if (
        len(source) > MAX_MARKUP_BYTES
        or len(source.encode("utf-8", errors="replace")) > MAX_MARKUP_BYTES
    ):
        return _markup_limit("source byte")
    soup = BeautifulSoup(source, "html.parser")
    # Iterative preflight includes even disallowed subtrees. Reject the fragment
    # once, before recursive emission, rather than leaking RecursionError or
    # producing many warnings for the same hostile tree.
    stack = [iter(soup.children)]
    nodes = 0
    while stack:
        node = next(stack[-1], None)
        if node is None:
            stack.pop()
            continue
        nodes += 1
        if nodes > MAX_MARKUP_NODES:
            return _markup_limit("node count")
        if isinstance(node, Tag):
            if len(stack) > MAX_MARKUP_DEPTH:
                return _markup_limit("depth")
            stack.append(iter(node.children))

    def emit(node: object, in_math: bool = False) -> str:
        if isinstance(node, Comment):
            return ""
        if isinstance(node, NavigableString):
            return str(escape(str(node)))
        if not isinstance(node, Tag):
            return ""
        name = node.name.lower()
        allowed = _MATH if in_math or not table else _TABLE | _INLINE | {"math"}
        if name not in allowed:
            return ""
        math = in_math or name == "math" or not table
        attrs = []
        if name == "figure":
            attrs.append(' class="paper-table"')
        for key, value in node.attrs.items():
            if not isinstance(value, str):
                continue
            if math and key in _MATH_ATTRS:
                attrs.append(f' {key}="{escape(value)}"')
            elif name in {"td", "th", "col", "colgroup"} and key in {
                "rowspan",
                "colspan",
                "span",
            }:
                if (
                    len(value) <= 4
                    and value.isascii()
                    and value.isdigit()
                    and 0 <= int(value) <= 1000
                ):
                    attrs.append(f' {key}="{value}"')
            elif (
                name == "th"
                and key == "scope"
                and value in {"row", "col", "rowgroup", "colgroup"}
            ):
                attrs.append(f' scope="{value}"')
        children = node.children
        if math and name in {"mi", "mo", "mn", "ms", "mtext"}:
            children = (c for c in children if isinstance(c, NavigableString))
        body = "".join(emit(c, math) for c in children)
        opening = f"<{name}{''.join(attrs)}>"
        return opening if name in {"br", "col"} else f"{opening}{body}</{name}>"

    return Markup("".join(emit(c) for c in soup.children))


def raster_mime(data: bytes) -> str:
    """Validate bounded raster bytes and derive MIME, ignoring claimed metadata."""
    from paper2html.ingestion._safety import raster

    raster(data)
    with Image.open(BytesIO(data)) as image:
        return {
            "PNG": "image/png",
            "JPEG": "image/jpeg",
            "GIF": "image/gif",
            "WEBP": "image/webp",
        }[image.format]
