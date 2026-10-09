# Narrated video CLI

`paper2html video` is a **native, staged CLI** for people and agents in any harness. It does not import the ignored paper-specific prototypes or require Compass, HyperFrames agent skills, or `~/.agents` helpers. It is deliberately not an unattended URL-to-MP4 command.

**Support level:** this generic CLI is new and tested with local fixtures/mocked providers. The earlier hand-reviewed AutoSciBench video proves the approach, not every generic adapter/model/live-paper combination. Keep source/script/audio/preview reviews; no automated check certifies scientific truth or cinematic quality.

## Prerequisites

Follow [installation.md](installation.md). Source preparation needs Python; rendering additionally needs Node **22+**, npm, FFmpeg/ffprobe and Chromium. Gemini requests need `GEMINI_API_KEY`, an explicitly chosen available model, current user-reviewed prices, and paid-stage consent.

```sh
paper2html video --help
paper2html video doctor --json
paper2html video instructions
```

From a locked checkout, prefix commands with `uv run --frozen --no-dev`. Doctor is a local inspection: it must not download packages or spend money. The renderer is a project-local published dependency, HyperFrames **0.8.142** (Apache-2.0), not vendored external skill code. Setup is an explicit npm network bootstrap; renderer/browser tooling can have upstream telemetry/network behavior. No upload command is provided.

## 1. Prepare sources

Use a fresh project directory:

```sh
paper2html video init outputs/my-video --source /path/to/paper.pdf
# Native arXiv HTML + full matching PDF: choose an explicit revision.
paper2html video init outputs/arxiv-video --source https://arxiv.org/abs/1706.03762v7
paper2html video status outputs/my-video --json
```

A local PDF is honestly **PDF-only**. arXiv requires an explicit `vN`; do not silently normalize away the version or send different revisions to the model. Source files/hashes, identity, original page assets, extracted text and native figure/table IDs are recorded in the project. Inspect them against the original before paid generation. Renderer visuals use original source pages, not invented diagrams, numerical charts or fabricated screenshots; full-page layouts can contain small text and require preview review.

For the full standalone HTML reader, use the separate free command:

```sh
paper2html process /path/to/paper.pdf --source-only -o outputs/paper.html
# To keep the reader aligned to the video's frozen arXiv revision:
paper2html process outputs/arxiv-video/sources/original.pdf --source-only -o outputs/matching-paper.html
```

The general HTML command normalizes arXiv input to the latest revision; an explicit URL suffix is not a version-pinning guarantee there. Convert the video's frozen PDF when exact matching matters. That PDF reader preserves page facsimiles/searchable text rather than native article structure.

## 2. Create and check a script

Two paths share the same validation/approval gates:

### Bring a reviewed script (no API call)

Prepare a JSON script with the exact `source_revision` from the manifest (`null` for local PDF) and a nonempty `scenes` array. Each scene needs a unique safe `id`, `title`, `narration`, and source `claims`. Each claim has `claim`, physical 1-indexed `pdf_page`, a verbatim `quote`, and `source: "pdf"`; native HTML claims additionally use `source: "html"` and a real `html_id`. Optional `visual_refs` select a real `pdf_page` and, where applicable, an existing native `html_id`.

```sh
paper2html video script import outputs/my-video /path/to/reviewed-script.json
paper2html video script check outputs/my-video
```

### Explicitly authorize a Gemini draft

Prepare a rate-profile JSON. This skeleton is **not runnable pricing**: replace `null` with current reviewed USD-per-million rates for each exact selected model. TTS output pricing must be the applicable audio-output rate, not a text-model rate.

```json
{
  "models": {
    "EXACT_SCRIPT_MODEL_ID": {
      "input_usd_per_million": null,
      "output_usd_per_million": null
    },
    "EXACT_TTS_MODEL_ID": {
      "input_usd_per_million": null,
      "output_usd_per_million": null
    }
  }
}
```

After authorizing the model, prices, source transmission and limits:

```sh
paper2html video script draft outputs/my-video \
  --model EXACT_SCRIPT_MODEL_ID --rate-profile /path/to/reviewed-rates.json \
  --request-budget 2 --run-budget 10 --max-output-tokens 8192 --consent
paper2html video script check outputs/my-video
```

Budgets are **estimated request/run admission limits, not invoice guarantees**. Counted input and requested output allowances are reserved before generation. A failed or unknown-billed attempt retains its reservation and is not silently repeated. Missing/unknown rates block; no provider/model fallback. Review actual provider limits and pricing before each new run; supplied rates can be wrong or change.

Both full sources are used when paired HTML/PDF exists. Credential-redacted provider response/draft evidence and the current reviewed script are retained separately; the original response hash and any redaction notice preserve provenance without exposing keys. Quote/page/ID checks are structural grounding, not proof that the narration faithfully scopes every scientific or numerical claim. Review/correct the actual script before approval.

## 3. Approve the current script and requested voice

First prepare the exact requested model/voice, rates and budget, without an API call:

```sh
paper2html video voice-request outputs/my-video \
  --model EXACT_TTS_MODEL_ID --voice Kore \
  --rate-profile /path/to/reviewed-rates.json \
  --request-budget 2 --run-budget 10 --max-output-tokens 8192
```

After the user reviews the source/script and requested voice/budget:

```sh
paper2html video approve outputs/my-video script
paper2html video approve outputs/my-video voice
```

These approvals bind the current source/script and voice request. A generated script or successful check is not approval. Finalize script edits before creating `voice-request`; this initial CLI refuses to overwrite an existing request. To change it, create a fresh project and import the corrected script rather than editing approval files.

Then explicitly consent to narration generation with the exact approved model/voice and reviewed rate profile:

```sh
paper2html video voice outputs/my-video \
  --model EXACT_TTS_MODEL_ID --voice Kore \
  --rate-profile /path/to/reviewed-rates.json \
  --request-budget 2 --run-budget 10 --max-output-tokens 8192 --consent
```

Kore is an example voice, not an automatic provider substitution. Model availability is not guaranteed by these instructions. Native narration measures actual WAV durations; it does not force speech to exactly two minutes. Caption timing is approximate sentence-level timing unless stronger alignment evidence is explicitly provided—do not describe it as verified word alignment.

Listen to every WAV, especially paper/model names, before approving the audio:

```sh
paper2html video approve outputs/my-video audio
```

No public-upload rights are implied by the software license or a publicly accessible source PDF.

## 4. Install runtime, build and review

Explicit network setup:

```sh
paper2html video runtime-install outputs/my-video --consent
paper2html video build outputs/my-video
paper2html video preview outputs/my-video --serve
```

The local renderer is version-pinned; ordinary status/help/doctor does not install it. Build assets and audio are local; there is no rendering-time model call or composition CDN dependency. Native video styling is intentionally minimal/source-first, not the bespoke motion of the prior hand-built demonstration.

`preview` checks the composition but does **not** approve it. Use the actual server URL reported by the renderer; check all scenes, original visual readability, captions and sound. Stop the preview server with Ctrl-C when finished; an agent may run it in a background process while you review.

After user review, record approval of the current checked preview:

```sh
paper2html video approve outputs/my-video preview
```

Edits to script, source, WAVs, runtime/build inputs invalidate affected approvals. Status rechecks hashes; editing local JSON is not a supported approval bypass.

## 5. Render locally and verify the actual file

```sh
paper2html video render outputs/my-video
paper2html video verify outputs/my-video
paper2html video status outputs/my-video --json
```

Rendering requires current preview approval. Existing/incomplete render attempts are not silently overwritten or restarted. Verification inspects the encoded MP4, not just the HTML preview: dimensions/codecs, actual duration/audio, full decode and sampled nonblank/motion frames. **These samples do not establish that the expected source page is visible.** Inspect the encoded frames against the selected original assets yourself; source-presence/readability and every intermediate pose remain unverified by these automated checks. Read the reported artifact paths and warnings.

## Resume and failures

Use `status` and existing receipts; do not rerun `init` into an existing project. The state is durable and commands are locked against concurrent mutation/paid dispatch. After a crash, a lock or dispatched receipt requires investigation; never auto-delete it or reissue a possibly billed request.

Machine-readable JSON output contains a schema version, status and actionable blockers. A nonzero exit is failure, even when JSON describes a recoverable blocker. The initial CLI uses exit `2` for expected blocked/usage cases, including blocked `status`; parse `error_code` to distinguish them, not guessed exit-code meanings. **Doctor is diagnostics-only:** it may return exit `0` while reporting missing optional capabilities in JSON; read those capabilities/blockers. Help/instructions and a serving preview can contain ordinary terminal output.

Credential presence, model availability and software licenses are separate concerns. Software additions follow AGPL-3.0-or-later; renderer/dependencies and source content retain their own terms. No content upload, public hosting or paid retries are implied. See [agents.md](agents.md) for approval responsibilities and [source fidelity](source-fidelity.md) for HTML conversion limits.
