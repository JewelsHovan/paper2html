# Reference: options, safety, caches and providers

Detailed operating notes for `paper2html process` and the library. Start with the [README](../README.md) and [installation guide](installation.md); see [video.md](video.md) for the narrated-video workflow.

## Command-line options

Global options go **before** `process`; process options go **after** it.

| Option | Position | Default | Purpose |
| --- | --- | --- | --- |
| `--provider` | global | `claude` | `claude`, `openai`, or deterministic offline `mock` |
| `--model`, `--worker-model` | global | provider default | Model names for annotation |
| `--cache-dir` | global | `~/.cache/paper2html` | Cache root override |
| `--max-cost-usd` | global | none | Estimated admission budget (not an invoice guarantee) |
| `--max-concurrency` | global | `4` | Concurrent pipeline calls |
| `--max-output-tokens` | global | `8192` | Output token limit per call |
| `--request-timeout-seconds` | global | `60` | Per-attempt provider timeout |
| `--source-only` | process | off | Preserve source content with no AI provider or annotations |
| `-o`, `--output` | process | `<source stem>.html` | Output path; collisions are refused |
| `--overwrite` | process | off | Replace an existing output, never the source |

```bash
# Requires ANTHROPIC_API_KEY; this is a paid operation, not a validation command.
uv run paper2html --provider claude --max-cost-usd 2 --max-concurrency 2 process 1706.03762 -o attention.html
# OPENAI_API_KEY is used with --provider openai.
# Deterministic development-only mock annotations:
uv run paper2html --provider mock process paper.pdf -o annotated.html
```

Sources may be local PDFs, validated arXiv IDs or arXiv URLs. The general command normalizes arXiv input to the latest revision; it is not a version-pinning guarantee. Destination collisions are refused unless `--overwrite` is supplied. Even with overwrite, source aliases (including symlink/hardlink aliases) are refused. Output is published atomically; no-clobber publication is supported on local filesystems. Do not use output paths inside ingestion-managed directories. `inspect` and `cache stats` are explicitly **stubs**, not working inspection tools.

`--source-only` skips AI annotations altogether; omitting it uses the configured provider but **still preserves complete PDF page facsimiles**, including vector figures. PDF title/authors/abstract remain visible; optional AI summaries and figure notes are separate from the original content. Source/page validation runs before provider construction: budget or invalid-page errors stop conversion without charging or replacing an existing output, even with `--overwrite`.

## Source fidelity

For arXiv papers with native HTML, the source view preserves supported article content in its original order, including front matter, inline math, captions, tables, and references. SVG `<object>` figures are safely rasterized and embedded. PDFs use complete page facsimiles (including vector graphics) with separate searchable text—not a claim of reconstructed semantic math. See [source fidelity](source-fidelity.md) and the [live fidelity check for arXiv 2511.12723](validation-2511.12723.md).

Missing Cairo omits ordinary SVG figures with an actionable warning; raster figures and text can still work. Static LaTeXML `<foreignObject>` labels additionally require the optional Playwright Chromium backend (`uv sync --frozen --no-dev --extra browser && uv run --frozen --no-dev playwright install chromium`). Without it those figures are omitted with an install warning. Do not suppress warnings when checking completeness.

## Cost and completeness

`--max-cost-usd` is an **estimated admission budget, not an invoice guarantee**. Built-in pricing is unverified and may drift. Input/image allowances are conservative heuristics, not provider tokenization. Unknown capped models are rejected; uncapped unknown-model accounting can report zero with a warning. Dispatched requests with unknown usage (including cancellation/timeouts) retain conservative reservations. Capped requests do not hide transport retries; actual usage is reconciled when known, and overruns stop subsequent admissions. Already-dispatched calls cannot be unbilled. Review provider prices before use.

Failed sections/planning/synthesis produce explicit warnings and partial results. Incomplete aggregate results are not cached as complete. An HTML file or a zero exit status does not certify scientific accuracy or complete annotations. A later run can recover; successful cached results replay without provider calls.

## Ingestion and rendering safety

Ingestion accepts only policy-approved arXiv HTTPS fetch targets, validates redirects and limits encoded/decoded bodies.

| Limit | Default |
| --- | --- |
| HTTP body | 32 MiB |
| PDF | 128 MiB, 500 pages |
| Image | 16 MiB, 25 million pixels, 16,384 pixels per side |
| HTTP operation | 30 seconds |
| GET chain | checked 120-second deadline, four redirects |
| PDF page raster | 150 DPI |

Native PDF/Cairo processing is **in-process, not a CPU sandbox**: byte/dimension limits do not provide a hard CPU/memory/time guarantee. Hostile documents requiring isolation must run in an externally constrained process/container.

Direct `from_pdf`/`from_arxiv_html` model adapters do **not** enforce all ingestion download/source/page limits. Use ingestion before adaptation for those protections. Rendering independently sanitizes MathML/tables and validates embedded raster images. SVG support is deliberately restricted: safe geometry, local references and supported CSS; no external/file fetches, scripts or active content. Base64 SVG data URIs pass the same SVG validator, not a bypass. Embedded base64 raster assets must validate and fit aggregate budgets; nested SVG and unsupported encodings/features are rejected. Static LaTeXML `<foreignObject>` labels (XHTML spans and presentation MathML) use an optional, offline, JavaScript-disabled Chromium renderer after shared SVG safety checks and strict reconstruction. This is **not** general-purpose HTML-in-SVG support: unknown geometry, markup, attributes and CSS are rejected, and publisher styles/fonts are not loaded. Some legitimate complex SVGs can be omitted or differ typographically.

## Cache and library lifecycle

Default root: `~/.cache/paper2html` (override with `--cache-dir`).

| Location | Contents |
| --- | --- |
| `responses/` | Provider response entries |
| `ingestion/` | PDF content-addressed artifacts and downloads |
| `ingestion-artifacts.sqlite3` | Registry of newly created ingestion artifacts; eviction verifies identity and protects replaced/unrecognized files |
| `library.db` | Canonical HTML output paths for `list`/`open` |

```bash
uv run paper2html cache clear                     # responses only
uv run paper2html cache clear --scope ingestion   # registered artifacts only
uv run paper2html cache clear --scope all         # both, not library/output/source
```

Legacy root response caches and unregistered ingestion files are intentionally retained, not automatically adopted or deleted. User files, library records and rendered HTML are not eviction targets. Abandoned temporary files/snapshots after SIGKILL and empty owned directories are retained: there is no broad sweep; inspect manually before any cleanup. Catalog registration failures warn and preserve usable published content; unregistered artifacts remain protected. Library upserts intentionally preserve original title/authors while refreshing output path, processing date and cost. **Do not clear concurrently with processing**; this is not a cross-process transactional cache protocol. Trusted symlink cache roots are resolved, but owned internal symlinks are rejected.

## Provider interface compatibility

Custom providers implement async `complete(messages, images, tools, *, model, max_output_tokens=8192, transport_retries=True, request_timeout_seconds=60.0)` and async `aclose()`. The keyword-limit contract is a breaking change for older adapters/test doubles. Forward output/time limits; honor the no-hidden-retry contract. Owned SDK clients are closed; injected clients remain caller-owned. The synchronous `run_pipeline` entry point owns cleanup and cannot run inside an active event loop; use the async orchestrator there.

## Offline validation

```bash
uv sync --frozen --group dev --group browser
uv run --frozen playwright install chromium   # provisioning only; Linux CI uses --with-deps
PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:cacheprovider -p no:cov -m 'not integration and not browser'
PAPER2HTML_BROWSER_TESTS=1 uv run pytest -p no:cacheprovider -p no:cov -m browser
uv run ruff check src tests benchmarks
uv run ruff format --check src tests benchmarks
uv run python benchmarks/offline_pipeline.py --quick
```

The browser group is optional. Without opt-in, browser tests skip; with `PAPER2HTML_BROWSER_TESTS=1`, missing Playwright/browser installation is a failure. Tests use isolated offline contexts with service workers blocked, request/error/resource checks, real keyboard focus, and generated fixtures only. On macOS a cached headless shell is a fallback; `PAPER2HTML_CHROMIUM_EXECUTABLE` can select an explicit executable. CI separately asserts native Cairo availability before optional Cairo tests. No validation command above calls live arXiv/providers. Do not select `integration` without intentionally authorizing network/paid calls.

See [remediation and limits](remediation.md) and [benchmark methodology](../benchmarks/README.md).
