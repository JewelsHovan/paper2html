from __future__ import annotations

from contextlib import closing
import json
import sqlite3
import types
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

import pymupdf
import rich.box
import typer
from bs4 import BeautifulSoup
from rich.console import Console
from rich.table import Table

from paper2html.cache import clear_cache, clear_cache_scoped
from paper2html.ingestion._client import close_client
from paper2html.config import load_config
from paper2html.ingestion import normalise_id
from paper2html.ingestion import PdfFallbackResult
from paper2html.ingestion.arxiv import ingest_arxiv
from paper2html.ingestion.pdf import PdfIngestionResult, ingest_pdf
from paper2html.library import get_paper_by_id, init_db, list_papers, record_paper
from paper2html.models.document import from_arxiv_html, from_pdf
from paper2html.pipeline.errors import BudgetExceededError, PipelineError
from paper2html.pipeline.providers import (
    AnthropicProvider,
    MockProvider,
    OpenAIProvider,
)
from paper2html.pipeline.runner import run_pipeline
from paper2html.pipeline.schemas import PipelineConfig
from paper2html.renderer import render_html
from paper2html.review.glue import attach_reading_aids
from paper2html.review.schemas import AnnotationBundle
from paper2html.video.cli import app as video_app

app = typer.Typer()
app.add_typer(video_app, name="video")
console = Console()


@app.callback()
def main(
    ctx: typer.Context,
    provider: str = typer.Option(
        "claude", help="LLM provider: claude, openai, or mock"
    ),
    model: str = typer.Option("", help="Model name (defaults to provider default)"),
    worker_model: str = typer.Option(
        "", help="Worker model name (defaults to provider default)"
    ),
    cache_dir: str = typer.Option("", help="Cache directory override"),
    max_cost_usd: float | None = typer.Option(
        None, help="Estimated pipeline budget in USD (not an invoice guarantee)"
    ),
    max_concurrency: int = typer.Option(
        4, min=1, help="Maximum concurrent pipeline calls"
    ),
    request_timeout_seconds: float = typer.Option(
        60.0, help="Per-attempt provider timeout in seconds"
    ),
    max_output_tokens: int = typer.Option(
        8192, min=1, help="Output token limit per pipeline call"
    ),
) -> None:
    ctx.ensure_object(dict)
    try:
        ctx.obj["pipeline_limits"] = PipelineConfig(
            max_cost_usd=max_cost_usd,
            max_concurrency=max_concurrency,
            max_output_tokens=max_output_tokens,
            request_timeout_seconds=request_timeout_seconds,
        )
        ctx.obj["config"] = load_config(
            provider=provider,
            model=model,
            cache_dir=cache_dir,
            worker_model=worker_model,
        )
    except ValueError as exc:
        console.print(f"[red]Configuration error: {exc}[/red]")
        raise typer.Exit(code=1)


def build_provider(config):
    """Construct the appropriate provider instance from config.

    Args:
        config: Config object with .provider, .anthropic_api_key, .openai_api_key fields.

    Returns:
        A ProviderInterface instance.

    Raises:
        typer.Exit(1): When a required API key is missing or provider is unknown.
    """
    provider_name = config.provider

    if provider_name == "mock":
        return MockProvider()

    if provider_name == "claude":
        if not config.anthropic_api_key:
            console.print("[red]Error: ANTHROPIC_API_KEY is not set.[/red]")
            raise typer.Exit(code=1)
        return AnthropicProvider(api_key=config.anthropic_api_key)

    if provider_name == "openai":
        if not config.openai_api_key:
            console.print("[red]Error: OPENAI_API_KEY is not set.[/red]")
            raise typer.Exit(code=1)
        return OpenAIProvider(api_key=config.openai_api_key)

    console.print(
        f"[red]Error: Unknown provider '{provider_name}'. "
        "Choose one of: claude, openai, mock[/red]"
    )
    raise typer.Exit(code=1)


def _preflight_output(source: Path, output: Path, overwrite: bool) -> None:
    """Reject source aliases and collisions before any paid or ingestion work."""
    if output.resolve() == source.resolve() or (
        source.exists() and output.exists() and output.samefile(source)
    ):
        raise ValueError("Output must not be the same file as the source")
    if output.exists() or output.is_symlink():
        if not overwrite:
            raise ValueError(
                f"Output already exists: {output}. Use --overwrite to replace it"
            )
        if output.exists() and not output.is_file():
            raise ValueError(f"Output is not a regular file: {output}")
    if not output.parent.is_dir():
        raise ValueError(f"Output directory does not exist: {output.parent}")


@app.command()
def process(
    ctx: typer.Context,
    source: str,
    output: str = typer.Option(
        "",
        "--output",
        "-o",
        help="Output HTML path (default: <source stem>.html in current directory; refuses collisions)",
    ),
    source_only: bool = typer.Option(
        False,
        "--source-only",
        help="Preserve source content without any AI provider or annotations",
    ),
    overwrite: bool = typer.Option(
        False, "--overwrite", help="Replace an existing output, never the source"
    ),
) -> None:
    """Process a paper from an arXiv URL/ID or local PDF path."""
    config = ctx.obj["config"]
    pipeline_config = PipelineConfig(
        orchestrator_model=config.model,
        worker_model=config.worker_model,
        request_timeout_seconds=ctx.obj["pipeline_limits"].request_timeout_seconds,
        max_cost_usd=ctx.obj["pipeline_limits"].max_cost_usd,
        max_concurrency=ctx.obj["pipeline_limits"].max_concurrency,
        max_output_tokens=ctx.obj["pipeline_limits"].max_output_tokens,
        provider_name=config.provider,
    )

    from paper2html.pipeline.budget import COST_TABLE

    if (
        not source_only
        and config.provider != "mock"
        and pipeline_config.max_cost_usd is not None
    ):
        unknown = [
            model
            for model in (config.model, config.worker_model)
            if model not in COST_TABLE
        ]
        if unknown:
            console.print(f"[red]Unknown capped pricing for model: {unknown[0]}[/red]")
            raise typer.Exit(code=1)
    if not source_only and config.provider in {"claude", "openai"}:
        key = (
            config.anthropic_api_key
            if config.provider == "claude"
            else config.openai_api_key
        )
        if not key:
            name = (
                "ANTHROPIC_API_KEY" if config.provider == "claude" else "OPENAI_API_KEY"
            )
            console.print(f"[red]Error: {name} is not set.[/red]")
            raise typer.Exit(code=1)
    p = Path(source)
    is_local_pdf = p.suffix.lower() == ".pdf" and p.exists()
    try:
        if p.suffix.lower() == ".pdf" and not p.exists() and "://" not in source:
            raise ValueError(f"Local PDF does not exist: {p}")
        doc_stem = p.stem if is_local_pdf else normalise_id(source).replace("/", "_")
        out_path = Path(output) if output else Path(f"{doc_stem}.html")
        _preflight_output(p, out_path, overwrite)
    except (ValueError, OSError, RuntimeError) as exc:
        console.print(f"[red]Output/source error: {exc}[/red]")
        raise typer.Exit(code=1)

    # Source detection and ingestion
    arxiv_id_for_record: str | None = None
    title_for_record: str = ""
    authors_for_record: list[str] = []
    source_type: str = "pdf"

    try:
        if is_local_pdf:
            # PDF path branch
            try:
                pdf_result = (
                    ingest_pdf(p, config.cache_dir, extract_figures=False)
                    if source_only
                    else ingest_pdf(p, config.cache_dir)
                )
            except Exception as exc:
                console.print(f"[red]Ingestion error: {exc}[/red]")
                raise typer.Exit(code=1)
            finally:
                close_client()

            for warning in pdf_result.ingestion_warnings:
                console.print(
                    f"Ingestion warning: {warning}", style="yellow", markup=False
                )

            mupdf_doc = pymupdf.open(str(pdf_result.pdf_path))
            try:
                doc = from_pdf(
                    mupdf_doc,
                    pdf_result.pdf_path,
                    page_assets=_page_assets(pdf_result),
                    extract_images=not source_only,
                )
            finally:
                mupdf_doc.close()

            source_type = "pdf"
            title_for_record = doc.metadata.title or p.name
            authors_for_record = list(doc.metadata.authors or [])

        else:
            # arXiv ID/URL branch
            try:
                arxiv_result = ingest_arxiv(source, config.cache_dir)
            except Exception as exc:
                console.print(f"[red]Ingestion error: {exc}[/red]")
                raise typer.Exit(code=1)
            finally:
                close_client()

            for warning in getattr(arxiv_result, "ingestion_warnings", []):
                console.print(
                    f"Ingestion warning: {warning}", style="yellow", markup=False
                )

            m = arxiv_result.metadata
            # Duck-type bridge: from_arxiv_html reads .entry_id/.summary but
            # ArxivMetadata exposes .arxiv_id/.abstract
            meta_ns = types.SimpleNamespace(
                entry_id=m.arxiv_id,
                title=m.title,
                authors=m.authors,
                summary=m.abstract,
            )

            if isinstance(arxiv_result, PdfFallbackResult):
                pdf_result = (
                    ingest_pdf(
                        arxiv_result.pdf_path, config.cache_dir, extract_figures=False
                    )
                    if source_only
                    else ingest_pdf(arxiv_result.pdf_path, config.cache_dir)
                )
                mupdf_doc = pymupdf.open(str(arxiv_result.pdf_path))
                try:
                    doc = from_pdf(
                        mupdf_doc,
                        arxiv_result.pdf_path,
                        meta_ns,
                        page_assets=_page_assets(pdf_result),
                        extract_images=not source_only,
                    )
                finally:
                    mupdf_doc.close()
                source_type = "pdf"
            else:
                soup = BeautifulSoup(arxiv_result.raw_html, "html.parser")
                # Bridge fetched figure bytes into the normalized doc: from_arxiv_html
                # keys figure_assets by the RAW <img src> attribute, which ingestion
                # now retains on each Figure as raw_src.
                figure_assets = {
                    fig.raw_src: fig.asset_bytes
                    for fig in arxiv_result.figures
                    if fig.raw_src and fig.asset_bytes
                }
                doc = from_arxiv_html(soup, meta_ns, figure_assets=figure_assets)
                source_type = "arxiv"

            arxiv_id_for_record = m.arxiv_id
            title_for_record = m.title or ""
            authors_for_record = list(m.authors or [])

    except typer.Exit:
        raise
    except Exception as exc:
        console.print(
            f"Source error: {str(exc).replace(chr(10), ' ')}", style="red", markup=False
        )
        raise typer.Exit(code=1) from exc

    # Fail closed before any potentially paid provider work. Public rendering
    # independently reconstructs/validates again; no trusted-markup bypass.
    from paper2html.source_view import render_source

    try:
        render_source(doc)
    except (ValueError, OSError, RuntimeError, SyntaxError) as exc:
        console.print(
            f"Source preflight error: {str(exc).replace(chr(10), ' ')}",
            style="red",
            markup=False,
        )
        raise typer.Exit(code=1) from exc

    for warning in doc.ingestion_warnings:
        console.print(f"Document warning: {warning}", style="yellow", markup=False)

    result = None
    bundle = AnnotationBundle(items=[])
    if not source_only:
        provider = build_provider(config)

        # Run pipeline
        try:
            result = run_pipeline(
                doc,
                pipeline_config,
                provider,
                cache_dir=config.cache_dir,
                console=console,
            )
        except BudgetExceededError as exc:
            console.print(f"[red]Budget exceeded: {exc}[/red]")
            raise typer.Exit(code=1)
        except (PipelineError, ValueError) as exc:
            console.print(f"[red]Pipeline error: {exc}[/red]")
            raise typer.Exit(code=1)

        # Surface stage failures as well as section failures.
        for warning in result.warnings:
            console.print(f"[yellow]Warning: {warning}[/yellow]")

        # Surface partial failures
        if result.failed_sections:
            failed_ids = ", ".join(fs.section_id for fs in result.failed_sections)
            console.print(
                f"[yellow]Warning: {len(result.failed_sections)} section(s) failed: "
                f"{failed_ids}[/yellow]"
            )

        # Glue + render
        bundle = attach_reading_aids(AnnotationBundle(items=[]), result)
    try:
        _preflight_output(p, out_path, overwrite)
        render_html(doc, bundle, out_path, overwrite=overwrite)
    except (ValueError, OSError, RuntimeError) as exc:
        console.print(f"[red]Output error: {exc}[/red]")
        raise typer.Exit(code=1)
    console.print(f"[green]HTML written to {out_path}[/green]")

    # Record in library (fire-and-forget)
    db_path = config.cache_dir / "library.db"
    record_paper(
        db_path,
        arxiv_id=arxiv_id_for_record,
        title=title_for_record,
        authors=authors_for_record,
        processed_date=datetime.now(tz=timezone.utc).isoformat(),
        source_type=source_type,
        output_html_path=str(out_path.resolve()),
        cost_spent=result.estimated_cost_usd if result is not None else 0.0,
    )


def _page_assets(result: PdfIngestionResult) -> list[bytes]:
    from paper2html.ingestion._limits import LIMITS

    total = sum(p.stat().st_size for p in result.page_image_paths)
    if total > LIMITS.source_bytes:
        raise ValueError("PDF facsimile asset byte budget exceeded")
    return [p.read_bytes() for p in result.page_image_paths]


@app.command()
def inspect(ctx: typer.Context, source: str) -> None:
    """Inspect cached artifacts for a paper."""
    console.print("[bold]paper2html[/bold] inspect — stub")


@app.command("list")
def paper_list(ctx: typer.Context) -> None:
    """List all processed papers in the local library."""
    config = ctx.obj["config"]
    db_path: Path = config.cache_dir / "library.db"
    try:
        with closing(init_db(db_path)) as conn:
            rows = list_papers(conn)
    except (ValueError, OSError, sqlite3.Error) as exc:
        console.print(
            f"Library error: {exc}. Check library schema/permissions or choose another --cache-dir; preserve the existing database.",
            style="red",
            markup=False,
        )
        raise typer.Exit(code=1) from exc

    table = Table(
        "ID",
        "ArXiv ID",
        "Title",
        "Authors",
        "Date",
        "Cost",
        "Status",
        box=rich.box.SIMPLE,
        show_header=True,
    )

    for row in rows:
        arxiv_cell = row["arxiv_id"] or "—"

        raw_title: str = row["title"]
        title_cell = (raw_title[:60] + "…") if len(raw_title) > 60 else raw_title

        try:
            author_list: list[str] = json.loads(row["authors"])
        except (json.JSONDecodeError, TypeError):
            author_list = []
        if not author_list:
            authors_cell = "—"
        elif len(author_list) == 1:
            authors_cell = author_list[0]
        else:
            authors_cell = f"{author_list[0]} et al."

        date_cell = row["processed_date"][:10]
        cost_cell = f"${row['cost_spent']:.2f}"

        html_path = Path(row["output_html_path"])
        status_cell = (
            "[green]ok[/green]" if html_path.exists() else "[dim]missing[/dim]"
        )

        table.add_row(
            str(row["id"]),
            arxiv_cell,
            title_cell,
            authors_cell,
            date_cell,
            cost_cell,
            status_cell,
        )

    console.print(table)


@app.command("open")
def paper_open(ctx: typer.Context, paper_id: int) -> None:
    """Open a processed paper's HTML output in the default browser."""
    config = ctx.obj["config"]
    db_path: Path = config.cache_dir / "library.db"
    try:
        with closing(init_db(db_path)) as conn:
            row = get_paper_by_id(conn, paper_id)
    except (ValueError, OSError, sqlite3.Error) as exc:
        console.print(
            f"Library error: {exc}. Check library schema/permissions or choose another --cache-dir; preserve the existing database.",
            style="red",
            markup=False,
        )
        raise typer.Exit(code=1) from exc

    if row is None:
        console.print(f"[red]No paper found with id {paper_id}.[/red]")
        raise typer.Exit(code=1)

    html_path = Path(row["output_html_path"])
    if not html_path.is_file():
        console.print(
            f"[red]HTML file for paper id {paper_id} not found: {html_path}. Reprocess the source to regenerate it.[/red]"
        )
        raise typer.Exit(code=1)

    uri = html_path.resolve().as_uri()
    webbrowser.open(uri)
    console.print(f"[green]Opening {uri}[/green]")


cache_app = typer.Typer()
app.add_typer(cache_app, name="cache")


@cache_app.callback()
def cache_callback(ctx: typer.Context) -> None:
    """Cache management commands."""
    ctx.ensure_object(dict)


@cache_app.command("clear")
def cache_clear(
    ctx: typer.Context,
    scope: str = typer.Option("responses", help="responses, ingestion, or all"),
) -> None:
    """Clear response entries (default) or tracked ingestion artifacts; preserve user files."""
    ctx.ensure_object(dict)
    config = ctx.obj.get("config")
    cache_dir = config.cache_dir if config else None
    if scope not in {"responses", "ingestion", "all"}:
        raise typer.BadParameter(
            "Choose responses, ingestion, or all", param_hint="--scope"
        )
    try:
        if scope == "responses":
            count = clear_cache(cache_dir)
            console.print(f"Cleared {count} response entry(s).")
        else:
            counts = clear_cache_scoped(cache_dir, scope=scope)
            console.print(
                f"Cleared {counts.responses} response entry(s), {counts.ingestion} ingestion file(s); "
                f"skipped {counts.skipped} protected/unrecognized ingestion artifact(s)."
            )
    except (ValueError, OSError, sqlite3.Error) as exc:
        console.print(
            f"Cache error: {exc}. Check cache permissions/unsafe symlinks or choose another --cache-dir; do not delete unrecognized files.",
            style="red",
            markup=False,
        )
        raise typer.Exit(code=1) from exc


@cache_app.command("stats")
def cache_stats() -> None:
    """Cache statistics (not implemented)."""
    console.print("cache stats — stub")
