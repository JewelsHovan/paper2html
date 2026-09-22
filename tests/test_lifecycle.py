"""Offline ownership, scoped eviction, and resource failure regressions."""

from contextlib import closing
import sqlite3
from unittest.mock import MagicMock

import diskcache
import httpx
import pymupdf
import pytest
from typer.testing import CliRunner

from paper2html import cache
from paper2html.cli import app
from paper2html.ingestion.pdf import ingest_pdf
from paper2html.ingestion._safety import atomic_write
from paper2html.library import db

PAPER = dict(
    arxiv_id="2301.00001",
    title="Original",
    authors=["Alice"],
    processed_date="2025-01-01",
    source_type="arxiv",
    cost_spent=0.1,
    output_html_path="old.html",
)


def invoke(root, *args):
    return CliRunner().invoke(app, ["--cache-dir", str(root), *args])


def pdf(path):
    with pymupdf.open() as doc:
        doc.new_page().insert_text((50, 50), "Lifecycle test")
        doc.save(path)
    return path


@pytest.mark.parametrize("scope", ["responses", "ingestion", "all"])
def test_real_pdf_scoped_clear_and_regenerate(tmp_path, scope):
    root = tmp_path / "cache"
    root.mkdir()
    source = pdf(root / "input.pdf")
    first = ingest_pdf(source, root)
    pages = first.page_image_paths
    with closing(cache.get_cache(root)) as responses:
        responses["answer"] = "response"
    # User files remain protected even under the generated directory.
    unknown = pages[0].parent / "user.pdf"
    unknown.write_bytes(b"user input")
    output = pages[0].parent / "output.html"
    output.write_text("user output")
    library = root / "library.db"
    db.record_paper(library, **PAPER)
    snapshots = {p: p.read_bytes() for p in (source, unknown, output, library)}
    external = tmp_path / "external"
    external.mkdir()
    target = external / "keep.pdf"
    target.write_bytes(b"keep")
    (root / "ingestion" / "external").symlink_to(external, target_is_directory=True)
    counts = cache.clear_cache_scoped(root, scope=scope)
    assert counts.responses == (scope != "ingestion")
    assert counts.ingestion == (len(pages) if scope != "responses" else 0)
    assert all(p.exists() == (scope == "responses") for p in pages)
    if scope != "responses":
        assert counts.skipped == 3
    assert target.read_bytes() == b"keep"
    assert all(p.read_bytes() == data for p, data in snapshots.items())
    second = ingest_pdf(source, root)
    assert second.page_image_paths == pages
    assert all(p.is_file() for p in pages)
    with closing(cache.get_cache(root)) as responses:
        assert ("answer" in responses) == (scope == "ingestion")
    again = cache.clear_cache_scoped(root, scope=scope)
    assert again.responses == 0
    assert again.ingestion == (len(pages) if scope != "responses" else 0)
    assert all(p.exists() == (scope == "responses") for p in pages)
    assert all(p.read_bytes() == data for p, data in snapshots.items())
    assert target.read_bytes() == b"keep"


@pytest.mark.parametrize("replacement", ["changed", "identical", "symlink", "hardlink"])
def test_registered_replacements_are_protected(tmp_path, replacement):
    dest = tmp_path / "ingestion/downloads/2301.00001.pdf"
    atomic_write(b"original", dest, root=tmp_path)
    other = tmp_path / "user.pdf"
    other.write_bytes(b"original")
    if replacement == "changed":
        dest.write_bytes(b"changed")
    else:
        dest.unlink()
        if replacement == "identical":
            dest.write_bytes(b"original")
        elif replacement == "symlink":
            dest.symlink_to(other)
        else:
            dest.hardlink_to(other)
    counts = cache.clear_cache_scoped(tmp_path, scope="ingestion")
    assert counts.ingestion == 0
    assert counts.skipped == 1
    assert dest.exists() and other.read_bytes() == b"original"


def test_legacy_response_cache_and_unknown_values_survive(tmp_path):
    with closing(diskcache.Cache(str(tmp_path))) as legacy:
        legacy["old"] = "old response"
    with closing(cache.get_cache(tmp_path)) as current:
        assert current.get("old") is None
        current["new"] = b"x" * 100000
    arbitrary = tmp_path / "responses" / "user.val"
    arbitrary.write_bytes(b"user")
    assert cache.clear_cache(tmp_path) == 1
    assert arbitrary.read_bytes() == b"user"
    with closing(diskcache.Cache(str(tmp_path))) as legacy:
        assert legacy["old"] == "old response"


def test_legacy_ingestion_reuse_is_not_adopted(tmp_path):
    source = pdf(tmp_path / "input.pdf")
    pages = ingest_pdf(source, tmp_path).page_image_paths
    (tmp_path / cache._CATALOG).unlink()
    ingest_pdf(source, tmp_path)
    assert cache.clear_cache_scoped(tmp_path, scope="ingestion").ingestion == 0
    assert all(p.exists() for p in pages)


def test_invalid_scope_zero_mutation(tmp_path):
    root = tmp_path / "absent"
    with pytest.raises(ValueError):
        cache.clear_cache(root, scope="everything")
    assert not root.exists()
    result = invoke(root, "cache", "clear", "--scope", "everything")
    assert result.exit_code != 0
    assert not root.exists()


def test_cache_closes_when_clear_fails(tmp_path, monkeypatch):
    fake = MagicMock()
    fake.clear.side_effect = RuntimeError("clear failed")
    monkeypatch.setattr(cache, "get_cache", lambda _: fake)
    with pytest.raises(RuntimeError):
        cache.clear_cache(tmp_path)
    fake.close.assert_called_once()


@pytest.mark.parametrize(
    "component", ["responses", "responses/cache.db", cache._CATALOG]
)
def test_namespace_symlinks_fail_closed(tmp_path, component):
    external = tmp_path / "external"
    external.mkdir()
    target = external if component == "responses" else external / "file"
    if target != external:
        target.write_text("keep")
    root = tmp_path / "cache"
    link = root / component
    link.parent.mkdir(parents=True)
    link.symlink_to(target, target_is_directory=target.is_dir())
    with pytest.raises(ValueError):
        cache.clear_cache_scoped(
            root, scope="ingestion" if component == cache._CATALOG else "responses"
        )
    assert target.exists()
    if target.is_file():
        assert target.read_text() == "keep"


def test_upsert_moves_canonical_output_and_open(tmp_path, monkeypatch):
    a, b = tmp_path / "a.html", tmp_path / "b.html"
    a.write_text("A")
    b.write_text("B")
    with closing(db.init_db(tmp_path / "library.db")) as conn:
        row_id = db.upsert_paper(conn, **{**PAPER, "output_html_path": str(a)})
        updated = db.upsert_paper(
            conn,
            **{**PAPER, "title": "New", "authors": ["Bob"], "output_html_path": str(b)},
        )
        assert updated == row_id
        row = db.get_paper_by_id(conn, row_id)
        assert row["title"] == "Original" and row["authors"] == '["Alice"]'
    a.unlink()
    browser = MagicMock()
    monkeypatch.setattr("paper2html.cli.webbrowser.open", browser)
    assert invoke(tmp_path, "open", str(row_id)).exit_code == 0
    browser.assert_called_once_with(b.as_uri())
    b.unlink()
    result = invoke(tmp_path, "open", str(row_id))
    assert result.exit_code != 0 and "Reprocess" in result.output


def test_future_schema_unchanged_and_connection_closed(tmp_path, monkeypatch):
    path = tmp_path / "library.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 999")
    conn.close()
    owned = sqlite3.connect(path)
    monkeypatch.setattr(db.sqlite3, "connect", lambda _: owned)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="Unsupported library schema version 999"):
        db.init_db(path)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        owned.execute("SELECT 1")
    assert path.read_bytes() == before


def test_schema_failure_rolls_back_and_closes(tmp_path, monkeypatch):
    path = tmp_path / "library.db"
    connect = sqlite3.connect
    owned = connect(path)
    monkeypatch.setattr(db.sqlite3, "connect", lambda _: owned)
    monkeypatch.setattr(db, "_DDL", "CREATE TABLE partial (id INTEGER); INVALID SQL;")
    with pytest.raises(sqlite3.OperationalError):
        db.init_db(path)
    with pytest.raises(sqlite3.ProgrammingError):
        owned.execute("SELECT 1")
    with closing(connect(path)) as check:
        assert check.execute("SELECT name FROM sqlite_master").fetchall() == []
        assert check.execute("PRAGMA user_version").fetchone()[0] == 0


@pytest.mark.parametrize("operation", ["record", "list", "open"])
def test_owned_db_closes_on_failure(tmp_path, monkeypatch, operation):
    import paper2html.cli as cli

    owned = MagicMock()
    fail = MagicMock(side_effect=RuntimeError("injected"))
    if operation == "record":
        monkeypatch.setattr(db, "init_db", lambda _: owned)
        monkeypatch.setattr(db, "upsert_paper", fail)
        db.record_paper(tmp_path / "library.db", **PAPER)
    else:
        monkeypatch.setattr(cli, "init_db", lambda _: owned)
        monkeypatch.setattr(
            cli, "list_papers" if operation == "list" else "get_paper_by_id", fail
        )
        result = invoke(tmp_path, operation, *(["1"] if operation == "open" else []))
        assert result.exit_code != 0
    owned.close.assert_called_once()


def test_http_cleanup_ownership_and_reset(monkeypatch):
    from paper2html.ingestion import _client as module

    injected = httpx.Client()
    try:
        wrapper = module.RateLimitedClient(injected)
        module._reset_singleton(wrapper)
        module.close_client()
        assert not injected.is_closed
        assert module._singleton is None
        fake = MagicMock()
        monkeypatch.setattr(module.httpx, "Client", lambda **kw: fake)
        owned = module.get_client()
        assert owned is not wrapper
        module.close_client()
        fake.close.assert_called_once()
        module.close_client()
    finally:
        injected.close()
        module._reset_singleton()


@pytest.mark.parametrize("source", ["2301.00001", "local.pdf"])
@pytest.mark.parametrize("failure", [False, True])
def test_cli_ingestion_finally_cleanup(tmp_path, monkeypatch, source, failure):
    import paper2html.cli as cli
    from paper2html.ingestion import _client as module

    transport = MagicMock()
    monkeypatch.setattr(module.httpx, "Client", lambda **kw: transport)
    module._reset_singleton()
    if source.endswith(".pdf"):
        source = str(pdf(tmp_path / source))

    def ingestion(*args):
        module.get_client()
        if failure:
            raise RuntimeError("ingestion failure")
        return (
            MagicMock()
        )  # Subsequent model bridge need not succeed for this assertion.

    monkeypatch.setattr(
        cli, "ingest_pdf" if source.endswith(".pdf") else "ingest_arxiv", ingestion
    )
    CliRunner().invoke(
        app,
        [
            "--provider",
            "mock",
            "--cache-dir",
            str(tmp_path),
            "process",
            source,
            "-o",
            str(tmp_path / "out.html"),
        ],
    )
    assert module._singleton is None
    transport.close.assert_called_once()


def test_download_is_registered_on_write_not_unknown_reuse(tmp_path, monkeypatch):
    from paper2html.ingestion import arxiv

    source = pdf(tmp_path / "source.pdf")
    response = httpx.Response(
        200,
        content=source.read_bytes(),
        request=httpx.Request("GET", "https://arxiv.org/pdf/2301.00001"),
    )
    client = MagicMock()
    client.get.return_value = response
    monkeypatch.setattr(arxiv, "get_client", lambda: client)
    root = tmp_path / "cache"
    path = arxiv._download_pdf("2301.00001", root)
    assert arxiv._download_pdf("2301.00001", root) == path
    assert client.get.call_count == 1
    assert cache.clear_cache_scoped(root, scope="ingestion").ingestion == 1
    assert source.exists() and not path.exists()
    arxiv._download_pdf("2301.00001", root)
    assert client.get.call_count == 2


def test_registered_parent_replaced_by_external_symlink(tmp_path):
    root = tmp_path / "cache"
    dest = root / "ingestion/downloads/2301.00001.pdf"
    atomic_write(b"generated", dest, root=root)
    external = tmp_path / "external"
    dest.parent.rename(external)
    dest.parent.symlink_to(external, target_is_directory=True)
    counts = cache.clear_cache_scoped(root, scope="ingestion")
    assert counts.ingestion == 0 and counts.skipped >= 1
    assert (external / dest.name).read_bytes() == b"generated"


@pytest.mark.parametrize("paper_id", ["2301.00001", "hep-th/0101001", "cs.LG/0101001"])
def test_download_id_variants_registered_and_cleared(tmp_path, monkeypatch, paper_id):
    from paper2html.ingestion import arxiv

    source = pdf(tmp_path / "source.pdf")
    client = MagicMock()
    client.get.return_value = httpx.Response(
        200,
        content=source.read_bytes(),
        request=httpx.Request("GET", f"https://arxiv.org/pdf/{paper_id}"),
    )
    monkeypatch.setattr(arxiv, "get_client", lambda: client)
    root = tmp_path / "cache"
    for calls in (1, 2):
        path = arxiv._download_pdf(paper_id, root)
        assert path.name == paper_id.replace("/", "_") + ".pdf"
        assert client.get.call_count == calls
        with closing(sqlite3.connect(root / cache._CATALOG)) as conn:
            assert conn.execute("SELECT path FROM artifacts").fetchall() == [
                (f"ingestion/downloads/{path.name}",)
            ]
        assert cache.clear_cache_scoped(root, scope="ingestion").ingestion == 1
        assert not path.exists() and source.exists()


def test_cli_symlink_response_database_reports_refusal(tmp_path):
    target = tmp_path / "user.db"
    target.write_bytes(b"keep")
    responses = tmp_path / "responses"
    responses.mkdir()
    (responses / "cache.db").symlink_to(target)
    result = invoke(tmp_path, "cache", "clear")
    assert result.exit_code == 1
    assert "Cache error:" in result.output and "Symlink" in result.output
    assert "--cache-dir" in result.output
    assert target.read_bytes() == b"keep"


@pytest.mark.parametrize("command", [("list",), ("open", "1")])
def test_cli_future_library_schema_reports_refusal(tmp_path, command):
    path = tmp_path / "library.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA user_version = 99")
    before = path.read_bytes()
    result = invoke(tmp_path, *command)
    assert result.exit_code == 1
    assert "Library error:" in result.output and "schema version 99" in result.output
    assert "--cache-dir" in result.output
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "error",
    [
        sqlite3.OperationalError("database is locked"),
        PermissionError("read-only catalog"),
    ],
)
def test_registration_metadata_failure_keeps_usable_unregistered_pdf(
    tmp_path, monkeypatch, caplog, error
):
    from paper2html.ingestion import arxiv

    source = pdf(tmp_path / "source.pdf")
    client = MagicMock()
    client.get.return_value = httpx.Response(
        200,
        content=source.read_bytes(),
        request=httpx.Request("GET", "https://arxiv.org/pdf/2301.00001"),
    )
    monkeypatch.setattr(arxiv, "get_client", lambda: client)

    def fail(*args):
        raise error

    root = tmp_path / "cache"
    with monkeypatch.context() as injected:
        injected.setattr(cache, "register_ingestion_artifact", fail)
        path = arxiv._download_pdf("2301.00001", root)
    assert "not registered" in caplog.text
    assert "retained" in caplog.text
    assert path.read_bytes() == source.read_bytes()
    with pymupdf.open(path) as document:
        assert document.page_count == 1
    assert arxiv._download_pdf("2301.00001", root) == path
    assert client.get.call_count == 1
    assert cache.clear_cache_scoped(root, scope="ingestion").ingestion == 0
    assert path.exists() and source.exists()


def test_registration_security_valueerror_not_suppressed(tmp_path, monkeypatch):
    def unsafe(*args):
        raise ValueError("Unsafe registry path")

    monkeypatch.setattr(cache, "register_ingestion_artifact", unsafe)
    with pytest.raises(ValueError, match="Unsafe registry path"):
        atomic_write(
            b"content", tmp_path / "ingestion/downloads/2301.00001.pdf", root=tmp_path
        )


@pytest.mark.parametrize(
    "command,hook",
    [
        (("cache", "clear"), "clear_cache"),
        (("cache", "clear", "--scope", "ingestion"), "clear_cache_scoped"),
        (("list",), "init_db"),
        (("open", "1"), "init_db"),
    ],
)
@pytest.mark.parametrize(
    "error",
    [PermissionError("permission denied"), sqlite3.OperationalError("database locked")],
)
def test_cli_storage_errors_are_actionable(tmp_path, monkeypatch, command, hook, error):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(f"paper2html.cli.{hook}", fail)
    result = invoke(tmp_path, *command)
    assert result.exit_code == 1
    assert str(error) in result.output and "--cache-dir" in result.output
    assert "Traceback" not in result.output
