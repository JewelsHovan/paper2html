"""Source-claim verification and script validation (not semantic fact checking)."""

from __future__ import annotations

import re
from pathlib import Path

from .core import Blocked, load, manifest_source


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def check(root: Path, script: dict | None = None) -> dict:
    m = manifest_source(root)
    script = script if script is not None else load(root, "script.json")
    catalog = load(root, "sources/catalog.json")
    if (
        script.get("source_revision") != m["identity"]["revision"]
        or not isinstance(script.get("scenes"), list)
        or not script["scenes"]
    ):
        raise Blocked(
            "SCRIPT_SCHEMA", "Script missing scenes or source revision mismatch"
        )
    ids = {e["id"]: e["text"] for e in catalog["html_elements"]}
    pages = {p["pdf_page"]: p["text"] for p in catalog["pages"]}
    seen = set()
    for scene in script["scenes"]:
        sid = scene.get("id")
        if (
            not isinstance(sid, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", sid)
            or sid in seen
        ):
            raise Blocked("SCRIPT_SCHEMA", "Scene IDs must be unique safe identifiers")
        seen.add(sid)
        if not all(
            isinstance(scene.get(key), str) and scene[key].strip()
            for key in ("title", "narration")
        ):
            raise Blocked("SCRIPT_SCHEMA", "Scene needs title and spoken narration")
        if not isinstance(scene.get("claims"), list) or not scene["claims"]:
            raise Blocked("SCRIPT_CLAIMS", "Each scene requires source-grounded claims")
        for claim in scene["claims"]:
            page = claim.get("pdf_page")
            excerpt = claim.get("quote")
            if page not in pages or not isinstance(excerpt, str) or not excerpt.strip():
                raise Blocked(
                    "SCRIPT_CLAIMS", "Each claim requires a real PDF page and quote"
                )
            origin = claim.get("source", "pdf")
            if origin == "html":
                element = claim.get("html_id")
                if element not in ids or norm(excerpt) not in norm(ids[element]):
                    raise Blocked(
                        "SCRIPT_QUOTE", "HTML quote not found at claimed native ID"
                    )
            elif origin != "pdf" or norm(excerpt) not in norm(pages[page]):
                raise Blocked("SCRIPT_QUOTE", "PDF quote not found at claimed page")
            if not isinstance(claim.get("claim"), str) or not claim["claim"].strip():
                raise Blocked("SCRIPT_CLAIMS", "Claim text required")
        for ref in scene.get("visual_refs", []):
            if ref.get("pdf_page") not in pages:
                raise Blocked("SCRIPT_VISUAL", "Visual needs original PDF page")
            if ref.get("html_id") and ref["html_id"] not in ids:
                raise Blocked("SCRIPT_VISUAL", "Unknown native figure/table ID")
        if not isinstance(scene.get("visual_refs", []), list):
            raise Blocked("SCRIPT_VISUAL", "Invalid visual refs")
    return script
