# Historical paper-to-video demo

**For new installations, start with the native [video CLI guide](video.md) and [portable agent guide](agents.md).** The CLI does not require Compass or external agent skills. This document retains the earlier hand-reviewed, harness-specific demonstration as historical workflow evidence, not the product's setup contract.

The earlier demo was a reusable **agent-operated skill**, not an unattended command-line converter.

The project skill is [`.compass/skills/paper-video/SKILL.md`](../.compass/skills/paper-video/SKILL.md). It composes the installed HyperFrames/media-use skills with full-paper grounding and source/encode checks. It is user-invoked because fresh runs can make paid API calls.

## Load it

In a trusted Compass session opened in this repository:

```text
/reload
/skills paper-video
```

If another command owns the name, use `/skill:paper-video`. These are **chat slash commands**, not terminal commands. A new session also discovers the skill; simply creating the file does not load it into an existing session.

## Start the next demo

```text
/paper-video https://arxiv.org/abs/NEW_PAPER_ID
```

Or supply your preferences inline:

```text
/paper-video https://arxiv.org/abs/NEW_PAPER_ID two-minute landscape technical walkthrough; include the most useful original diagram and table; review script first
```

A local PDF plus native HTML can be supplied instead; include paths and identify the matching revision. The agent asks only for missing information and confirms paid calls for this new run. Credentials remain in the environment; do not paste them into chat or files.

Recommended first-demo shape: approximately two minutes, 16:9, English technical narration, captions, no music. This is a recommendation, not a permanently locked format.

## Continue after each approval

```text
/paper-video resume outputs/experiments/paper-video-NEW_PAPER_ID
```

“Looks good, proceed” accepts a displayed checkpoint while the workflow remains active. In a new session or after context loss, use the explicit resume command: this user-only skill cannot reload itself through the model's skill tool. The command reads project state rather than starting over.

Read-only status:

```text
/paper-video status outputs/experiments/paper-video-NEW_PAPER_ID
```

After reviewing the animated preview with sound:

```text
/paper-video render outputs/experiments/paper-video-NEW_PAPER_ID
```

This authorizes local MP4 rendering of the reviewed current preview, not a public upload.

## What the demo does

1. Download the **full native article and full PDF** separately from matching versioned URLs through bounded ingestion transport, using fresh per-run storage. Identify the actual revision and record source hashes; reader conversion alone does not provide both files.
2. Send both to a multimodal model for a coherent teaching script with source evidence.
3. Check claims, equations, example arithmetic, original visuals, quotations and limitations. Keep raw model output and reviewed corrections separate.
4. Pause for script/storyboard approval; optional visual sketches.
5. Generate narration with the provider/model/voice confirmed for this run: recommend **Gemini TTS**, proposed model `gemini-3.8-flash-tts` while available. Supported alternatives are allowed when explicitly selected. Measure audio and align captions.
6. Build original-paper visuals and speech-paced animation with HyperFrames; inspect every scene and moving-element text clearance.
7. Pause at a checked playable preview for final render approval.
8. Render and verify the actual MP4, including encoded source visuals, audio, duration and contact sheet.

## Requirements and limits

- Existing project Python environment/ingestion tools for paper conversion.
- Installed HyperFrames workflow/domain skills, Node 22+, FFmpeg, local Chromium and working Gemini credentials/access. Missing dependencies are discovered and surfaced, not silently bypassed.
- Review original-visual attribution/license constraints before sharing; a public PDF URL is not blanket redistribution permission.
- Runtime model IDs and helpers are checked against current documentation and installed behavior. Project commands use the inspected CLI version pin, not bare `npx hyperframes`.
- HyperFrames initialization checks skills via GitHub and can record telemetry attribution. Those setup effects are disclosed before running; follow the installed opt-out procedure if required.
- Outputs stay under ignored `outputs/experiments/`, separately for each paper. No commit, root-code edit, cloud render or public upload is implied.
- This is reusable workflow knowledge with approval checkpoints. Models can still make errors; new paper quality is not guaranteed by the successful previous demo.

## Proven example

The first paper's experiment notes, refinements and verified MP4 live locally under `outputs/experiments/paper-video-2608.08528/`. The later AutoSciBench evidence is under `outputs/experiments/paper-video-2610.05140/`. These are intentionally ignored, non-shipping artifacts—not working links in a fresh checkout. The verified videos are approximately two minutes, with original diagrams/tables and Gemini narration.

Those outputs are ignored and may not exist in another checkout. Its crop coordinates, source IDs, exact quotes, narration, numeric example and proof timestamps must **not** be copied as generic defaults for a new paper.
