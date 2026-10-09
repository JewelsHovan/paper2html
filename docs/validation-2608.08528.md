# Native HTML fidelity check: arXiv 2608.08528

Live source-only conversion of **Task-to-Model Optimization for Enterprise LLM Coding Assistants: A Data-Driven Framework for Cost-Optimal Routing**, checked on 2026-09-29. No model calls or paid annotations were used.

```bash
uv sync --group browser
uv run playwright install chromium
DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib uv run paper2html process \
  https://arxiv.org/abs/2608.08528 --source-only -o outputs/2608.08528.html
```

The original conversion rejected Figure 1 because its inline SVG contains 54 `foreignObject` labels, including 17 MathML expressions. Merely allowing that tag in Cairo would not preserve the labels. Supported static labels now use strict reconstruction followed by offline, JavaScript-disabled Chromium rasterization. Other SVGs retain the Cairo path. The validated raster is reused by ingestion, the normalized figure record and the source view.

## Source-to-output checks

| Content | Result |
| --- | --- |
| Sections | All 26 retained |
| Article text outside the SVG | Exact whitespace-normalized match, 37,515 characters; redundant TeX annotations excluded |
| Article MathML | All 84 expressions outside the figure retained |
| Figure | One embedded PNG, 910 × 361; no missing-image placeholders |
| Figure labels | All 54 label strings match the strictly reconstructed browser input in order |
| Figure math | All 17 MathML expressions retained in the rasterizer input and displayed within the figure image |
| Tables | All 8 HTML tables retained, including equation layouts |
| Captions | All 5 retained |
| References | All 9 retained |
| Offline browser | No unexpected requests, failed requests or page errors |
| Responsive layout | Document width equals viewport width at 390 and 1280 pixels; wide tables/equations scroll inside keyboard-focusable regions |

The rendered diagram was visually checked against the diagram on PDF page 8. It retains the branches, labels, math and shaded cells. Fonts and label metrics are not identical to the PDF or publisher website: local serif/math fonts replace publisher fonts. SVG labels are embedded image content, not selectable/searchable article math. This is deliberately a static PNG, not active inline SVG.

The downloaded PDF has 12 pages; arXiv's abstract-page comments list 11. A separate PDF-facsimile conversion preserves all 12 pages and searchable extracted text. The native HTML conversion is the preferred flowing reader for this paper now that the figure is included.

Generated source captures, images, HTML and machine-readable comparison reports remain under ignored `outputs/`; regression tests use synthetic figures only. These checks establish coverage for this paper, not universal support for arbitrary HTML inside SVG. Unsupported elements/styles and unavailable Chromium still produce explicit omission warnings.
