# Contributing

Small, focused pull requests with regression tests are welcome. Discuss substantial behavior changes in an issue first.

## Development setup

Use Python 3.11+ and [uv](https://docs.astral.sh/uv/):

```bash
uv sync --frozen --group dev --group browser
uv run playwright install chromium
```

SVG tests require native Cairo. See [README.md](README.md) for platform setup, including the macOS library-loader setting.

## Checks

```bash
uv run ruff check src tests benchmarks
uv run ruff format --check src tests benchmarks
PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:cacheprovider -p no:cov -m 'not integration and not browser'
PAPER2HTML_BROWSER_TESTS=1 uv run pytest -p no:cacheprovider -p no:cov -m browser
uv run python benchmarks/offline_pipeline.py --quick
uv build
```

Run `uv run ruff format src tests benchmarks` to format your changes. Explicit browser checks must not silently skip missing prerequisites. CI also requires a real Cairo conversion.

## Change guidelines

- Preserve the entire source paper and its figures independently of optional AI annotations. Report missing or unsupported content rather than silently declaring conversion complete.
- Add a regression that fails before the fix; prefer deterministic fixtures and mocked transports/providers.
- Never make live arXiv requests or paid provider calls from the default test suite. Live integration tests require explicit opt-in.
- Keep resource limits, output-file protection, cache ownership, and error recovery intact.
- Document user-visible flags, compatibility changes, and remaining limits.
- Do not commit API keys, downloaded papers, generated HTML, local caches, or agent workspace state. Use `outputs/` for local conversions.
- Keep benchmark baselines distinct from speedup claims; timings are hardware-dependent.

For architecture and regression references, see [docs/remediation.md](docs/remediation.md).

## License

Contributions to project code are under **AGPL-3.0-or-later**, unless explicitly identified as third-party material with its own compatible license. Do not submit copyrighted paper content or assets without permission.
