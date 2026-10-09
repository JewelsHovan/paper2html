# Agent instructions

This project is CLI-first and harness-agnostic. Use the same supported commands a human would use; do not require Compass or any installed agent skill to operate the product.

## Run the product

- Start with `paper2html --help`, `paper2html video --help` and `paper2html video instructions`.
- From a locked checkout, prefix commands with `uv run --frozen --no-dev`.
- Free source HTML: `paper2html process SOURCE --source-only -o OUTPUT.html`.
- Consult [docs/installation.md](docs/installation.md), [docs/video.md](docs/video.md) and [docs/agents.md](docs/agents.md). Read command help instead of guessing options.
- Use JSON video status and durable project state when resuming. The CLI, not ignored experiment scripts or chat commands, is the public automation interface.

## Non-negotiable boundaries

- Preserve the entire paper and original visuals. Check against source evidence; a successful command does not certify completeness or scientific accuracy.
- No paid calls, provider changes or package/browser installation without the user's authorization. No blind retry of failed/unknown-billed requests. Budgets are estimates, not invoices.
- Get explicit user approval for the current script/voice and checked current preview before synthesis/rendering respectively. Never self-approve, fabricate consent, or edit state to bypass a gate.
- Keep credentials in environment variables; never print keys or write them into source, receipts or prompts. No public/cloud upload without separate authorization.
- Preserve unrelated user changes. Do not commit, push, reset, delete project locks or clean caches unless asked.

## Develop and verify

- `uv sync --frozen --group dev --group browser`; provision Chromium explicitly when authorized.
- Offline suite: `uv run --frozen pytest -m 'not integration and not browser'`.
- Browser gate: `PAPER2HTML_BROWSER_TESTS=1 uv run --frozen pytest -m browser` after provisioning.
- `uv run --frozen ruff check src tests benchmarks`; `uv run --frozen ruff format --check src tests benchmarks`.
- `uv build`; verify wheel installation/help/local-fixture usage from an isolated environment.
- Tests must not call live providers/download papers by default. Mock transports, use generated fixtures, and test stale approvals, budgets, crashes, concurrency, path protection and secret redaction.
- Keep generated artifacts under ignored `outputs/`. Do not import code from that directory or machine-specific agent directories into the installed package.

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).
