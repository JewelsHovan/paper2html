# Agent-driven usage

The supported product interface is the **CLI**, not a harness API or chat slash command. Humans and agents use the same commands. No Compass, Claude Code, Codex, Cursor or other specific harness is required.

## Discover the installed contract

```sh
paper2html --help
paper2html process --help
paper2html video --help
paper2html video doctor --json
paper2html video instructions
```

Use `uv run --frozen --no-dev paper2html` instead of `paper2html` when running from the locked checkout. Read subcommand help before constructing flags; do not invent a `paper2html video URL` shortcut. Use the installed version's contract rather than a historical experiment script.

## Prompt you can give any agent

> Use the installed paper2html CLI to turn this paper into a complete HTML reader and, optionally, a narrated video. Start by reading `paper2html video instructions` and the relevant `--help`. Inspect prerequisites without installing anything silently. Use a new project directory and JSON status to resume safely. Preserve the entire source paper and original visuals; disclose unsupported content. Show the sources and draft before asking for script/voice approval. Ask me to authorize a reviewed estimated budget and each paid stage; do not retry failed or unknown-billed requests. Show a checked preview with sound and ask before rendering its current hashes. Verify the actual encoded MP4. Do not publish, upload, delete sources, reveal credentials or approve your own work.

## HTML-only path

```sh
paper2html process /path/to/paper.pdf --source-only -o /path/to/paper.html
```

No model credentials or calls are required. arXiv input downloads source files. Inspect warnings and compare against the source; successful execution is not proof of complete content. Optional paid HTML annotations have separate provider/options; global options go before `process`.

## Video stages

Follow the exact commands in [video.md](video.md) and the installed help. A video project contains its own state, evidence, receipts and artifacts. Use `status --json` before acting after context loss. JSON output is the machine contract; ordinary text is for people.

An agent may prepare evidence, check structure and suggest corrections. It must stop for the user at:

1. **Budget/network authorization:** renderer installation and provider requests must be explicit. Review model availability/pricing. No key is needed merely to read status/help.
2. **Script and voice approval:** compare the draft with the full source, including the scope of numeric claims and original-visual attribution. Lock the exact reviewed script and requested model/voice, not a remembered earlier draft.
3. **Preview approval:** show animation and narration. The user should listen for proper-noun pronunciation and inspect source-figure/table readability. Current preview inputs must match the approved hashes.
4. **Local render:** use the explicit rendering command only after preview approval. Public upload is not included.

CLI approval records are **attestations**, not proof that a human actually reviewed something. An agent must not turn its own quality assessment into user consent, fabricate an approval identity/receipt, or bypass a gate by editing state JSON.

## Errors and resumption

- Parse the JSON `error_code`, blocking reason and next action; also respect a nonzero exit status. Do not grep human prose as an API.
- Missing credentials/dependencies: report the missing item and setup command. Never print a key, substitute a provider, or install packages without authorization.
- Budget refusal: show the estimate/reservation and ask. Estimates are not invoices or enforceable provider-side spending caps.
- Unknown-billed/failed paid attempt: preserve its receipt/reservation. Do not blindly repeat it; a new authorization must explicitly acknowledge the earlier attempt.
- Stale approval: display what changed, re-review and obtain approval against the new hash. Do not rewrite the old approval hash.
- Project lock: stop if another process owns the run. After a crash, inspect rather than automatically deleting a lock or redispatching a request.
- Missing source visual/version mismatch: stop or use an explicitly chosen PDF-only mode. Do not silently replace the full paper with a summary.

## What checks do and do not establish

Automated tests can validate hashes, referenced pages/assets, exact quotes, budget math, approval freshness, audio metadata, and encoded-file integrity. They cannot establish that every paraphrase is faithful, every diagram is readable, or the paper's scientific claims are true. Human/agent source review is still required; zero findings/placeholders is not a universal fidelity guarantee.

## Privacy and reproducibility

Credentials belong in environment variables/secret managers; `.env` is not automatically loaded. Network setup may download npm/browser packages; paid generation sends the supplied source and script to the explicitly selected provider. Source content may have redistribution constraints independent of the project's software license.

Store generated papers and video projects in ignored `outputs/` or another user-selected directory. No runtime code should import ignored experiment scripts, resolve `~/.agents` skill paths or require `.compass` configuration. Do not commit generated source content or secrets. Record installed Python/renderer/model versions and retain receipts when sharing a reproducibility report.
