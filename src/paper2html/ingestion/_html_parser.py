"""HTML page fetching and single-pass section/figure parsing."""

from __future__ import annotations

import logging
import copy
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from paper2html.ingestion._client import get_client
from paper2html.ingestion._types import FigureRef, MathMLNode, Section

logger = logging.getLogger(__name__)


def fetch_html_page(arxiv_id: str) -> tuple[str | None, str | None]:
    """Fetch the native HTML version of an arXiv paper.

    Returns (html_text, final_url) on success, or (None, None) when the
    HTML version does not exist (HTTP 404).  All other HTTP errors are
    re-raised.
    """
    url = f"https://arxiv.org/html/{arxiv_id}"
    logger.debug("Fetching HTML page %s", url)
    response = get_client().get(url)
    if response.status_code == 404:
        logger.info("No HTML version for %s (404)", arxiv_id)
        return None, None
    response.raise_for_status()
    return response.text, str(response.url)


def parse_sections(
    html_text: str,
    page_url: str,
) -> tuple[list[tuple[Section, list[FigureRef]]], list[FigureRef]]:
    """Parse *html_text* into sections and figure references in a single pass.

    Returns:
        (section_tuples, orphan_refs) where *section_tuples* is a list of
        (Section, list[FigureRef]) pairs and *orphan_refs* is a list of
        FigureRef instances for figures not contained in any section.
    """
    soup = BeautifulSoup(html_text, "html.parser")

    # Resolve relative figure srcs exactly as a browser would: against the
    # document's own URL (post-redirect), honoring a <base href> if present.
    # arXiv serves the doc at the unversioned path (e.g.
    # ".../html/1706.03762") with version-prefixed sibling srcs (e.g.
    # "1706.03762v7/Figures/x.png"), so RFC 3986 resolution drops the doc's
    # last path segment — forcing a trailing slash would mis-resolve to a
    # nonexistent ".../html/1706.03762/1706.03762v7/..." (HTTP 404).
    base_url = page_url
    base_el = soup.find("base", href=True)
    if base_el and isinstance(base_el, Tag):
        base_url = urljoin(page_url, str(base_el["href"]))

    section_elements = soup.find_all("section") or [soup.find("body") or soup]

    # Track figure elements that have been assigned to a section.
    consumed_figures: set[int] = set()

    section_tuples: list[tuple[Section, list[FigureRef]]] = []

    for sec_el in section_elements:
        if not isinstance(sec_el, Tag):
            continue

        # Heading
        h_tag = next(
            (
                h
                for h in sec_el.find_all(re.compile(r"^h[1-6]$"))
                if sec_el.name != "section" or h.find_parent("section") is sec_el
            ),
            None,
        )
        if h_tag and isinstance(h_tag, Tag):
            heading_level = int(h_tag.name[1])
            heading_text = h_tag.get_text(strip=True)
        else:
            heading_level = 0
            heading_text = ""

        # MathML nodes at section level (not inside figure captions)
        mathml_nodes = [
            MathMLNode(raw_mathml=str(m))
            for m in sec_el.find_all("math")
            if isinstance(m, Tag)
            and m.find_parent("figure") is None
            and (sec_el.name != "section" or m.find_parent("section") is sec_el)
        ]

        # Equation refs: IDs on math/equation elements
        equation_refs: list[str] = []
        for el in sec_el.find_all(True):
            if not isinstance(el, Tag):
                continue
            if sec_el.name == "section" and el.find_parent("section") is not sec_el:
                continue
            if el.name in ("math", "equation") and el.get("id"):
                equation_refs.append(str(el["id"]))

        # Figure refs
        fig_refs: list[FigureRef] = []
        for fig_el in sec_el.find_all("figure"):
            if not isinstance(fig_el, Tag):
                continue
            fig_id = id(fig_el)
            if fig_el.find_parent("figure") is not None or (
                sec_el.name == "section" and fig_el.find_parent("section") is not sec_el
            ):
                continue
            consumed_figures.add(fig_id)

            figcaption = fig_el.find("figcaption")
            caption = (
                figcaption.get_text(strip=True)
                if figcaption and isinstance(figcaption, Tag)
                else ""
            )
            caption_mathml: list[str] = []
            if figcaption and isinstance(figcaption, Tag):
                caption_mathml = [
                    str(m) for m in figcaption.find_all("math") if isinstance(m, Tag)
                ]

            fig_refs.extend(_image_refs(fig_el, base_url, caption, caption_mathml))

        own_body = copy.copy(sec_el)
        for nested in own_body.find_all("section"):
            nested.decompose()
        body_html = own_body.decode_contents()

        section = Section(
            heading_level=heading_level,
            heading_text=heading_text,
            body_html=body_html,
            equation_refs=equation_refs,
            figures=[],  # populated by caller after figure fetching
            mathml_nodes=mathml_nodes,
        )
        section_tuples.append((section, fig_refs))

    # Orphan figures: figures in the document not inside any <section>
    orphan_refs: list[FigureRef] = []
    for fig_el in soup.find_all("figure"):
        if not isinstance(fig_el, Tag):
            continue
        if id(fig_el) in consumed_figures or fig_el.find_parent("figure") is not None:
            continue

        figcaption = fig_el.find("figcaption")
        caption = (
            figcaption.get_text(strip=True)
            if figcaption and isinstance(figcaption, Tag)
            else ""
        )
        caption_mathml = []
        if figcaption and isinstance(figcaption, Tag):
            caption_mathml = [
                str(m) for m in figcaption.find_all("math") if isinstance(m, Tag)
            ]

        orphan_refs.extend(_image_refs(fig_el, base_url, caption, caption_mathml))

    # Frontmatter logos and other images outside figures are source content too.
    article = soup.find("article", class_="ltx_document") or soup.find("body") or soup
    for image in article.find_all(["img", "object", "svg"]):
        if image.find_parent(["figure", "svg", "object"]) is None:
            orphan_refs.extend(_image_refs(image, base_url, "", []))

    return section_tuples, orphan_refs


def _image_refs(
    root: Tag, base_url: str, caption: str, mathml: list[str]
) -> list[FigureRef]:
    """One guarded fetch per image, including SVG objects; never emit objects."""
    nodes = (
        [root]
        if root.name in {"img", "object", "svg"}
        else root.find_all(["img", "object", "svg"])
    )
    from paper2html.source_view import asset_key

    refs = []
    for node in nodes:
        if node is not root and node.find_parent(["svg", "object"]) is not None:
            continue
        raw = node.get("data" if node.name == "object" else "src")
        if node.name == "object" and not (
            str(node.get("type", "")).lower() == "image/svg+xml"
            or str(raw or "").split("?")[0].lower().endswith(".svg")
        ):
            continue
        if node.name == "svg":
            # LaTeXML minipages may contain raster scientific data inside spans.
            # Resolve/fetch these before their parent SVG through the same guarded
            # ingress, never by granting Chromium resource access.
            for image in node.select("foreignobject img"):
                nested_src = str(image.get("src", ""))
                if nested_src:
                    refs.append(
                        FigureRef(
                            caption=caption,
                            caption_mathml=[],
                            src_url=urljoin(base_url, nested_src),
                            raw_src=nested_src,
                        )
                    )
        refs.append(
            FigureRef(
                caption=caption,
                caption_mathml=mathml,
                src_url=urljoin(base_url, str(raw)) if raw else None,
                raw_src=asset_key(node)
                if node.name == "svg"
                else str(raw)
                if raw
                else None,
                inline_svg=str(node).encode() if node.name == "svg" else None,
            )
        )
    return refs
