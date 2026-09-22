# Security policy

## Reporting a vulnerability

Please do not publish credentials, private documents, or a working exploit in a public issue. Use [GitHub private vulnerability reporting](https://github.com/JewelsHovan/paper2html/security/advisories/new) for this repository. Include the affected revision, a minimal synthetic reproduction, and the expected impact.

Security fixes currently target the latest `main` branch; no older-version support window is promised.

## Security boundaries

- Treat PDFs, HTML, images and model responses as untrusted input.
- Native PDF/SVG processing is not sandboxed. Use external process/container resource isolation for hostile documents.
- Cost limits are conservative admission estimates, not guarantees of a provider's invoice.
- Do not clear application caches concurrently with processing.
- Do not put generated outputs in application-managed cache directories.
- Do not commit provider credentials or paper content that you cannot redistribute.

See [README.md](README.md) and [docs/remediation.md](docs/remediation.md) for implemented protections and remaining limits.
