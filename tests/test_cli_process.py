"""CLI e2e tests for `paper2html process`.

All tests are SYNC (CliRunner.invoke is synchronous) — no asyncio marks.
Per KG constraint #S99: per-function @pytest.mark.asyncio only on async tests;
CLI tests here are all sync, so no marks at all.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pymupdf
import pytest
from bs4 import BeautifulSoup
from typer.testing import CliRunner

from paper2html.cli import app
from paper2html.pipeline.errors import BudgetExceededError
from paper2html.pipeline.schemas import PipelineResult, SectionFailure

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fixture_pdf(tmp_path: Path) -> Path:
    """Create a minimal single-page PDF with text containing MockProvider terms.

    MockProvider synthesis always returns term_a + term_b (from _synthesis_response).
    Worker for section "sec-1" returns term_sec-1 (from _worker_response("sec-1")).
    The text must literally contain these terms for .term-def span injection (KG #S91).
    """
    pdf_path = tmp_path / "fixture.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text(
        (72, 72),
        (
            "This paper introduces term_a and term_b as key concepts. "
            "The method term_sec-1 is described here."
        ),
    )
    doc.save(str(pdf_path))
    doc.close()
    return pdf_path


def _invoke(tmp_path: Path, *args: str):
    """Invoke the CLI with --cache-dir pointing at tmp_path."""
    env = {"COLUMNS": "200", "TERM": "dumb"}
    return runner.invoke(app, ["--cache-dir", str(tmp_path), *args], env=env)


# Minimal valid 1x1 PNG.
_PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc````"
    b"\x00\x00\x00\x05\x00\x01\xa5\xf6E@\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _make_arxiv_ingest_result():
    """An ArxivIngestResult whose figure carries raw_src + asset_bytes.

    Reproduces the real arXiv shape: the raw_html <img src> matches the
    figure's raw_src, so the CLI's figure_assets bridge must embed the bytes.
    """
    from paper2html.ingestion._types import (
        ArxivIngestResult,
        ArxivMetadata,
        Figure,
        Section,
    )

    # Wrap sections in div/article exactly as real arXiv (LaTeXML) does, so
    # this exercises the div-wrapper section extraction end-to-end through the CLI.
    raw_html = (
        "<html><body><header>nav</header>"
        '<div class="ltx_page_main"><div class="ltx_page_content"><article>'
        "<section><h2>Intro</h2>"
        "<p>This paper introduces term_a and term_b as key concepts. "
        "The method term_sec-1 is described here.</p>"
        '<figure id="fig-1"><img src="v1/figs/a.png"/>'
        "<figcaption>A figure</figcaption></figure>"
        "</section>"
        "</article></div></div></body></html>"
    )
    fig = Figure(caption="A figure", asset_bytes=_PNG_1X1, raw_src="v1/figs/a.png")
    section = Section(
        heading_level=2, heading_text="Intro", body_html="", figures=[fig]
    )
    return ArxivIngestResult(
        metadata=ArxivMetadata(arxiv_id="1234.56789", title="Test", authors=["A"]),
        sections=[section],
        raw_html=raw_html,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_mock_process_exits_zero(tmp_path):
    """SC-1: mock process exits 0."""
    pdf = _make_fixture_pdf(tmp_path)
    out = tmp_path / "out.html"
    result = _invoke(
        tmp_path, "--provider", "mock", "process", str(pdf), "-o", str(out)
    )
    assert result.exit_code == 0, (
        f"Expected exit 0; got {result.exit_code}\nOutput:\n{result.output}"
    )


def test_mock_process_html_written(tmp_path):
    """SC-1: HTML file is written and non-empty."""
    pdf = _make_fixture_pdf(tmp_path)
    out = tmp_path / "out.html"
    _invoke(tmp_path, "--provider", "mock", "process", str(pdf), "-o", str(out))
    assert out.exists(), f"Expected {out} to exist"
    assert out.stat().st_size > 0, "Expected non-empty HTML file"


def test_arxiv_figure_bytes_embedded_in_html(tmp_path):
    """Regression: arXiv figures fetched by ingestion must reach the rendered
    HTML as embedded base64 (data:image). Guards the CLI figure_assets bridge —
    from_arxiv_html was previously called without figure_assets, silently
    dropping every figure even after the URL-resolution fix.
    """
    out = tmp_path / "out.html"
    with patch("paper2html.cli.ingest_arxiv", return_value=_make_arxiv_ingest_result()):
        result = _invoke(
            tmp_path, "--provider", "mock", "process", "1234.56789", "-o", str(out)
        )
    assert result.exit_code == 0, (
        f"Expected exit 0; got {result.exit_code}\nOutput:\n{result.output}"
    )
    html = out.read_text(encoding="utf-8")
    assert "data:image" in html, (
        "Expected the fetched figure's bytes embedded as a data:image URI; "
        "the figure_assets bridge in cli.py likely dropped them."
    )
    # The section (nested under div/article) must have been parsed and analyzed,
    # not silently dropped — a dropped section yields a 0% bar and abstract-only
    # output. A .term-def span only appears if a worker analyzed the section.
    assert "term-def" in html, (
        "Expected the div-wrapped section to be parsed and analyzed; "
        "from_arxiv_html likely found 0 sections (div-wrapper regression)."
    )


def test_mock_process_term_def_spans(tmp_path):
    """SC-1: Output HTML contains .term-def spans (from synthesis term_a/term_b)."""
    pdf = _make_fixture_pdf(tmp_path)
    out = tmp_path / "out.html"
    _invoke(tmp_path, "--provider", "mock", "process", str(pdf), "-o", str(out))
    soup = BeautifulSoup(out.read_text(encoding="utf-8"), "html.parser")
    spans = soup.find_all(class_="term-def")
    assert len(spans) > 0, (
        "Expected at least one .term-def span in rendered HTML. "
        f"term_definitions requires the section text to literally contain the term (KG #S91). "
        f"Spans found: {len(spans)}"
    )


def test_mock_process_summary_block(tmp_path):
    """SC-1: Output HTML contains .summary-block elements."""
    pdf = _make_fixture_pdf(tmp_path)
    out = tmp_path / "out.html"
    _invoke(tmp_path, "--provider", "mock", "process", str(pdf), "-o", str(out))
    soup = BeautifulSoup(out.read_text(encoding="utf-8"), "html.parser")
    blocks = soup.find_all(class_="summary-block")
    assert len(blocks) > 0, (
        "Expected at least one .summary-block element in rendered HTML"
    )


def test_mock_process_margin_note(tmp_path):
    """SC-1: Output HTML contains aside.margin-note elements."""
    pdf = _make_fixture_pdf(tmp_path)
    out = tmp_path / "out.html"
    _invoke(tmp_path, "--provider", "mock", "process", str(pdf), "-o", str(out))
    soup = BeautifulSoup(out.read_text(encoding="utf-8"), "html.parser")
    notes = soup.find_all("aside", class_="margin-note")
    assert len(notes) > 0, (
        "Expected at least one aside.margin-note element in rendered HTML"
    )


def test_mock_process_paper_appears_in_list(tmp_path):
    """After process, the paper appears in `paper2html list`."""
    pdf = _make_fixture_pdf(tmp_path)
    out = tmp_path / "out.html"
    _invoke(tmp_path, "--provider", "mock", "process", str(pdf), "-o", str(out))
    list_result = _invoke(tmp_path, "list")
    assert list_result.exit_code == 0, f"list failed: {list_result.output}"
    assert "fixture" in list_result.output.lower() or str(out) in list_result.output, (
        f"Expected processed paper in list output; got:\n{list_result.output}"
    )


def test_mock_process_no_api_key_required(tmp_path, monkeypatch):
    """--provider mock requires no API key env vars."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    pdf = _make_fixture_pdf(tmp_path)
    out = tmp_path / "out.html"
    result = _invoke(
        tmp_path, "--provider", "mock", "process", str(pdf), "-o", str(out)
    )
    assert result.exit_code == 0, (
        f"Expected exit 0 with mock provider (no API keys); got {result.exit_code}\n"
        f"Output:\n{result.output}"
    )


def test_budget_exceeded_error(tmp_path, monkeypatch):
    """BudgetExceededError → clear one-line budget error, exit 1, no traceback."""
    pdf = _make_fixture_pdf(tmp_path)
    out = tmp_path / "out.html"

    def _raise_budget(*args, **kwargs):
        raise BudgetExceededError(current_usd=0.05, projected_usd=0.10, limit_usd=0.10)

    monkeypatch.setattr("paper2html.cli.run_pipeline", _raise_budget)
    result = _invoke(
        tmp_path, "--provider", "mock", "process", str(pdf), "-o", str(out)
    )
    assert result.exit_code != 0, "Expected non-zero exit on BudgetExceededError"
    assert "udget" in result.output, (
        f"Expected 'Budget' in output; got:\n{result.output}"
    )
    assert "Traceback" not in result.output, (
        f"Expected no traceback in output; got:\n{result.output}"
    )


def test_failed_sections_warning_and_html_written(tmp_path, monkeypatch):
    """failed_sections non-empty → warning in output AND HTML file still written."""

    pdf = _make_fixture_pdf(tmp_path)
    out = tmp_path / "out.html"

    def _partial_result(*args, **kwargs):
        return PipelineResult(
            doc_id="test",
            provider="mock",
            orchestrator_model="mock",
            worker_model="mock",
            failed_sections=[SectionFailure(section_id="sec-1", error="boom")],
        )

    monkeypatch.setattr("paper2html.cli.run_pipeline", _partial_result)
    result = _invoke(
        tmp_path, "--provider", "mock", "process", str(pdf), "-o", str(out)
    )
    # Warning should be in output
    assert "Warning" in result.output or "failed" in result.output.lower(), (
        f"Expected warning about failed sections; got:\n{result.output}"
    )
    # HTML should still be written (render from successful sections = empty)
    assert out.exists(), "Expected HTML to be written even with failed sections"


def test_claude_missing_api_key(tmp_path, monkeypatch):
    """--provider claude with ANTHROPIC_API_KEY missing → exit 1, key name in output."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    pdf = _make_fixture_pdf(tmp_path)
    out = tmp_path / "out.html"
    result = _invoke(
        tmp_path, "--provider", "claude", "process", str(pdf), "-o", str(out)
    )
    assert result.exit_code != 0, (
        "Expected non-zero exit when ANTHROPIC_API_KEY missing"
    )
    assert "ANTHROPIC_API_KEY" in result.output, (
        f"Expected 'ANTHROPIC_API_KEY' in output; got:\n{result.output}"
    )
    assert "Traceback" not in result.output, (
        f"Expected no traceback; got:\n{result.output}"
    )


def test_openai_missing_api_key(tmp_path, monkeypatch):
    """--provider openai with OPENAI_API_KEY missing → exit 1, key name in output."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    pdf = _make_fixture_pdf(tmp_path)
    out = tmp_path / "out.html"
    result = _invoke(
        tmp_path, "--provider", "openai", "process", str(pdf), "-o", str(out)
    )
    assert result.exit_code != 0, "Expected non-zero exit when OPENAI_API_KEY missing"
    assert "OPENAI_API_KEY" in result.output, (
        f"Expected 'OPENAI_API_KEY' in output; got:\n{result.output}"
    )
    assert "Traceback" not in result.output, (
        f"Expected no traceback; got:\n{result.output}"
    )


@pytest.mark.parametrize("alias", ["identical", "relative", "symlink", "hardlink"])
@pytest.mark.parametrize("overwrite", [False, True])
def test_source_alias_rejected_before_work(tmp_path, monkeypatch, alias, overwrite):
    pdf = _make_fixture_pdf(tmp_path)
    original = pdf.read_bytes()
    out = pdf
    if alias == "relative":
        monkeypatch.chdir(tmp_path)
        out = Path("fixture.pdf")
    elif alias == "symlink":
        out = tmp_path / "alias.html"
        out.symlink_to(pdf)
    elif alias == "hardlink":
        out = tmp_path / "alias.html"
        out.hardlink_to(pdf)
    with (
        patch("paper2html.cli.build_provider") as provider,
        patch("paper2html.cli.ingest_pdf") as ingest,
    ):
        result = _invoke(
            tmp_path,
            "--provider",
            "mock",
            "process",
            str(pdf),
            "-o",
            str(out),
            *(["--overwrite"] if overwrite else []),
        )
    assert result.exit_code == 1
    assert "same file" in result.output
    provider.assert_not_called()
    ingest.assert_not_called()
    assert pdf.read_bytes() == original


@pytest.mark.parametrize("default", [False, True])
def test_existing_output_requires_consent(tmp_path, monkeypatch, default):
    monkeypatch.chdir(tmp_path)
    pdf = _make_fixture_pdf(tmp_path)
    out = tmp_path / "fixture.html"
    out.write_text("original")
    with (
        patch("paper2html.cli.build_provider") as provider,
        patch("paper2html.cli.ingest_pdf") as ingest,
    ):
        result = _invoke(
            tmp_path,
            "--provider",
            "mock",
            "process",
            str(pdf),
            *([] if default else ["-o", str(out)]),
        )
    assert result.exit_code == 1
    assert "--overwrite" in result.output
    provider.assert_not_called()
    ingest.assert_not_called()
    assert out.read_text() == "original"


def test_explicit_overwrite_succeeds(tmp_path):
    pdf = _make_fixture_pdf(tmp_path)
    original = pdf.read_bytes()
    out = tmp_path / "out.html"
    out.write_text("original")
    result = _invoke(
        tmp_path,
        "--provider",
        "mock",
        "process",
        str(pdf),
        "-o",
        str(out),
        "--overwrite",
    )
    assert result.exit_code == 0, result.output
    assert "<html" in out.read_text()
    assert pdf.read_bytes() == original


@pytest.mark.parametrize(
    "provider,models",
    [
        ("claude", ("claude-opus-4-8", "claude-haiku-4-5")),
        ("openai", ("gpt-4o", "gpt-4o-mini")),
        ("mock", ("mock", "mock")),
    ],
)
@pytest.mark.parametrize("override", [False, True])
def test_pipeline_provider_models(tmp_path, provider, models, override, monkeypatch):
    # Provider construction and the pipeline are mocked below. Supply explicit
    # inert keys for CLI preflight rather than relying on the developer's shell.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-test-not-a-real-key")
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-not-a-real-key")
    pdf = _make_fixture_pdf(tmp_path)
    flags = (
        ["--model", "custom-orchestrator", "--worker-model", "custom-worker"]
        if override
        else []
    )
    with (
        patch("paper2html.cli.build_provider"),
        patch("paper2html.cli.run_pipeline") as pipeline,
    ):
        pipeline.return_value = PipelineResult(
            doc_id="test",
            provider=provider,
            orchestrator_model=models[0],
            worker_model=models[1],
        )
        result = _invoke(
            tmp_path,
            "--provider",
            provider,
            *flags,
            "process",
            str(pdf),
            "-o",
            str(tmp_path / "out.html"),
        )
    assert result.exit_code == 0, result.output
    config = pipeline.call_args.args[1]
    assert (config.orchestrator_model, config.worker_model) == (
        ("custom-orchestrator", "custom-worker") if override else models
    )


@pytest.mark.parametrize(
    "flags", [["--provider", "unknown"], ["--model", "  "], ["--worker-model", "  "]]
)
def test_invalid_config_fails_before_work(tmp_path, flags):
    with (
        patch("paper2html.cli.build_provider") as provider,
        patch("paper2html.cli.ingest_arxiv") as ingest,
    ):
        result = _invoke(tmp_path, *flags, "process", "1234.56789")
    assert result.exit_code == 1
    assert "Configuration error" in result.output
    provider.assert_not_called()
    ingest.assert_not_called()


def test_arxiv_default_collision_checked_before_ingestion(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "hep-th_9901001.html"
    out.write_text("original")
    with (
        patch("paper2html.cli.build_provider") as provider,
        patch("paper2html.cli.ingest_arxiv") as ingest,
    ):
        result = _invoke(
            tmp_path,
            "--provider",
            "mock",
            "process",
            "https://arxiv.org/abs/hep-th/9901001v2",
        )
    assert result.exit_code == 1
    provider.assert_not_called()
    ingest.assert_not_called()
    assert out.read_text() == "original"


def test_pipeline_limit_options_forwarded(tmp_path):
    pdf = _make_fixture_pdf(tmp_path)
    from paper2html.pipeline.runner import run_pipeline as real_run

    with patch("paper2html.cli.run_pipeline", wraps=real_run) as run:
        result = _invoke(
            tmp_path,
            "--provider",
            "mock",
            "--max-cost-usd",
            "5",
            "--max-concurrency",
            "2",
            "--max-output-tokens",
            "123",
            "--request-timeout-seconds",
            "180",
            "process",
            str(pdf),
            "-o",
            str(tmp_path / "result.html"),
        )
    assert result.exit_code == 0, result.output
    config = run.call_args.args[1]
    assert (
        config.max_cost_usd,
        config.max_concurrency,
        config.max_output_tokens,
        config.request_timeout_seconds,
    ) == (5, 2, 123, 180)


def test_unknown_capped_model_before_ingestion_or_provider(tmp_path):
    with (
        patch("paper2html.cli.ingest_arxiv") as ingest,
        patch("paper2html.cli.build_provider") as build,
    ):
        result = _invoke(
            tmp_path,
            "--provider",
            "openai",
            "--model",
            "unknown",
            "--max-cost-usd",
            "5",
            "process",
            "1234.56789",
            "-o",
            str(tmp_path / "result.html"),
        )
    assert result.exit_code != 0 and "Unknown capped pricing" in result.output
    ingest.assert_not_called()
    build.assert_not_called()


def test_ingestion_failure_never_constructs_provider(tmp_path):
    with (
        patch(
            "paper2html.cli.ingest_arxiv", side_effect=ValueError("ingestion failed")
        ),
        patch("paper2html.cli.build_provider") as build,
    ):
        result = _invoke(
            tmp_path,
            "--provider",
            "mock",
            "process",
            "1234.56789",
            "-o",
            str(tmp_path / "result.html"),
        )
    assert result.exit_code != 0
    build.assert_not_called()


def test_missing_local_pdf_message(tmp_path):
    with patch("paper2html.cli.ingest_arxiv") as ingest:
        result = _invoke(
            tmp_path, "--provider", "mock", "process", str(tmp_path / "missing.pdf")
        )
    assert result.exit_code != 0 and "Local PDF does not exist" in result.output
    ingest.assert_not_called()


def test_structured_arxiv_ingestion_warnings_visible(tmp_path):
    ingested = _make_arxiv_ingest_result()
    ingested.ingestion_warnings.append("asset [bad] timed out; skipped")
    with patch("paper2html.cli.ingest_arxiv", return_value=ingested):
        result = _invoke(
            tmp_path,
            "--provider",
            "mock",
            "process",
            "1234.56789",
            "-o",
            str(tmp_path / "out.html"),
        )
    assert result.exit_code == 0, result.output
    assert "Ingestion warning: asset [bad] timed out; skipped" in result.output


def test_structured_pdf_ingestion_warnings_visible(tmp_path):
    from paper2html.ingestion.pdf import ingest_pdf as real_ingest

    source = _make_fixture_pdf(tmp_path)

    def ingestion(path, cache):
        ingested = real_ingest(path, cache)
        ingested.ingestion_warnings.append("figure xref 12: unsupported format")
        return ingested

    with patch("paper2html.cli.ingest_pdf", side_effect=ingestion):
        result = _invoke(
            tmp_path,
            "--provider",
            "mock",
            "process",
            str(source),
            "-o",
            str(tmp_path / "out.html"),
        )
    assert result.exit_code == 0, result.output
    assert "Ingestion warning: figure xref 12: unsupported format" in result.output
