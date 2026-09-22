"""Tests for the local SQLite library index and CLI commands: list, open, cache clear.

SC-1: all tests in this file must pass via `uv run pytest tests/test_library.py -v`.
"""

from __future__ import annotations

import json
import webbrowser
from pathlib import Path
from unittest.mock import MagicMock

from typer.testing import CliRunner

from paper2html.cli import app
from paper2html.library.db import (
    get_paper_by_id,
    init_db,
    list_papers,
    record_paper,
    upsert_paper,
)

runner = CliRunner()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEFAULT_PAPER = dict(
    arxiv_id="2301.00001",
    title="A Great Paper",
    authors=["Alice Smith", "Bob Jones"],
    processed_date="2024-01-15T12:00:00Z",
    source_type="arxiv",
    output_html_path="/tmp/fake/out.html",
    cost_spent=0.42,
)


def _seed(cache_dir: Path, **overrides) -> int:
    """Open the db at cache_dir/library.db, insert one paper, return row id."""
    conn = init_db(cache_dir / "library.db")
    kwargs = {**_DEFAULT_PAPER, **overrides}
    row_id = upsert_paper(conn, **kwargs)
    conn.close()
    return row_id


def _invoke(tmp_path: Path, *args: str):
    """Invoke the CLI with --cache-dir pointing at tmp_path."""
    env = {"COLUMNS": "200", "TERM": "dumb"}
    return runner.invoke(app, ["--cache-dir", str(tmp_path), *args], env=env)


# ---------------------------------------------------------------------------
# db.py unit tests
# ---------------------------------------------------------------------------


def test_init_db_creates_table(tmp_path):
    """Fresh database returns empty list_papers."""
    conn = init_db(tmp_path / "library.db")
    assert list_papers(conn) == []
    conn.close()


def test_init_db_idempotent(tmp_path):
    """Calling init_db twice on the same path does not destroy existing rows."""
    db_path = tmp_path / "library.db"
    conn = init_db(db_path)
    upsert_paper(conn, **_DEFAULT_PAPER)
    conn.close()

    # Re-open via init_db
    conn2 = init_db(db_path)
    rows = list_papers(conn2)
    conn2.close()
    assert len(rows) == 1


def test_user_version_is_1(tmp_path):
    """PRAGMA user_version is set to 1 on a fresh database."""
    db_path = tmp_path / "library.db"
    conn = init_db(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    assert version == 1


def test_upsert_insert(tmp_path):
    """Single upsert stores all fields correctly; authors round-trips as JSON."""
    db_path = tmp_path / "library.db"
    conn = init_db(db_path)
    row_id = upsert_paper(conn, **_DEFAULT_PAPER)
    rows = list_papers(conn)
    conn.close()

    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == row_id
    assert row["arxiv_id"] == "2301.00001"
    assert row["title"] == "A Great Paper"
    assert json.loads(row["authors"]) == ["Alice Smith", "Bob Jones"]
    assert row["processed_date"] == "2024-01-15T12:00:00Z"
    assert row["source_type"] == "arxiv"
    assert row["output_html_path"] == "/tmp/fake/out.html"
    assert abs(row["cost_spent"] - 0.42) < 1e-9


def test_upsert_no_duplicate_updates_date_and_cost(tmp_path):
    """Same arxiv_id twice → exactly 1 row; date and cost updated, title preserved."""
    db_path = tmp_path / "library.db"
    conn = init_db(db_path)

    upsert_paper(conn, **_DEFAULT_PAPER)
    upsert_paper(
        conn,
        **{
            **_DEFAULT_PAPER,
            "title": "Should Not Overwrite",
            "processed_date": "2024-06-01T00:00:00Z",
            "cost_spent": 1.99,
        },
    )

    rows = list_papers(conn)
    conn.close()

    assert len(rows) == 1
    row = rows[0]
    assert row["processed_date"] == "2024-06-01T00:00:00Z"
    assert abs(row["cost_spent"] - 1.99) < 1e-9
    assert row["title"] == "A Great Paper"  # not overwritten


def test_upsert_null_arxiv_id_allows_multiple(tmp_path):
    """Two inserts with arxiv_id=None both persist (no unique-constraint conflict)."""
    db_path = tmp_path / "library.db"
    conn = init_db(db_path)

    paper_no_arxiv = {**_DEFAULT_PAPER, "arxiv_id": None}
    upsert_paper(conn, **paper_no_arxiv)
    upsert_paper(conn, **paper_no_arxiv)

    rows = list_papers(conn)
    conn.close()

    assert len(rows) == 2


def test_list_papers_empty(tmp_path):
    """list_papers returns [] on a fresh database."""
    conn = init_db(tmp_path / "library.db")
    assert list_papers(conn) == []
    conn.close()


def test_list_papers_returns_rows(tmp_path):
    """Inserting 3 papers returns 3 dicts from list_papers."""
    db_path = tmp_path / "library.db"
    conn = init_db(db_path)

    papers = [
        {
            **_DEFAULT_PAPER,
            "arxiv_id": f"2301.0000{i}",
            "processed_date": f"2024-0{i + 1}-01T00:00:00Z",
        }
        for i in range(3)
    ]
    for p in papers:
        upsert_paper(conn, **p)

    rows = list_papers(conn)
    conn.close()

    assert len(rows) == 3
    assert all(isinstance(r, dict) for r in rows)


def test_get_paper_by_id_hit(tmp_path):
    """get_paper_by_id returns the correct dict for a valid id."""
    db_path = tmp_path / "library.db"
    conn = init_db(db_path)
    row_id = upsert_paper(conn, **_DEFAULT_PAPER)
    row = get_paper_by_id(conn, row_id)
    conn.close()

    assert row is not None
    assert row["id"] == row_id
    assert row["arxiv_id"] == "2301.00001"


def test_get_paper_by_id_miss(tmp_path):
    """get_paper_by_id returns None for a non-existent id."""
    conn = init_db(tmp_path / "library.db")
    result = get_paper_by_id(conn, 99999)
    conn.close()
    assert result is None


def test_record_paper_swallows_exception(tmp_path, monkeypatch, caplog):
    """record_paper catches internal errors and logs a WARNING instead of raising."""
    import paper2html.library.db as db_module

    monkeypatch.setattr(
        db_module, "upsert_paper", MagicMock(side_effect=RuntimeError("boom"))
    )

    import logging

    with caplog.at_level(logging.WARNING, logger="paper2html.library.db"):
        record_paper(tmp_path / "library.db", **_DEFAULT_PAPER)  # must not raise

    assert any("library: failed to record paper" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# CLI tests: list
# ---------------------------------------------------------------------------


def test_list_command_empty(tmp_path):
    """paper2html list on an empty database exits 0 without error."""
    result = _invoke(tmp_path, "list")
    assert result.exit_code == 0


def test_list_command_shows_ok_status(tmp_path):
    """paper2html list shows 'ok' when the HTML file exists."""
    html_file = tmp_path / "out.html"
    html_file.write_text("<html/>")

    _seed(tmp_path, output_html_path=str(html_file))

    result = _invoke(tmp_path, "list")
    assert result.exit_code == 0
    assert "ok" in result.output


def test_list_command_missing_file_shows_status(tmp_path):
    """paper2html list shows 'missing' when the HTML file no longer exists; exit 0."""
    _seed(tmp_path, output_html_path=str(tmp_path / "nonexistent.html"))

    result = _invoke(tmp_path, "list")
    assert result.exit_code == 0
    assert "missing" in result.output


# ---------------------------------------------------------------------------
# CLI tests: open
# ---------------------------------------------------------------------------


def test_open_command_calls_webbrowser(tmp_path, monkeypatch):
    """paper2html open <id> calls webbrowser.open with the file:// URI; exit 0."""
    html_file = tmp_path / "paper.html"
    html_file.write_text("<html/>")

    row_id = _seed(tmp_path, output_html_path=str(html_file))

    calls: list[str] = []

    def fake_open(url: str) -> None:
        calls.append(url)

    monkeypatch.setattr(webbrowser, "open", fake_open)

    result = _invoke(tmp_path, "open", str(row_id))
    assert result.exit_code == 0
    assert len(calls) == 1
    assert calls[0] == html_file.as_uri()


def test_open_command_missing_id_exits_nonzero(tmp_path):
    """paper2html open 99999 on an empty db exits non-zero; output contains the id."""
    result = _invoke(tmp_path, "open", "99999")
    assert result.exit_code != 0
    assert "99999" in result.output
    # No unexpected traceback — only SystemExit allowed as exception
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_open_command_missing_file_exits_nonzero(tmp_path):
    """paper2html open <id> exits non-zero when HTML file is absent."""
    row_id = _seed(tmp_path, output_html_path=str(tmp_path / "gone.html"))

    result = _invoke(tmp_path, "open", str(row_id))
    assert result.exit_code != 0
    assert str(row_id) in result.output or "gone.html" in result.output


# ---------------------------------------------------------------------------
# CLI tests: cache clear
# ---------------------------------------------------------------------------


def test_cache_clear_delegates_and_preserves_index(tmp_path, monkeypatch):
    """cache clear calls clear_cache, prints the eviction count, and does NOT delete index rows."""
    import paper2html.cli as cli_module

    mock_clear = MagicMock(return_value=5)
    monkeypatch.setattr(cli_module, "clear_cache", mock_clear)

    # Seed one library row so we can verify it survives the cache clear.
    html_file = tmp_path / "out.html"
    html_file.write_text("<html/>")
    _seed(tmp_path, output_html_path=str(html_file))

    result = _invoke(tmp_path, "cache", "clear")
    assert result.exit_code == 0
    mock_clear.assert_called_once()
    assert "5" in result.output

    # Library row must still be present.
    conn = init_db(tmp_path / "library.db")
    rows = list_papers(conn)
    conn.close()
    assert len(rows) == 1
