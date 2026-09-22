"""SQLite-backed local library index for processed papers.

Re-exports the public API from :mod:`paper2html.library.db`.
"""

from __future__ import annotations

from paper2html.library.db import (
    get_paper_by_id,
    init_db,
    list_papers,
    record_paper,
    upsert_paper,
)

__all__ = [
    "get_paper_by_id",
    "init_db",
    "list_papers",
    "record_paper",
    "upsert_paper",
]
