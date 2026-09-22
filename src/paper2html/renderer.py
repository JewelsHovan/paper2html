"""HTML renderer for paper2html.

Converts a :class:`~paper2html.models.document.NormalizedDocument` and an
:class:`~paper2html.review.schemas.AnnotationBundle` into a single,
self-contained HTML file that can be opened offline in any modern browser
(file:// URL with no network dependencies).

Security: document markup is reconstructed through strict allowlists and images
are validated as raster bytes at the rendering boundary.

Bundle reading-aid fields (term_definitions, margin_notes, section_summaries):
    These fields are empty in production until ticket-010 glue lands.  The
    renderer degrades gracefully — reading-aid UI elements are simply absent
    when the corresponding data is empty.
"""

from __future__ import annotations

import base64
import os
import stat
import tempfile
import re
import warnings
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader
from markupsafe import Markup, escape

from paper2html.models.document import NormalizedDocument
from paper2html._render_safety import raster_mime, sanitize_markup
from paper2html.review.schemas import AnnotationBundle

# ---------------------------------------------------------------------------
# Internal paths
# ---------------------------------------------------------------------------
_TEMPLATES_DIR = Path(__file__).parent / "templates"
_ASSETS_DIR = Path(__file__).parent / "assets" / "katex"


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _split_sentences(text: str) -> list[str]:
    """Split text into sentence fragments exactly as the document model does.

    Mirrors ``Section.text_content.split(". ")`` — no normalisation, no strip.
    """
    return text.split(". ")


def _sentences_to_html(
    text: str,
    section_id: str,
    figure_numbers: dict[int, str],
    term_definitions: dict[str, str],
    used_terms: set[str],
) -> Markup:
    """Annotate raw-text intervals, then escape and emit markup exactly once.

    Figure links own their intervals. Terms use longest-first overlap resolution
    and first occurrence per section; generated HTML is never regex input.
    """
    result_parts: list[str] = []
    for i, raw in enumerate(_split_sentences(text), start=1):
        intervals: list[tuple[int, int, str, str]] = []
        for match in re.finditer(r"\bFig(?:ure)?\.?\s*(\d+)\b", raw):
            fig_id = figure_numbers.get(int(match.group(1)))
            if fig_id is not None:
                intervals.append((match.start(), match.end(), "figure", fig_id))
        for term in sorted(term_definitions, key=len, reverse=True):
            if not term or term.lower() in used_terms:
                continue
            for match in re.finditer(
                r"(?<!\w)" + re.escape(term) + r"(?!\w)", raw, re.I
            ):
                start, end = match.span()
                if any(start < b and end > a for a, b, _, _ in intervals):
                    continue
                intervals.append((start, end, "term", term_definitions[term]))
                used_terms.add(term.lower())
                break
        parts: list[str] = []
        cursor = 0
        for start, end, kind, value in sorted(intervals):
            parts.append(str(escape(raw[cursor:start])))
            label = escape(raw[start:end])
            if kind == "figure":
                parts.append(f'<a href="#{escape(value)}" class="fig-ref">{label}</a>')
            else:
                parts.append(
                    f'<span class="term-def" data-def="{escape(value)}">{label}</span>'
                )
            cursor = end
        parts.append(str(escape(raw[cursor:])))
        sent_id = escape(f"{section_id}-sent-{i}")
        result_parts.append(f'<span id="{sent_id}">{"".join(parts)}</span>')
    return Markup(". ".join(result_parts))


def _figure_to_data_uri(image_bytes: bytes) -> str | None:
    """Embed validated raster bytes only; claimed MIME is never trusted."""
    if not image_bytes:
        return None
    try:
        mime = raster_mime(image_bytes)
    except (ValueError, OSError, SyntaxError) as exc:
        warnings.warn(
            f"Unsafe or invalid figure omitted: {exc}", UserWarning, stacklevel=2
        )
        return None
    b64 = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _is_proof_section(title: str) -> bool:
    """Return True when the section title indicates a proof/derivation/appendix."""
    return bool(re.search(r"\b(proof|derivation|appendix)\b", title, re.IGNORECASE))


def _load_katex_assets() -> dict[str, str]:
    """Read vendored KaTeX assets from the assets/katex directory.

    Returns a dict with keys: katex_js, auto_render_js, katex_css.
    """
    # Standalone HTML redistributes KaTeX, so retain its full MIT notice there
    # as well as in the source/package assets. This is trusted vendored text.
    license_notice = (_ASSETS_DIR / "LICENSE").read_text(encoding="utf-8")
    return {
        "katex_js": "/*\n"
        + license_notice
        + "\n*/\n"
        + (_ASSETS_DIR / "katex.min.js").read_text(encoding="utf-8"),
        "auto_render_js": (_ASSETS_DIR / "auto-render.min.js").read_text(
            encoding="utf-8"
        ),
        "katex_css": (_ASSETS_DIR / "katex.min.css").read_text(encoding="utf-8"),
    }


def _build_template_context(
    doc: NormalizedDocument,
    bundle: AnnotationBundle,
) -> dict[str, Any]:
    """Assemble the full Jinja2 template context dict."""

    from paper2html.source_view import render_source

    source_view = render_source(doc)
    show_metadata_header = not doc.original_html
    figure_notes = [
        {"label": fig.label or fig.id, "gloss": fig.gloss}
        for fig in doc.figures.values()
        if fig.gloss
    ]

    # Figure number map: fig-N → N (int key)
    figure_numbers: dict[int, str] = {}
    for fig_id in doc.figures:
        # Extract trailing integer from ids like "fig-3" or "fig-12"
        m = re.search(r"(\d+)$", fig_id)
        if m:
            n = int(m.group(1))
            figure_numbers[n] = fig_id

    # Scan every rendered textual field, including table-only inline math.
    math_texts = [doc.metadata.title, doc.metadata.abstract, *doc.metadata.authors]
    for section in doc.sections:
        math_texts.extend([section.title, section.text_content, *section.tables])
    for fig in doc.figures.values():
        math_texts.extend([fig.caption, fig.gloss or ""])
    math_texts.extend(c.raw_text for c in doc.citations.values())
    math_texts.extend(bundle.margin_notes)
    for summaries in bundle.section_summaries.values():
        math_texts.extend(summaries.values())
    has_katex = any(
        "\\(" in text or "\\[" in text for text in math_texts if text
    ) or any(eq.raw_latex for eq in doc.equations.values())

    # Load KaTeX assets (only reads files if needed, but always load for simplicity)
    katex_assets: dict[str, str] = {
        "katex_js": "",
        "auto_render_js": "",
        "katex_css": "",
    }
    if source_view is not None:
        # Native article MathML and PDF transcription must never be rewritten.
        # Only text actually displayed in the separate aids/header needs KaTeX.
        aid_texts = [
            *bundle.margin_notes,
            *bundle.term_definitions.keys(),
            *bundle.term_definitions.values(),
        ]
        for section in doc.sections:
            summaries = bundle.section_summaries.get(section.id, {})
            if summaries and all(k in summaries for k in ("skim", "read", "deep")):
                aid_texts.extend([section.title, *summaries.values()])
        for note in figure_notes:
            aid_texts.extend([note["label"], note["gloss"]])
        if show_metadata_header:
            aid_texts.extend(
                [doc.metadata.title, doc.metadata.abstract, *doc.metadata.authors]
            )
        has_katex = any("\\(" in text or "\\[" in text for text in aid_texts if text)
    if has_katex:
        katex_assets = _load_katex_assets()

    # Load static assets from templates dir
    theme_css = (_TEMPLATES_DIR / "theme.css").read_text(encoding="utf-8")
    reading_aids_js = (_TEMPLATES_DIR / "reading_aids.js").read_text(encoding="utf-8")

    # Build section contexts
    sections_ctx: list[dict[str, Any]] = []
    for section in doc.sections:
        # used_terms is per-section (reset for each section)
        used_terms: set[str] = set()

        html_content = _sentences_to_html(
            section.text_content,
            section.id,
            figure_numbers,
            bundle.term_definitions,
            used_terms,
        )

        # Resolve figures for this section
        figs_ctx: list[dict[str, Any]] = []
        for fig_id in section.figure_ids:
            fig = doc.figures.get(fig_id)
            if fig is None:
                continue
            data_uri = _figure_to_data_uri(fig.image_bytes)
            alt = fig.alt_text or fig.caption or f"Figure {fig_id}"
            figs_ctx.append(
                {
                    "id": fig_id,
                    "data_uri": data_uri,
                    "alt": alt,
                    "caption": fig.caption,
                    "gloss": fig.gloss,
                    "referencing_sentence_ids": fig.referencing_sentence_ids,
                }
            )

        # Resolve equations for this section (skip equations with neither field).
        # Only block (display) equations render as standalone blocks; inline math
        # is emitted within the prose body as KaTeX-delimited LaTeX, so rendering
        # inline equations here too would duplicate them out of context.
        eqs_ctx: list[dict[str, Any]] = []
        for eq_id in section.equation_ids:
            eq = doc.equations.get(eq_id)
            if eq is None:
                continue
            if not eq.mathml and not eq.raw_latex:
                continue
            if eq.display_type != "block":
                continue
            eqs_ctx.append(
                {
                    "id": eq_id,
                    "mathml": sanitize_markup(eq.mathml) if eq.mathml else "",
                    "raw_latex": eq.raw_latex,
                    "display_type": eq.display_type,
                }
            )

        # Summaries: only include when all three levels present
        sec_summaries = bundle.section_summaries.get(section.id)
        summaries = None
        if sec_summaries and all(k in sec_summaries for k in ("skim", "read", "deep")):
            summaries = sec_summaries

        # Level clamped 1–6 for valid h-tag
        level = max(1, min(6, section.level))

        sections_ctx.append(
            {
                "id": section.id,
                "title": section.title,
                "level": level,
                "html_content": html_content,
                "is_proof": _is_proof_section(section.title),
                "figures": figs_ctx,
                "equations": eqs_ctx,
                "tables": [sanitize_markup(t, table=True) for t in section.tables],
                "summaries": summaries,
            }
        )

    # Citations in insertion order
    citations_ctx = [
        {"id": cit_id, "raw_text": cit.raw_text}
        for cit_id, cit in doc.citations.items()
    ]

    return {
        "source_view": source_view,
        "show_metadata_header": show_metadata_header,
        "figure_notes": figure_notes,
        "term_definitions": bundle.term_definitions,
        "title": doc.metadata.title or "Untitled",
        "authors": doc.metadata.authors,
        "abstract": doc.metadata.abstract,
        "sections": sections_ctx,
        "citations": citations_ctx,
        "margin_notes": bundle.margin_notes,
        "has_katex": has_katex,
        "katex_js": katex_assets["katex_js"],
        "auto_render_js": katex_assets["auto_render_js"],
        "katex_css": katex_assets["katex_css"],
        "theme_css": theme_css,
        "reading_aids_js": reading_aids_js,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def render_html(
    doc: NormalizedDocument,
    bundle: AnnotationBundle,
    output_path: Path,
    *,
    overwrite: bool = True,
) -> Path:
    """Render a paper to a self-contained HTML file.

    All figures are inlined as base64 data URIs.  KaTeX assets (for PDF-sourced
    documents with raw_latex equations) are inlined from the vendored bundle at
    ``src/paper2html/assets/katex/``.  No network requests are made at render time.

    Args:
        doc: Normalized document produced by the ingestion layer.
        bundle: Annotation bundle from the review stage.  Reading-aid fields
            (term_definitions, margin_notes, section_summaries) are populated
            by attach_reading_aids (review/glue.py) after the pipeline run;
            the renderer degrades gracefully when fields are empty.
        output_path: Destination path, atomically replaced if it already exists.
            Symlinks are replaced, not followed. Parent directory must exist.
        overwrite: False uses atomic no-clobber publication on local filesystems.
            True preserves existing regular-file mode; new files use 0644.

    Returns:
        The output_path, unchanged, for caller convenience.

    Security:
        Untrusted MathML and tables are allowlist-reconstructed; raster images
        are validated from bytes. Only vendored assets and rebuilt markup are safe.
        Source budget violations fail closed before publication; no incomplete
        normalized fallback is used. The CLI also preflights before provider work.
    """
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=True,
        keep_trailing_newline=True,
    )
    template = env.get_template("paper.html.jinja2")
    context = _build_template_context(doc, bundle)
    html_text = template.render(**context)
    # A sibling temp keeps replacement on the same filesystem. The destination
    # remains intact on write/close/replace failure, including KeyboardInterrupt.
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp:
            temp_path = Path(temp.name)
            temp.write(html_text)
        # Existing regular-file permissions survive replacement. New publications
        # use explicit 0644 (no process-global umask read/change race).
        mode = 0o644
        if overwrite:
            try:
                existing = output_path.lstat()
                if stat.S_ISREG(existing.st_mode):
                    mode = stat.S_IMODE(existing.st_mode)
            except FileNotFoundError:
                pass
        os.chmod(temp_path, mode)
        if overwrite:
            os.replace(temp_path, output_path)
        else:
            # Atomic create-if-absent on the same local filesystem. Never fall
            # back to replace: unsupported link semantics fail safely.
            os.link(temp_path, output_path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return output_path


def check_offline_integrity(html_path: Path) -> list[str]:
    """Parse an HTML file and return a list of offline-integrity violations.

    A violation is any reference that would require a network request:
    - Resource ``src``, ``href``, ``action``, or ``data`` attributes starting
      with ``http://``, ``https://``, or ``//`` (protocol-relative).
      Ordinary ``<a href>`` hyperlinks are navigation, not load dependencies,
      and are allowed so original citations/dataset links can be preserved.
    - Any ``srcset`` attribute containing ``http`` or ``//`` candidates.
    - SVG ``<image>`` elements with external ``href`` or ``xlink:href``.
    - Any ``<link>`` element with an external ``href``.
    - ``@import`` rules inside ``<style>`` blocks.
    - ``url(...)`` in ``<style>`` blocks whose argument does not start with
      ``data:`` or ``#``.

    Args:
        html_path: Path to the HTML file to inspect.

    Returns:
        A list of human-readable violation strings.  Empty list means the file
        passes the offline-integrity check.
    """
    text = html_path.read_text(encoding="utf-8")
    soup = BeautifulSoup(text, "html.parser")
    violations: list[str] = []

    # Attribute-level checks
    _EXTERNAL_PREFIXES = ("http://", "https://", "//")

    for tag in soup.find_all(True):
        # src / href / action / data attributes
        for attr in ("src", "href", "action", "data"):
            if tag.name == "a" and attr == "href":
                continue
            val = tag.get(attr)
            if not val or not isinstance(val, str):
                continue
            for prefix in _EXTERNAL_PREFIXES:
                if val.startswith(prefix):
                    violations.append(f"<{tag.name}> has external {attr}={val!r}")
                    break

        # srcset attribute
        srcset = tag.get("srcset")
        if srcset and isinstance(srcset, str):
            for candidate in srcset.split(","):
                candidate = candidate.strip().split()[0] if candidate.strip() else ""
                if candidate.startswith("http") or candidate.startswith("//"):
                    violations.append(
                        f"<{tag.name}> has external srcset candidate: {candidate!r}"
                    )

        # SVG <image> elements
        if tag.name == "image":
            for svg_href_attr in ("href", "xlink:href"):
                val = tag.get(svg_href_attr)
                if val and isinstance(val, str):
                    for prefix in _EXTERNAL_PREFIXES:
                        if val.startswith(prefix):
                            violations.append(
                                f"<image> has external {svg_href_attr}={val!r}"
                            )
                            break

        # <link> elements with external href
        if tag.name == "link":
            href = tag.get("href")
            if href and isinstance(href, str):
                for prefix in _EXTERNAL_PREFIXES:
                    if href.startswith(prefix):
                        violations.append(f"<link> has external href={href!r}")
                        break

    # Style block checks
    for style_tag in soup.find_all("style"):
        css_text = style_tag.get_text() if style_tag else ""
        if not css_text:
            continue

        # @import rules
        for imp in re.findall(r"@import\s+['\"]?([^;'\"\s]+)", css_text):
            violations.append(f"<style> contains @import: {imp!r}")

        # url() with non-data argument
        for url_arg in re.findall(r"url\(\s*(['\"]?)([^)'\"]+)\1\s*\)", css_text):
            arg = url_arg[1]
            if not arg.startswith("data:") and not arg.startswith("#"):
                violations.append(f"<style> contains non-inline url(): {arg!r}")

    return violations
