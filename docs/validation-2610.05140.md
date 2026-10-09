# AutoSciBench v1 fidelity validation

Acceptance case: **AutoSciBench: Autonomous Benchmark Generation for Evaluating Scientific Agents**, arXiv **2610.05140v1**, checked on **2026-10-07**. Full version-pinned native HTML and the **46-page PDF** were downloaded separately through bounded ingestion, identified against their contents, and hashed before changes. No AI annotations were generated.

## Bounded experiment

The user initially authorized six candidate trials / 90 minutes and later explicitly extended the work by two candidates / 30 minutes for two focused review findings, preserving existing uncommitted work, without commits/pushes. Eight candidates were evaluated against the same source article and fixed offline evaluator. The initial resource inventory omitted seven raster dependencies inside foreignObject labels; these were subsequently captured through guarded ingestion, the original inventory retained, and candidate 1 rebaselined (unchanged 60/64) before candidate 2. Original HTML/PDF and the 64-object denominator never changed; no workload cases were removed.

| Run | Source visual objects rasterized | Missing placeholders | Visual qualification |
| --- | --- | --- | --- |
| Baseline with Cairo working | 7 / 64 | 57 | Appendix task/prompt panels missing |
| Candidate 1 | 60 / 64 | 4 | Not promoted: some tables/headers clipped |
| Candidate 2 | 64 / 64 | 0 | Not promoted: malformed prompt geometry caused unreadable shrinking |
| Candidate 3 | 64 / 64 | 0 | Provisional: independent review found image amplification and mobile overflow |
| Candidate 4 | 49 / 64 | 15 | New readability floor exposed additional malformed short-card geometry |
| Candidate 5 | 46 / 64 | 18 | Reflow/controls added; strict frame profile omitted a near-white source fill |
| Candidate 6 | 64 / 64 | 0 | 33 malformed gray-card panels explicitly reflowed; weighted budgets, four-edge checks, local table/image scrolling and seven original-image downloads |
| Candidate 7 | 64 / 64 | 0 | Initial positive frame classifier and listing whitespace preservation; provisionally valid pending tightened frame bounds |
| Candidate 8 | 64 / 64 | 0 | Complete positive frame classification and literal-space/tab retention; first overly tight header-bound pass failed 15/64 and was corrected within the candidate before final publication |

**64 means outer source image/SVG objects, not 64 main figures.** Seven scientific raster images are nested inside four of those objects.

## Source checks

- Exact whitespace-normalized article text outside visual objects and redundant TeX annotations.
- All **98 source section elements**, **12 HTML tables**, **34 captions**, and **44 native MathML expressions** outside visuals retained in source order; source IDs and bibliography preserved.
- All **57 LaTeXML panels** reconstruct **50,465 non-whitespace label characters**, seven nested images, and the panel MathML without textual/image-count loss. Additionally, **684/684 source SVG listing lines** retain their exact text including literal spaces/tabs after removal of one verified terminal exact `\n` LaTeXML pretty-print separator per line. This comparison excludes only redundant TeX annotations and listing-download UI; it is not a pixel-equivalence claim.
- Every retained raster is bounded, validates and is nonblank. Representative table, clinical-image and long prompt panels were inspected; counts alone were insufficient to accept candidates 1/2.
- The fresh live CLI output also has **64 embedded images and zero missing-image placeholders**.
- Existing 2608.08528 figure renders at **910 × 361**, with no reflow; existing browser/security regression fixtures remain confirmation cases.

Extension final gates: **675 full offline tests passed** (18 opt-in skips; before the final marker-spoof fixture), **64 explicitly enabled focused browser tests passed** and a wider **92-browser/source-fidelity test subset passed** before the last narrow fixture. Ruff lint and format checks passed. The unchanged fixed v1 evaluator reports **64/64 images**, zero placeholders, all content/ID/reference/offline checks true and 33 reflow markers. Independent focused security review is still requested; counts alone do not establish pixel equivalence.

## Reproduction

```bash
# macOS/Homebrew loader setting; install Cairo/Playwright as documented in README.
DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib uv run paper2html process \
  https://arxiv.org/abs/2610.05140 --source-only -o outputs/2610.05140.html
```

The general CLI still normalizes an arXiv ID to the latest revision; this command is not a version-pinning guarantee. The experiment used separately frozen explicit v1 HTML/PDF for repeatable comparison, and checked the live result against that source. Keep a fresh source package for future revisions.

Local/ignored evidence: [contract](../outputs/experiments/fidelity-2610.05140/CONTRACT.md), [trial ledger](../outputs/experiments/fidelity-2610.05140/LEDGER.md), [final metrics](../outputs/experiments/fidelity-2610.05140/trial-08/metrics.json), [panel text comparison](../outputs/experiments/fidelity-2610.05140/panel-text-verification.json), [source manifest](../outputs/experiments/paper-video-2610.05140/sources/manifest.json), and [reader](../outputs/2610.05140.html). These artifacts are not supplied by a clean clone.

## Limits

**Focused independent re-review approved the final guard for the bounded paper-specific claim.** The original geometry/whitespace defects and subsequent sibling-label-loss case are resolved. An exact single-container/single-content guard rejects visible siblings, direct text and duplicate content before destructive reflow; the reviewer confirmed the original sibling-header and nested duplicate-content cases fail closed. The frozen paper's output remains byte-identical. Final repository gates passed: **676 offline tests and 15 explicitly enabled browser tests**, plus Ruff lint/format. The two reported review cases have been addressed in the user-authorized two-candidate/30-minute extension: reflow requires exhaustive *positive* inert-frame classification before any SVG removal, and only an exact terminal listing pretty-print newline is stripped (other literal whitespace is retained). Mutation tests reject extra rectangles, arbitrary gray paths, non-inert strokes and bad bounds. The frame profile intentionally rejects unfamiliar diagrams rather than discarding unclassified geometry. Node/text counts still do not certify every visual detail.

This establishes bounded coverage for this source revision, **not universal SVG support or publisher/PDF layout equivalence**. Native MathML outside graphics stays native; math/text inside a rasterized graphic is visual content, not selectable source math. Unsupported/active constructs and resource/budget violations still fail closed or produce explicit omission warnings.

Eighteen arXiv prompt SVGs encode roughly 14-pixel label boxes around long listings; another fifteen short-card panels fail the readability floor. All 33 are explicitly labeled **“Source panel reflowed for readability”**; original text is retained, but wrapping/geometry changes. Rendered-panel text/images are checked against all four edges. Thumbnail clinical annotations can still be small: original-sized panel viewing and seven exact, self-contained original nested-raster downloads provide access without claiming thumbnail legibility or pixel-perfect PDF reproduction. The original full PDF remains authoritative for exact typography and placement. The source-order reader keeps the native article's placement: some native prompt captions and panels are separate, and legacy normalized figure records warn that they lack an associated image even though all source-order visual objects are present. This validation does not claim those normalized associations were repaired.

The source-only conversion is free of provider calls. Script/TTS/video generation is a separate approval-gated workflow, not evidence of paper-conversion completeness.
