"""Provider-agnostic Pydantic v2 document model for paper2html.

Design notes
------------
* **Not frozen**: ticket 007 back-fills ``alt_text`` / ``gloss`` on Figure via
  ``model_copy(update=...)`` — mutability by convention is intentional.
* **Sentence-id scheme**: ``{section_id}-sent-{n}`` (1-indexed), where the
  section text is split on ``". "``.  These ids appear in
  ``Figure.referencing_sentence_ids`` for arXiv-HTML sources only; PDF leaves
  the list empty.
* **figure_assets key contract**: keys are the RAW ``<img src>`` attribute
  values exactly as they appear in the HTML, NOT resolved URLs.  The caller is
  responsible for passing the matching key.
* **Bytes serialisation**: Pydantic v2's default JSON mode encodes bytes as
  UTF-8, which raises on binary data (e.g. PNG headers).  All models derive
  from ``_Base`` which sets ``ser_json_bytes="base64"`` and
  ``val_json_bytes="base64"`` so that roundtrips via ``model_dump(mode="json")``
  → ``model_validate`` are lossless byte-for-byte.
"""

from __future__ import annotations

import base64
import re
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from bs4 import BeautifulSoup, NavigableString, Tag
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    import arxiv  # noqa: F401
    import pymupdf  # noqa: F401


# ---------------------------------------------------------------------------
# Shared base — all models use base64 bytes serialisation
# ---------------------------------------------------------------------------


class _Base(BaseModel):
    """Shared config: base64 bytes in JSON mode for lossless roundtrips."""

    model_config = ConfigDict(ser_json_bytes="base64", val_json_bytes="base64")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ARXIV_ID_RE = re.compile(
    r"(\d{4}\.\d{4,5}(?:v\d+)?|[a-z-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?)"
)


def _short_arxiv_id(entry_id: str) -> str | None:
    """Extract and normalise the arXiv id from a versioned entry URL.

    Returns the id *without* the version suffix (e.g. ``2301.00001`` rather
    than ``2301.00001v1``).  Returns ``None`` if no id can be parsed.
    """
    m = _ARXIV_ID_RE.search(entry_id)
    if m is None:
        return None
    raw = m.group(1)
    # Strip trailing vN
    return re.sub(r"v\d+$", "", raw)


def _author_names(metadata: object) -> list[str]:
    """Extract author name strings from a duck-typed metadata object.

    Tolerant: each item in ``metadata.authors`` is tried for ``.name``; falls
    back to ``str(item)`` so plain string lists also work.
    """
    names: list[str] = []
    for a in getattr(metadata, "authors", []):
        name = getattr(a, "name", None)
        names.append(name if name is not None else str(a))
    return names


def _mime(ext: str) -> str:
    """Return MIME type string for common image extensions."""
    ext = ext.lower()
    if ext in ("jpeg", "jpg"):
        return "image/jpeg"
    return f"image/{ext}"


# ---------------------------------------------------------------------------
# Enum
# ---------------------------------------------------------------------------


class IngestionPath(str, Enum):
    """Source of the document."""

    ARXIV_HTML = "arxiv_html"
    PDF = "pdf"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class DocumentMetadata(_Base):
    """Bibliographic metadata for a normalised document."""

    arxiv_id: Optional[str] = None
    title: str = ""
    authors: list[str] = Field(default_factory=list)
    abstract: str = ""
    source_url: Optional[str] = None
    ingestion_path: IngestionPath = IngestionPath.ARXIV_HTML


class Section(_Base):
    """One structural section of the document."""

    id: str
    title: str = ""
    level: int
    text_content: str = ""
    parent_id: Optional[str] = None
    subsection_ids: list[str] = Field(default_factory=list)
    equation_ids: list[str] = Field(default_factory=list)
    figure_ids: list[str] = Field(default_factory=list)
    # Sanitized HTML for data tables in this section (built by from_arxiv_html;
    # cells carry KaTeX-delimited inline math). Rendered with the |safe filter.
    tables: list[str] = Field(default_factory=list)


class Figure(_Base):
    """A figure asset with optional image bytes."""

    id: str
    caption: str = ""
    image_bytes: bytes = b""
    image_mime_type: str = "image/png"
    referencing_sentence_ids: list[str] = Field(default_factory=list)
    label: Optional[str] = None
    page_number: Optional[int] = None
    alt_text: Optional[str] = None
    gloss: Optional[str] = None


class Equation(_Base):
    """A mathematical equation."""

    id: str
    raw_latex: Optional[str] = None
    mathml: Optional[str] = None
    display_type: str = "inline"
    source_text: str = ""


class Citation(_Base):
    """A bibliographic citation."""

    id: str
    raw_text: str = ""
    arxiv_id: Optional[str] = None
    doi: Optional[str] = None
    title: Optional[str] = None
    authors: list[str] = Field(default_factory=list)


class NormalizedDocument(_Base):
    """Top-level container for a normalised paper."""

    metadata: DocumentMetadata
    # Untrusted source, reconstructed anew at the renderer boundary.
    original_html: str = ""
    source_assets: dict[str, bytes] = Field(default_factory=dict)
    page_assets: list[bytes] = Field(default_factory=list)
    sections: list[Section] = Field(default_factory=list)
    figures: dict[str, Figure] = Field(default_factory=dict)
    equations: dict[str, Equation] = Field(default_factory=dict)
    citations: dict[str, Citation] = Field(default_factory=dict)
    ingestion_warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# arXiv-HTML adapter
# ---------------------------------------------------------------------------

# Tags whose text must NOT be flattened into a section's prose body.
#  - h1..h6: rendered separately as the section title (avoids heading duplication)
#  - section: nested subsections get their own Section object
#  - figure: rendered separately (avoids caption text bleeding into the body)
#  - table: rendered separately as a structured table
#  - annotation: holds the raw LaTeX source inside <math> (avoids LaTeX leaking
#    into prose, e.g. "h_{t}" appended after the rendered "ht")
_SKIP_PROSE_TAGS = frozenset(
    {
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "section",
        "figure",
        "table",
        "annotation",
        "script",
        "style",
        "noscript",
        "template",
    }
)

# Leading section number with no space before the title, e.g. "5.4Regularization".
_TITLE_NUM_RE = re.compile(r"^(\d+(?:\.\d+)*)(\S)")

# Leading bibliography label, e.g. "[32] " at the start of a reference entry.
_BIB_LABEL_RE = re.compile(r"^\[\d+\]\s*")


def _normalize_title(raw: str) -> str:
    """Insert a space between a leading section number and its title text.

    arXiv renders the number and title as adjacent spans, so ``get_text`` yields
    e.g. ``"5.4Regularization"`` — split it back to ``"5.4 Regularization"``.
    """
    return _TITLE_NUM_RE.sub(r"\1 \2", raw)


def _prose_text(node: object) -> str:
    """Extract readable prose from an HTML node for a section body.

    Unlike ``get_text``, this:
      * drops headings, nested sections, figures, tables, and <math> annotations
        (each rendered through its own dedicated path);
      * converts inline ``<math>`` to a KaTeX-delimited LaTeX span ``\\(...\\)``
        from its ``alttext`` (so math renders instead of flattening to a soup of
        Unicode glyphs followed by the raw LaTeX source);
      * drops display ``<math>`` (rendered separately as an equation block).
    """
    if isinstance(node, NavigableString):
        return str(node)
    if not isinstance(node, Tag):
        return ""
    name = node.name.lower()
    if name in _SKIP_PROSE_TAGS:
        return ""
    if name == "math":
        if node.get("display") == "block":
            return ""  # rendered as a standalone equation block
        alttext = node.get("alttext")
        return f" \\({alttext}\\) " if alttext else ""
    return "".join(_prose_text(child) for child in node.children)


def _escape_cell(text: str) -> str:
    """Minimal HTML escape for table cell text. KaTeX delimiters survive
    (``\\``, ``(``, ``)`` are not escaped); ``<``/``>`` inside ``alttext`` decode
    back in the browser text node so KaTeX still sees the original characters.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _build_table_html(table_el: Tag, caption: str) -> str:
    """Render an arXiv ``ltx_tabular`` table as clean, self-contained HTML.

    Cells are extracted via ``_prose_text`` so inline ``<math>`` becomes
    KaTeX-delimited LaTeX; everything is rebuilt from scratch (no LaTeXML markup
    passes through), so the output is safe to emit with the ``|safe`` filter.
    """
    rows_html: list[str] = []
    for tr in table_el.find_all("tr"):
        if not isinstance(tr, Tag):
            continue
        cells = [c for c in tr.find_all(["td", "th"]) if isinstance(c, Tag)]
        if not cells:
            continue
        cell_parts: list[str] = []
        for cell in cells:
            tag = "th" if cell.name == "th" else "td"
            text = re.sub(r"\s+", " ", _prose_text(cell)).strip()
            spans = ""
            for attr in ("rowspan", "colspan"):
                value = str(cell.get(attr, ""))
                if (
                    len(value) <= 4
                    and value.isascii()
                    and value.isdigit()
                    and 0 <= int(value) <= 1000
                ):
                    spans += f' {attr}="{value}"'
            cell_parts.append(f"<{tag}{spans}>{_escape_cell(text)}</{tag}>")
        rows_html.append("<tr>" + "".join(cell_parts) + "</tr>")
    if not rows_html:
        return ""
    cap_html = f"<figcaption>{_escape_cell(caption)}</figcaption>" if caption else ""
    return (
        '<figure class="paper-table"><table>'
        + "".join(rows_html)
        + "</table>"
        + cap_html
        + "</figure>"
    )


def from_arxiv_html(
    soup: BeautifulSoup,
    metadata: object,
    figure_assets: dict[str, bytes] | None = None,
) -> NormalizedDocument:
    """Build a :class:`NormalizedDocument` from a BeautifulSoup arXiv-HTML page.

    Parameters
    ----------
    soup:
        Parsed HTML document.
    metadata:
        Duck-typed object with ``.title``, ``.authors``, ``.summary``,
        ``.entry_id`` attributes — typically an ``arxiv.Result`` or a
        ``types.SimpleNamespace``.
    figure_assets:
        Optional mapping of raw ``<img src>`` attribute values to PNG bytes.
        Keys must match the attribute value verbatim (not resolved URLs).
    """
    warnings: list[str] = []
    article = soup.find("article", class_="ltx_document") or soup.find("body") or soup
    original_html = str(article)
    from paper2html.source_view import collect_source_assets

    source_assets = collect_source_assets(article, figure_assets or {}, warnings)

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------
    entry_id_str = str(getattr(metadata, "entry_id", "") or "")
    doc_metadata = DocumentMetadata(
        arxiv_id=_short_arxiv_id(entry_id_str),
        title=getattr(metadata, "title", "") or "",
        authors=_author_names(metadata),
        abstract=getattr(metadata, "summary", "") or "",
        source_url=entry_id_str or None,
        ingestion_path=IngestionPath.ARXIV_HTML,
    )

    # ------------------------------------------------------------------
    # Section parsing — pre-order recursive walk
    # ------------------------------------------------------------------
    sections: list[Section] = []
    # Map from BeautifulSoup element id() → our Section id string
    # (used when wiring equations and figures back to their section)
    _el_to_sec_id: dict[int, str] = {}

    # Give sectionless papers a real attachment owner without flattening tables.
    if not soup.find("section"):
        body = soup.find("body") or soup
        fallback = soup.new_tag("section")
        for child in list(body.contents):
            fallback.append(child.extract())
        body.append(fallback)

    # Top-level sections are any <section> with no <section> ancestor. arXiv
    # (LaTeXML) wraps the body content in <div class="ltx_page_main"> →
    # <div class="ltx_page_content"> → <article>, so the sections are NOT direct
    # children of <body>; a recursive=False scan there finds zero. Subsections
    # may have intervening wrappers; ownership follows the nearest section.
    root_sections = [
        sec_el
        for sec_el in soup.find_all("section")
        if isinstance(sec_el, Tag) and sec_el.find_parent("section") is None
    ]

    def _walk_sections(
        sec_el: Tag,
        sec_id: str,
        parent_id: Optional[str],
        parent_level: int,
    ) -> None:
        """Recursively walk section elements in pre-order, building Section objects."""
        # Heading
        h_tag = next(
            (
                h
                for h in sec_el.find_all(re.compile(r"^h[1-6]$"))
                if h.find_parent("section") is sec_el
            ),
            None,
        )
        if h_tag and isinstance(h_tag, Tag):
            level = int(h_tag.name[1])
            title = _normalize_title(h_tag.get_text(strip=True))
        else:
            level = parent_level + 1
            title = ""

        # text_content: clean prose from direct children. Headings, nested
        # sections, figures, tables, and <math> annotations are excluded; inline
        # math is converted to KaTeX-delimited LaTeX (see _prose_text).
        text_content = "".join(_prose_text(child) for child in sec_el.children).strip()
        # Collapse the runs of whitespace that dropping inline elements can leave.
        text_content = re.sub(r"[ \t]+", " ", text_content)
        text_content = re.sub(r" *\n *", "\n", text_content)

        # A heading-only parent is a valid container: its children retain the
        # prose. Still warn for genuinely empty leaf sections.
        if not text_content.strip() and sec_el.find("section") is None:
            warnings.append(f"{sec_id}: empty text_content")

        # Collect immediate child sections
        child_section_els = [
            child
            for child in sec_el.find_all("section")
            if child.find_parent("section") is sec_el
        ]
        child_ids: list[str] = []
        for m, child_el in enumerate(child_section_els, start=1):
            child_id = f"{sec_id}-{m}"
            child_ids.append(child_id)

        sec_obj = Section(
            id=sec_id,
            title=title,
            level=level,
            text_content=text_content,
            parent_id=parent_id,
            subsection_ids=child_ids,
        )
        sections.append(sec_obj)
        _el_to_sec_id[id(sec_el)] = sec_id

        # Recurse into children
        for m, child_el in enumerate(child_section_els, start=1):
            child_id = f"{sec_id}-{m}"
            _walk_sections(child_el, child_id, sec_id, level)

    for n, root_el in enumerate(root_sections, start=1):
        if not isinstance(root_el, Tag):
            continue
        _walk_sections(root_el, f"sec-{n}", None, 0)

    # ------------------------------------------------------------------
    # Equations — document order
    # ------------------------------------------------------------------
    equations: dict[str, Equation] = {}
    for n, el in enumerate(soup.find_all("math"), start=1):
        if not isinstance(el, Tag):
            continue
        eq_id = f"eq-{n}"
        display_type = "block" if el.get("display") == "block" else "inline"
        raw_latex: Optional[str] = el.get("alttext") or None  # type: ignore[assignment]
        mathml = str(el)

        # Find containing section
        parent_sec_el = el.find_parent("section")
        if parent_sec_el is not None:
            containing_sec_id = _el_to_sec_id.get(id(parent_sec_el))
            if containing_sec_id:
                for sec in sections:
                    if sec.id == containing_sec_id:
                        sec.equation_ids.append(eq_id)
                        break

        # source_text from enclosing <p>
        p_el = el.find_parent("p")
        source_text = p_el.get_text()[:200] if p_el and isinstance(p_el, Tag) else ""

        if not raw_latex and not mathml:
            warnings.append(f"{eq_id}: neither raw_latex nor mathml")

        equations[eq_id] = Equation(
            id=eq_id,
            raw_latex=raw_latex,
            mathml=mathml if mathml else None,
            display_type=display_type,
            source_text=source_text,
        )

    # ------------------------------------------------------------------
    # Data tables (ltx_tabular) — built as clean HTML, attached to their section.
    # The enclosing <figure class="ltx_table"> is recorded so the figure loop
    # below skips it (otherwise it becomes an empty, image-less figure).
    # ------------------------------------------------------------------
    consumed_table_figures: set[int] = set()
    for table_el in soup.find_all("table"):
        if not isinstance(table_el, Tag):
            continue
        if "ltx_tabular" not in (table_el.get("class") or []):
            continue  # ltx_equation/ltx_eqn_table are equations, not data tables
        fig_wrapper = table_el.find_parent("figure")
        caption = ""
        if fig_wrapper is not None and isinstance(fig_wrapper, Tag):
            consumed_table_figures.add(id(fig_wrapper))
            cap_el = fig_wrapper.find("figcaption")
            if cap_el and isinstance(cap_el, Tag):
                caption = _normalize_title(cap_el.get_text(strip=True))
        table_html = _build_table_html(table_el, caption)
        if not table_html:
            continue
        parent_sec_el = table_el.find_parent("section")
        if parent_sec_el is not None:
            sec_id = _el_to_sec_id.get(id(parent_sec_el))
            if sec_id:
                for sec in sections:
                    if sec.id == sec_id:
                        sec.tables.append(table_html)
                        break

    # ------------------------------------------------------------------
    # Figures — document order
    # ------------------------------------------------------------------
    figures: dict[str, Figure] = {}
    # label -> fig_id reverse index for AC-10
    _label_to_fig_id: dict[str, str] = {}

    for n, fig_el in enumerate(soup.find_all("figure"), start=1):
        if not isinstance(fig_el, Tag):
            continue
        if fig_el.find_parent("figure") is not None:
            continue
        if id(fig_el) in consumed_table_figures:
            continue  # rendered as a data table, not a figure
        fig_id = f"fig-{n}"

        figcaption = fig_el.find("figcaption")
        caption = (
            figcaption.get_text(strip=True)
            if figcaption and isinstance(figcaption, Tag)
            else ""
        )
        label = fig_el.get("id") or None  # type: ignore[assignment]

        if label:
            _label_to_fig_id[str(label)] = fig_id

        # Wire fig into containing section
        parent_sec_el = fig_el.find_parent("section")
        if parent_sec_el is not None:
            containing_sec_id = _el_to_sec_id.get(id(parent_sec_el))
            if containing_sec_id:
                for sec in sections:
                    if sec.id == containing_sec_id:
                        sec.figure_ids.append(fig_id)
                        break

        # Resolve image bytes
        image_bytes = b""
        got_bytes = False

        img = fig_el.find(["img", "object"])
        if img and isinstance(img, Tag):
            src = img.get("data" if img.name == "object" else "src")
            if src:
                src = str(src)
                if src in source_assets:
                    image_bytes = source_assets[src]
                    got_bytes = True
                elif src.startswith("data:"):
                    try:
                        comma = src.index(",")
                        image_bytes = base64.b64decode(src[comma + 1 :])
                        got_bytes = True
                    except Exception:
                        warnings.append(f"{fig_id}: malformed data: URI")

        if not got_bytes:
            svg_el = fig_el.find("svg")
            if svg_el and isinstance(svg_el, Tag):
                try:
                    from paper2html.ingestion._safety import svg_png

                    image_bytes = svg_png(str(svg_el).encode())
                    got_bytes = True
                except (OSError, ImportError, Exception) as exc:
                    warnings.append(f"{fig_id}: cairosvg conversion failed: {exc}")
                    image_bytes = b""

        if not got_bytes and not image_bytes:
            warnings.append(f"{fig_id}: no extractable image bytes")

        image_mime_type = "image/png"
        if image_bytes:
            from paper2html._render_safety import raster_mime

            try:
                image_mime_type = raster_mime(image_bytes)
            except (ValueError, OSError, SyntaxError) as exc:
                warnings.append(f"{fig_id}: unsafe or invalid image omitted: {exc}")
                image_bytes = b""

        figures[fig_id] = Figure(
            id=fig_id,
            caption=caption,
            image_bytes=image_bytes,
            image_mime_type=image_mime_type,
            label=label,
        )

    # ------------------------------------------------------------------
    # AC-10: referencing sentence ids — anchors → figure ids
    # Walk all section elements in document order, match via _el_to_sec_id.
    # ------------------------------------------------------------------
    for sec_tag in soup.find_all("section"):
        if not isinstance(sec_tag, Tag):
            continue
        sec_id = _el_to_sec_id.get(id(sec_tag))
        if sec_id is None:
            continue

        # Find the Section object
        sec_obj: Section | None = None
        for s in sections:
            if s.id == sec_id:
                sec_obj = s
                break
        if sec_obj is None:
            continue

        # Split text_content into sentences
        sentences = sec_obj.text_content.split(". ")
        sent_ids = [f"{sec_id}-sent-{i}" for i in range(1, len(sentences) + 1)]

        # Find all <a href="#X"> anchors inside this section element
        for a_tag in sec_tag.find_all("a", href=True):
            if not isinstance(a_tag, Tag):
                continue
            href = str(a_tag.get("href", ""))
            if not href.startswith("#"):
                continue
            fragment = href[1:]
            fig_id = _label_to_fig_id.get(fragment)
            if fig_id is None:
                continue

            # Find which sentence contains this anchor's text
            anchor_text = a_tag.get_text(strip=True)
            sent_idx = 0
            for i, sent in enumerate(sentences):
                if anchor_text in sent:
                    sent_idx = i
                    break
            if sent_idx < len(sent_ids):
                sent_id = sent_ids[sent_idx]
            elif sent_ids:
                sent_id = sent_ids[0]
            else:
                sent_id = f"{sec_id}-sent-1"

            fig = figures.get(fig_id)
            if fig and sent_id not in fig.referencing_sentence_ids:
                fig.referencing_sentence_ids.append(sent_id)

    # ------------------------------------------------------------------
    # Citations — best-effort ltx_biblist parsing
    # ------------------------------------------------------------------
    citations: dict[str, Citation] = {}
    biblist = soup.find("ul", class_="ltx_biblist")
    if biblist and isinstance(biblist, Tag):
        for n, li in enumerate(biblist.find_all("li", class_="ltx_bibitem"), start=1):
            if not isinstance(li, Tag):
                continue
            ref_id = f"ref-{n}"
            raw_text = li.get_text(" ", strip=True)
            # Drop the leading "[N]" bib label — the <ol> already numbers each
            # entry, so keeping it renders a doubled "32. [32] ...".
            raw_text = _BIB_LABEL_RE.sub("", raw_text, count=1)
            citations[ref_id] = Citation(id=ref_id, raw_text=raw_text)

    return NormalizedDocument(
        metadata=doc_metadata,
        original_html=original_html,
        source_assets=source_assets,
        sections=sections,
        figures=figures,
        equations=equations,
        citations=citations,
        ingestion_warnings=warnings,
    )


# ---------------------------------------------------------------------------
# PDF adapter
# ---------------------------------------------------------------------------


def from_pdf(
    doc: "pymupdf.Document",
    pdf_path: Path,
    metadata: object | None = None,
    page_assets: list[bytes] | None = None,
    *,
    extract_images: bool = True,
) -> NormalizedDocument:
    """Build a :class:`NormalizedDocument` from an open PyMuPDF Document.

    Parameters
    ----------
    doc:
        An open ``pymupdf.Document`` instance.  The caller retains ownership.
    pdf_path:
        Path to the source PDF — used for default title and id derivation.
    page_assets:
        Optional already-rasterized, bounded PDF pages from ingest_pdf, in page order.
        The renderer exposes these as a facsimile with separate searchable text.
    extract_images:
        False skips fragment extraction when only the complete page view is needed.
    metadata:
        Optional duck-typed metadata object (same duck-type as arXiv adapter).
        When ``None`` a minimal ``DocumentMetadata`` is constructed from the
        path name.
    """
    if page_assets is not None and len(page_assets) != len(doc):
        raise ValueError("PDF facsimile requires one image for every source page")

    warnings: list[str] = []

    if metadata is None:
        doc_metadata = DocumentMetadata(
            arxiv_id=None,
            title=pdf_path.name,
            ingestion_path=IngestionPath.PDF,
        )
    else:
        entry_id_str = str(getattr(metadata, "entry_id", "") or "")
        doc_metadata = DocumentMetadata(
            arxiv_id=_short_arxiv_id(entry_id_str),
            title=getattr(metadata, "title", "") or "",
            authors=_author_names(metadata),
            abstract=getattr(metadata, "summary", "") or "",
            source_url=entry_id_str or None,
            ingestion_path=IngestionPath.PDF,
        )

    sections: list[Section] = []
    figures: dict[str, Figure] = {}
    fig_counter = 0

    for page_index in range(len(doc)):
        page = doc[page_index]
        page_n = page_index + 1  # 1-indexed display number
        sec_id = f"sec-{page_n}"

        text_content = page.get_text()
        if not text_content.strip():
            warnings.append(f"{sec_id}: empty text_content")

        sec_obj = Section(
            id=sec_id,
            title=f"Page {page_n}",
            level=1,
            text_content=text_content,
        )

        # Extract embedded images from this page
        for image_tuple in page.get_images(full=True) if extract_images else []:
            fig_counter += 1
            fig_id = f"fig-{fig_counter}"
            xref = image_tuple[0]

            try:
                from paper2html.ingestion._limits import validate_image_dimensions

                # Normalization reopens PDFs after ingestion (and directly on
                # arXiv fallback), so it must repeat the pre-extraction guard.
                if len(image_tuple) > 3:
                    validate_image_dimensions(image_tuple[2], image_tuple[3])
                info = doc.extract_image(xref)
                image_bytes: bytes = info["image"]
                from paper2html._render_safety import raster_mime

                mime = raster_mime(image_bytes)
                sec_obj.figure_ids.append(fig_id)
                figures[fig_id] = Figure(
                    id=fig_id,
                    image_bytes=image_bytes,
                    image_mime_type=mime,
                    page_number=page_index,  # 0-indexed per ticket spec
                )
            except Exception as exc:
                warnings.append(f"{fig_id}: failed to extract image xref={xref}: {exc}")
                sec_obj.figure_ids.append(fig_id)
                figures[fig_id] = Figure(
                    id=fig_id,
                    image_bytes=b"",
                    page_number=page_index,
                )

        sections.append(sec_obj)

    return NormalizedDocument(
        metadata=doc_metadata,
        page_assets=page_assets or [],
        sections=sections,
        figures=figures,
        equations={},
        citations={},
        ingestion_warnings=warnings,
    )
