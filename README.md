# paper2html

[![Offline checks](https://github.com/JewelsHovan/paper2html/actions/workflows/checks.yml/badge.svg)](https://github.com/JewelsHovan/paper2html/actions/workflows/checks.yml)
[![License: AGPL v3+](https://img.shields.io/badge/License-AGPL_v3%2B-blue.svg)](LICENSE)

Turn an arXiv paper or local PDF into a **complete, self-contained HTML reader**. The same CLI also supports a **staged, approval-gated narrated video workflow**. Use it directly or let an agent in any harness drive the commands; no Compass installation is required.

HTML source conversion needs no model or API key. Optional annotations use Claude/OpenAI; optional video narration uses an explicitly selected Gemini model and voice. Python 3.11+; `uv.lock` locks the checkout environment, while ordinary pip installs resolve the declared dependency ranges.

The goal is an HTML version of the **entire original paper**, including its figures—not a summary replacing the source. Conversion warnings and source-to-output checks matter: successful rendering alone does not establish completeness.

## Install

Start with Python 3.11+ and [uv](https://docs.astral.sh/uv/getting-started/installation/). Install native Cairo for SVG figures: `brew install cairo` (macOS), `sudo apt install libcairo2` (Ubuntu/Debian), or `sudo dnf install cairo` (Fedora).

```bash
git clone https://github.com/JewelsHovan/paper2html.git
cd paper2html
uv sync --frozen --no-dev --extra browser
uv run --frozen --no-dev playwright install chromium
# Linux: use `playwright install --with-deps chromium` for browser OS dependencies.
```

The browser extra handles supported static LaTeXML figure labels. Prefer it for arXiv fidelity; a plain `uv sync --frozen --no-dev` installs the smaller core. Development tools are not required for use.

**Without uv:** install from the checkout using `python -m pip install '.[browser]'`, then `python -m playwright install chromium`. Use a virtual environment; see the [installation guide](docs/installation.md). A PyPI release is not assumed by these instructions.

## Convert a paper

```bash
# Entire source paper; no model calls or API key required.
uv run --frozen --no-dev paper2html process paper.pdf --source-only -o paper.html
uv run --frozen --no-dev paper2html process https://arxiv.org/abs/1706.03762 --source-only -o attention.html
uv run --frozen --no-dev paper2html list
uv run --frozen --no-dev paper2html open 1
```

## Narrated videos and agents

```bash
uv run --frozen --no-dev paper2html video --help
uv run --frozen --no-dev paper2html video doctor --json
uv run --frozen --no-dev paper2html video instructions
```

[Video guide](docs/video.md) explains the staged workflow, optional Node 22+/FFmpeg/Chromium runtime, explicit network setup, Gemini credentials, estimated budgets, and source/script/preview reviews. No API spending or render happens merely by inspecting status. Generic video support is new: offline tests do not establish live provider availability or cinematic quality for every paper.

[Agent guide](docs/agents.md) provides the same portable CLI contract for any harness: JSON output, durable projects, hash-bound approvals, no implicit paid retries, and no automatic upload. Agents should obtain the user's approval rather than approve their own work. The optional repository-specific Compass demo is historical guidance, not a runtime dependency.

On Apple Silicon, if the Cairo loader cannot find Homebrew's library:

```bash
export DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib
uv run python -c 'import cairosvg'
```

Missing Cairo omits ordinary SVG figures with an actionable warning; raster figures and text can still work. Static LaTeXML `<foreignObject>` labels additionally require the optional Playwright Chromium backend (`uv sync --frozen --no-dev --extra browser && uv run --frozen --no-dev playwright install chromium`). Without it those figures are omitted with an install warning. Do not suppress warnings when checking completeness.

For arXiv papers with native HTML, the source view preserves supported article content in its original order, including front matter, inline math, captions, tables, and references. SVG `<object>` figures are safely rasterized and embedded. PDFs use complete page facsimiles (including vector graphics) with separate searchable text—not a claim of reconstructed semantic math. See [source fidelity](docs/source-fidelity.md).

```bash
uv run paper2html process https://arxiv.org/pdf/2511.12723 --source-only -o paper.html
# For deterministic development-only mock annotations:
uv run paper2html --provider mock process paper.pdf -o annotated.html
```

`--source-only` skips AI annotations altogether; omitting it uses the configured provider but **still preserves complete PDF page facsimiles**, including vector figures. PDF title/authors/abstract remain visible; optional AI summaries and figure notes are separate from the original content. Source/page validation runs before provider construction: budget or invalid-page errors stop conversion without charging or replacing an existing output, even with `--overwrite`. See the [live fidelity check for arXiv 2511.12723](docs/validation-2511.12723.md) for a source-to-output comparison of all text, figures, math, tables and references.

Global options go **before** `process`: `--provider`, `--model`, `--worker-model`, `--cache-dir`, `--max-cost-usd`, `--max-concurrency` (default 4), `--max-output-tokens` (8192), and `--request-timeout-seconds` (60). `--source-only`, `-o`/`--output` and `--overwrite` belong **after** `process`.

```bash
# Requires ANTHROPIC_API_KEY; this is a paid operation, not a validation command.
uv run paper2html --provider claude --max-cost-usd 2 --max-concurrency 2 process 1706.03762 -o attention.html
# OPENAI_API_KEY is used with --provider openai.
```

Sources may be local PDFs, validated arXiv IDs or arXiv URLs. Destination collisions are refused unless `--overwrite` is supplied. Even with overwrite, source aliases (including symlink/hardlink aliases) are refused. Output is published atomically; no-clobber publication is supported on local filesystems. Do not use output paths inside ingestion-managed directories. `inspect` and `cache stats` are explicitly **stubs**, not working inspection tools.

## Safety, cost and completeness

`--max-cost-usd` is an **estimated admission budget, not an invoice guarantee**. Built-in pricing is unverified and may drift. Input/image allowances are conservative heuristics, not provider tokenization. Unknown capped models are rejected; uncapped unknown-model accounting can report zero with a warning. Dispatched requests with unknown usage (including cancellation/timeouts) retain conservative reservations. Capped requests do not hide transport retries; actual usage is reconciled when known, and overruns stop subsequent admissions. Already-dispatched calls cannot be unbilled. Review provider prices before use.

Failed sections/planning/synthesis produce explicit warnings and partial results. Incomplete aggregate results are not cached as complete. An HTML file or a zero exit status does not certify scientific accuracy or complete annotations. A later run can recover; successful cached results replay without provider calls.

Ingestion accepts only policy-approved arXiv HTTPS fetch targets, validates redirects and limits encoded/decoded bodies. Defaults: 32 MiB HTTP bodies, 128 MiB PDFs, 16 MiB images, 25 million pixels, 16,384 pixels per side, 500 PDF pages; 30-second HTTP operations and a checked 120-second GET-chain deadline, four redirects. PDF pages rasterize at 150 DPI. Native PDF/Cairo processing is **in-process, not a CPU sandbox**: byte/dimension limits do not provide a hard CPU/memory/time guarantee. Hostile documents requiring isolation must run in an externally constrained process/container.

Direct `from_pdf`/`from_arxiv_html` model adapters do **not** enforce all ingestion download/source/page limits. Use ingestion before adaptation for those protections. Rendering independently sanitizes MathML/tables and validates embedded raster images. SVG support is deliberately restricted: safe geometry, local references and supported CSS; no external/file fetches, scripts or active content. Base64 SVG data URIs pass the same SVG validator, not a bypass. Embedded base64 raster assets must validate and fit aggregate budgets; nested SVG and unsupported encodings/features are rejected. Static LaTeXML `<foreignObject>` labels (XHTML spans and presentation MathML) use an optional, offline, JavaScript-disabled Chromium renderer after shared SVG safety checks and strict reconstruction. This is **not** general-purpose HTML-in-SVG support: unknown geometry, markup, attributes and CSS are rejected, and publisher styles/fonts are not loaded. Some legitimate complex SVGs can be omitted or differ typographically.

## Cache and library lifecycle

Default root: `~/.cache/paper2html` (override with `--cache-dir`). Response entries live under `responses/`; PDF content-addressed artifacts and downloads under `ingestion/`. Newly created ingestion artifacts are registered in `ingestion-artifacts.sqlite3`; eviction verifies identity and protects replaced/unrecognized files. `library.db` records canonical HTML output paths for `list`/`open`.

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

See [remediation and limits](docs/remediation.md) and [benchmark methodology](benchmarks/README.md).

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and checks, and [SECURITY.md](SECURITY.md) for private vulnerability reporting. Generated paper outputs, caches, credentials and local agent workspaces are intentionally excluded from Git; use `outputs/` for local conversions.

## License

Copyright © 2026 Julien Hovan. Project code is licensed under **GNU AGPL version 3 or later**; see [LICENSE](LICENSE). Vendored components and installed dependencies retain their own terms—see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Paper content is not relicensed by this software.
