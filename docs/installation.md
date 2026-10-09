# Installation

## Choose an installation

Use a checkout or release that contains the desired commands. These instructions do not assume the package has been published to PyPI. Check `paper2html --help` and `paper2html video --help` after installation.

### Locked checkout with uv (recommended)

Install Python 3.11+ and [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```sh
git clone https://github.com/JewelsHovan/paper2html.git
cd paper2html
uv sync --frozen --no-dev --extra browser
uv run --frozen --no-dev playwright install chromium
uv run --frozen --no-dev paper2html --help
```

`uv.lock` locks the Python checkout environment. `--no-dev` avoids test/lint dependencies. Browser provisioning downloads Chromium. On Linux use `playwright install --with-deps chromium`; installing OS packages may need elevated permissions. A core-only install is `uv sync --frozen --no-dev`, but some arXiv figures need the optional browser backend.

### Virtual environment and pip

From a checkout, on macOS/Linux:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install '.[browser]'
python -m playwright install chromium
paper2html --help
```

Or install a supplied wheel with its browser extra:

```sh
python -m pip install '/path/to/paper2html-0.1.0-py3-none-any.whl[browser]'
python -m playwright install chromium
```

Adjust the wheel filename to the supplied version. Pip resolves dependency ranges; it is not the same locked environment as `uv sync --frozen`. Do not install into the system Python. A wheel includes Python code, templates and agent instructions, not downloaded papers, API keys, Chromium, Node, FFmpeg or an installed Node renderer.

## System dependencies

| Capability | Requirement | Notes |
| --- | --- | --- |
| HTML conversion | Python 3.11+ | No API key for `--source-only` |
| Native SVG figures | Cairo | `brew install cairo`; `sudo apt install libcairo2`; `sudo dnf install cairo` |
| Supported LaTeXML labels | Python browser extra + Chromium | `python -m playwright install chromium` |
| Video runtime | Node 22+, npm, FFmpeg/ffprobe | Optional; use `paper2html video doctor --json` |
| Video generation | Gemini API key and explicit reviewed model/rates | Paid commands require authorization; no fallback |

macOS video prerequisites: `brew install node ffmpeg cairo`. Ubuntu/Debian: install Node **22 or newer** through your chosen maintained distribution, plus `sudo apt install ffmpeg libcairo2`; the default distro Node may be too old. Renderer installation is an explicit separate network step in the [video guide](video.md), not a side effect of `doctor` or status.

On Apple Silicon, if Cairo cannot find the Homebrew library:

```sh
export DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib
python -c 'import cairosvg'
```

### Platform status

- **Ubuntu 24.04 / Python 3.11:** automated offline CI, including browser-backed fixtures and package checks.
- **macOS / Apple Silicon:** locally exercised HTML conversion and the narrated-video experiment; not a macOS CI guarantee.
- **Windows:** not currently CI-tested. Python/pip may work, but native Cairo and renderer tooling need platform-specific setup. Do not interpret installation success as full feature support.

## First conversion

```sh
paper2html process paper.pdf --source-only -o paper.html
# Network download, but no model calls:
paper2html process https://arxiv.org/abs/1706.03762 --source-only -o attention.html
```

Open the resulting HTML directly in a browser. Conversion warnings matter: unsupported visuals can be omitted explicitly. Validate against the original; command success alone is not completeness. Outputs are self-contained; PDFs preserve full-page facsimiles with searchable text rather than reconstructed semantic equations.

Destination collisions fail unless `--overwrite` is supplied; source aliases are always refused. Do not put generated outputs in source-ingestion managed directories. To avoid paid annotations, keep `--source-only`.

## Credentials

Only configure keys for features you actually want:

```sh
export ANTHROPIC_API_KEY='your-key'  # optional Claude annotations
export OPENAI_API_KEY='your-key'     # optional OpenAI annotations
export GEMINI_API_KEY='your-key'     # optional narrated-video generation
```

The CLI does **not** automatically load `.env`. `.env.example` lists names; never commit populated files or put keys in command arguments, logs, receipts or agent prompts. Use your shell/secret manager. An agent can check whether a variable is present without revealing its value.

Global HTML options precede `process`; process-specific options follow it. Video has its own subcommand options; see `paper2html video --help`. For agent-driven use, see [agents.md](agents.md).

## Troubleshooting

- **`paper2html: command not found`:** activate the virtual environment or use `uv run --frozen --no-dev paper2html` in the checkout.
- **No `video` command:** you installed an older checkout/release. Check the installed distribution and source, not only the working directory.
- **SVG/browser warning:** install Cairo/browser prerequisites; do not suppress the warning or certify a partial file as complete.
- **Missing video runtime:** follow the explicit setup command in the video guide. Doctor does not silently install software.
- **Missing key / unknown model or pricing:** configure the explicitly intended provider/model and current pricing; no automatic substitution is permitted.
- **Approval became stale:** review changed artifacts and explicitly approve their new hashes. Do not reuse the old approval.

## Development

```sh
uv sync --frozen --group dev --group browser
uv run --frozen playwright install chromium
uv run --frozen pytest -m 'not integration and not browser'
```

See [CONTRIBUTING.md](../CONTRIBUTING.md). Default tests must not download papers or make paid calls. Native parsers are not a CPU sandbox; run hostile inputs in an externally constrained environment.
