"""SQLite-backed library index for processed papers.

Stdlib-only: sqlite3, json, logging, pathlib. Zero new dependencies.
"""

from __future__ import annotations

from contextlib import closing
import json
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS papers (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    arxiv_id         TEXT,
    title            TEXT NOT NULL,
    authors          TEXT NOT NULL,
    processed_date   TEXT NOT NULL,
    source_type      TEXT NOT NULL,
    output_html_path TEXT NOT NULL,
    cost_spent       REAL NOT NULL DEFAULT 0.0
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_arxiv_id ON papers(arxiv_id)
    WHERE arxiv_id IS NOT NULL;
"""

_UPSERT_WITH_ARXIV = """
INSERT INTO papers
    (arxiv_id, title, authors, processed_date, source_type, output_html_path, cost_spent)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(arxiv_id) WHERE arxiv_id IS NOT NULL DO UPDATE SET
    processed_date = excluded.processed_date,
    cost_spent     = excluded.cost_spent,
    output_html_path = excluded.output_html_path
"""

_INSERT_NO_ARXIV = """
INSERT INTO papers
    (arxiv_id, title, authors, processed_date, source_type, output_html_path, cost_spent)
VALUES (NULL, ?, ?, ?, ?, ?, ?)
"""


def init_db(db_path: Path) -> sqlite3.Connection:
    """Open (or create) the SQLite database at *db_path* and apply the schema.

    Idempotent: safe to call multiple times on the same path.
    Returns an open :class:`sqlite3.Connection` with :attr:`row_factory` set to
    :class:`sqlite3.Row`.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        current_version = conn.execute("PRAGMA user_version").fetchone()[0]
        if current_version not in (0, 1):
            raise ValueError(
                f"Unsupported library schema version {current_version}; supported: 1"
            )
        # executescript commits a pending transaction: put BEGIN inside the script.
        conn.executescript(
            "BEGIN IMMEDIATE;\n" + _DDL + "\nPRAGMA user_version = 1;\nCOMMIT;"
        )
        return conn
    except BaseException:
        try:
            conn.rollback()
        finally:
            conn.close()
        raise


def upsert_paper(
    conn: sqlite3.Connection,
    *,
    arxiv_id: str | None,
    title: str,
    authors: list[str],
    processed_date: str,
    source_type: str,
    output_html_path: str,
    cost_spent: float,
) -> int:
    """Insert or update a paper row.

    - If *arxiv_id* is not None: uses a partial-index upsert so that a second
      call with the same arxiv_id updates *processed_date*, *cost_spent*, and *output_html_path*
      without creating a duplicate row.  The existing row id is returned.
    - If *arxiv_id* is None: plain INSERT (multiple NULL rows allowed).

    Returns the row ``id``.
    """
    authors_json = json.dumps(authors)

    if arxiv_id is not None:
        conn.execute(
            _UPSERT_WITH_ARXIV,
            (
                arxiv_id,
                title,
                authors_json,
                processed_date,
                source_type,
                output_html_path,
                cost_spent,
            ),
        )
        conn.commit()
        # Retrieve the id of the canonical row (may be the existing one on conflict).
        row = conn.execute(
            "SELECT id FROM papers WHERE arxiv_id = ?", (arxiv_id,)
        ).fetchone()
        return int(row["id"])
    else:
        cursor = conn.execute(
            _INSERT_NO_ARXIV,
            (
                title,
                authors_json,
                processed_date,
                source_type,
                output_html_path,
                cost_spent,
            ),
        )
        conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]


def list_papers(conn: sqlite3.Connection) -> list[dict]:
    """Return all paper rows ordered by *processed_date* descending.

    Authors remain as the raw JSON text stored in the database.
    """
    rows = conn.execute(
        "SELECT id, arxiv_id, title, authors, processed_date, source_type, "
        "output_html_path, cost_spent FROM papers ORDER BY processed_date DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_paper_by_id(conn: sqlite3.Connection, paper_id: int) -> dict | None:
    """Return a single paper row by primary-key *id*, or ``None`` if not found."""
    row = conn.execute(
        "SELECT id, arxiv_id, title, authors, processed_date, source_type, "
        "output_html_path, cost_spent FROM papers WHERE id = ?",
        (paper_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def record_paper(
    db_path: Path,
    *,
    arxiv_id: str | None,
    title: str,
    authors: list[str],
    processed_date: str,
    source_type: str,
    output_html_path: str,
    cost_spent: float,
) -> None:
    """Fire-and-forget wrapper: write a paper to the index without raising.

    Any exception is caught and emitted as a WARNING log so the main pipeline
    is never interrupted by a library write failure.
    """
    try:
        with closing(init_db(db_path)) as conn:
            upsert_paper(
                conn,
                arxiv_id=arxiv_id,
                title=title,
                authors=authors,
                processed_date=processed_date,
                source_type=source_type,
                output_html_path=output_html_path,
                cost_spent=cost_spent,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("library: failed to record paper: %s", exc)
