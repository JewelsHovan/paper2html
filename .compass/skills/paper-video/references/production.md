# HyperFrames production and delivery

Follow the installed HyperFrames workflow for the current version. This reference adds lessons from the real-paper experiment, not a second animation framework. Names below identify existing tools; they do not introduce a `paper-video` executable.

## Resolve the installed owners

Load `hyperframes` first. A short research-paper explanation normally routes to `faceless-explainer`; longer/custom pieces use the owning route it selects. Let the confirmed `BRIEF.md` settle routing, narration, format and review preferences. Read the workflow's current `SKILL.md` and required command references before executing its helpers.

Discover actual installed skill directories; do not copy this machine's absolute `/Users/...` paths into a reusable project. Use `SKILL_DIR`, `WORKFLOW_DIR` and `MEDIA_DIR` as resolved local variables in commands. If required skills are absent, follow the HyperFrames installer's documented procedure, surface its global-install effects, and do not invent a missing workflow.

Initialize a NEW empty project with the documented HyperFrames `init` command at an explicitly selected current version, then write `BRIEF.md` immediately. Before initialization, tell the user that `init` checks/refreshes skills via GitHub and the workflow/CLI may record telemetry attribution; follow the installed opt-out guidance if they decline those effects. It is not an entirely offline scaffold. Keep all generated files and per-run adapters in that project. The old experiment's scripts are examples only: do not run its hardcoded `prepare_review.py`, `plan_visuals.py`, source crops or precision checks against another paper.

Before a project exists, choose an available CLI version from current package metadata. For a two-minute faceless route, the shape is `npx --yes "hyperframes@$CHOSEN_VERSION" init "$PROJECT" --non-interactive --example=blank --resolution landscape --skill=faceless-explainer`; replace the version, project path, route and resolution with the confirmed values. After initialization read the scaffold's actual pin, rather than assuming it equals the requested version.

For resumptions, inspect history and current files before changing them. Follow current HyperFrames history conventions. Probe the CLI pin via `npx hyperframes@latest upgrade --project . --check` (this intentional unpinned maintenance probe is the exception). If updating, verify with the new version and report the version change; preserve the pin when verification fails. Don't advance the pin silently after preview approval without rechecking the reviewed composition.

**All ordinary project commands must use the pin.** Read the scaffold's `package.json` scripts and assign `HF_VERSION` from their exact `hyperframes@X.Y.Z` pin; never guess it or silently run bare `npx hyperframes`. Use `npm run check -- ...`, `npm run render -- ...` where present, or `npx --yes "hyperframes@$HF_VERSION" <command> ...` for preview, snapshot, timeline, history and media adoption. Resolve the pin again after a verified upgrade. Commands below use that inspected `HF_VERSION`.

## Narration after script approval

Load `media-use` and its current audio/TTS docs. The brief must explicitly confirm Gemini for this run (or record the user's requested supported alternative). Verify credentials and the chosen Gemini model's availability. Proposed narration model: `gemini-3.8-flash-tts`; use that exact model only while supported. An unavailable model is a blocker or a choice for the user, not silent fallback.

A neutral shared audio-engine request looks like:

```json
{
  "provider": "gemini",
  "tts_model": "gemini-3.8-flash-tts",
  "voice": "Kore",
  "lang": "en",
  "style": "Clear neutral teaching voice, unhurried. Pause between concepts. Do not add words or speak these directions.",
  "lines": [{"id": "01", "text": "THE CURRENT APPROVED SCENE NARRATION"}],
  "bgm": {"mode": "none"}
}
```

This is a **shape example**, not text to synthesize. Generate one line per approved scene. Resolve a matching voice rather than blindly carrying over `Kore` when the brief says otherwise. Delivery directions belong in style/structured metadata, never the transcript.

Current shared-engine invocation (verify against installed docs):

```bash
node "$MEDIA_DIR/audio/scripts/audio.mjs" \
  --request ./audio_request.json --hyperframes . \
  --out ./audio_engine_meta.json --only tts
```

Important: a previous faceless workflow helper documented a Gemini flag but actually hardcoded `provider: auto`. Inspect the current wrapper before using it. Prefer the documented shared engine with an explicit Gemini request when the wrapper cannot forward that selection. Do not patch global skills. If necessary, make a small run-local adapter translating the id-keyed output to the workflow's current metadata schema. The explicit Gemini safeguards apply only to a Gemini brief; use the owning documented adapter for an approved alternative, with the same no-silent-fallback and audio-verification rules.

Gemini 3.8 returned complete WAV in the tested API; older TTS versions used headerless PCM. Check the current response MIME/schema; don't wrap an existing WAV header twice. Keep local audio as immutable approved assets.

Validate the result, not just exit status:

- Requested provider/model/voice actually used; no silent local/HeyGen fallback.
- Every requested scene has usable audio. Read failures/anomalies; partial metadata is not success.
- Measure durations with ffprobe; use actual speech durations to update scene windows.
- Word timings are transcribed/aligned from the audio, ordered and in bounds. No uniform word-spacing guesses.
- Compare transcript to approved narration, including numbers, acronyms and grouped equations. Listen/review pronunciation where possible; automated transcripts are not a substitute for listening.
- Record usage if exposed; clearly separate estimated cost from invoices. Never report the script-call cost as the full video cost when TTS usage is unavailable.

## Build against the approved timing

Use the owning workflow's design spec, scene packets, isolated frame workers, caption generator, assembler and transition injector. One worker per independent scene; workers write only their scene file, not shared state. Carry the exact role/packet and keep-out constraints to each worker. Avoid a costly agent fan-out during a small two-scene demo when direct execution suffices; never let a worker invent claims or change narration.

Read `hyperframes-core`, `hyperframes-animation` and `hyperframes-keyframes` as applicable. Search the registry before rebuilding a named treatment. Preserve the user-approved design; don't redesign during final polish.

Demo-tested pitfalls:

- Assets in scene templates must use **project-root paths**, e.g. `assets/paper/figure.png`, not `../../assets/...`: Studio and compiled renders can resolve relative paths differently. Verify current lint behavior.
- Ship local fonts and GSAP/runtime assets for reproducible playback, with in-file font-face declarations. No render-time model calls, random values or external fetches.
- Styles/scripts belong inside templated sub-compositions; host composition ID and registered timeline key match. Prefix DOM IDs with a letter and scene identifier so CSS selectors are valid.
- The installed assembler expected `asset_candidates` as semicolon-delimited text such as `assets/figure.png — Original Figure 2`, not a JSON-array string. Inspect the current parser before writing metadata.
- Keep captions on a dedicated root track and reserve their band. No important diagram labels, citations or data under that band.
- Preserve full original figures/tables before focus. Never zoom so an edge cuts source glyphs. Prefer a non-occluding outline and an optional additional detail view.
- Put moving tokens/dots/cursors in a reserved lane, not over card text. Verify the swept area against actual text boxes through the whole move; a clean endpoint snapshot is insufficient.
- Tie statistics, equation answers, badges and limitations to their measured spoken cues, not guessed scene fractions. Keep units with their number and baseline/provenance visible.
- GSAP should seek correctly backward as well as forward. Repeated from/to tweens need stable baselines or `immediateRender: false` where appropriate. Test dynamic text, image rasterization and transition boundaries.
- Prefer held reads to decorative motion. Don't drift source images or add busy movement to satisfy a checker.

## Preview and approval

Run the owning CLI's **current** `check` command; read its JSON result and genuine findings. A shell pipeline exiting zero or zero browser samples after lint failure is not a pass. Fix errors and material warnings, then rerun the affected gate.

Inspect sampled assembled frames from EVERY scene, including the full figure/table, all numeric states, source quotes and final hold. Sample before/after important speech cues and at transitions. Check font sizes, contrast, formula grouping, persistent illustrative labels and source/caption clearance. Use motion assertions where useful; do not add random movement to game them.

Verify random-access seek poses, source-image containment and moving-element text clearance. If using pixel comparison, disclose any narrowly justified compositor antialiasing tolerance; never excuse changing text/geometry as raster noise.

Serve the documented managed preview:

```bash
npx --yes "hyperframes@$HF_VERSION" preview --background
```

Verify HTTP 200 and hand back the printed **Studio project URL**, not raw `index.html`. Keep the server alive through review. Ask: render now, or changes? A prior script approval is not final MP4 approval. Record approval against the checked current composition; respect user edits made in Studio.

## Approved render and actual-encode checks

Use the project's pinned wrapper/version and check current render help/reference:

```bash
npx --yes "hyperframes@$HF_VERSION" render --quality delivery --fps 30 --strict-all \
  --output renders/paper-explainer.mp4
```

Use the brief's FPS if different. Pick workers based on local resources; no cloud upload is implied. Preserve existing outputs or choose a new name. Save input hashes and render summary. Check the summary's capture mode, GPU mode and stage timings.

After success:

1. Require a nonempty MP4; inspect ffprobe streams, dimensions, FPS and video/audio duration against the composition and frame-grid rounding.
2. Decode the entire file with ffmpeg to catch corrupt packets/streams.
3. Check narration audio is present and not silent; review sync and loudness rather than assuming a stream means audible speech.
4. Extract proof frames from the **encoded video** at this video's own scene/critical times. Compare to approved snapshots; inspect source visuals, captions, moving dot, example states and final hold. Generic checks must derive times dynamically—old paper timestamps aren't reusable.
5. Save a verification manifest and MP4-derived contact sheet. Recheck approved input hashes if the project may have changed during rendering.
6. Provide clickable file paths and actual duration/size. Stop the managed preview after review ends. Do not upload/publicly publish without separate authorization.

Do not rerun CLI lint/check/snapshot after rendering unless requested or repairing an issue; ffmpeg encode inspection is the delivery check. Follow the installed skill's privacy/feedback policy, with no source upload or identifying paths.

## Demo checkpoints

- Missing native HTML: disclose and ask PDF-only versus a different paper.
- Missing Gemini auth/backend: stop with setup guidance; never switch voice silently.
- Factual uncertainty: flag it before speech.
- TTS failure/timeout: preserve outcomes and successful clips; avoid blanket paid retries.
- Preview changes: repair only named scenes, preserving audio and other approved content.
- Full MP4 is too large for an artifact: link the original MP4 separately and publish a lightweight contact-sheet report. Don't downsample the deliverable just to fit an artifact limit.
