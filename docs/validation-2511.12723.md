# Live source-fidelity check: arXiv 2511.12723

Validated against the native HTML and PDF for **LAYA: Layer-wise Attention Aggregation for Interpretable Depth-Aware Neural Networks** (version 2). This was an explicitly authorized live check; it is not part of the default test suite.

```bash
# Native Cairo is required for the external SVG figure.
# On affected macOS/Homebrew installations:
export DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib
mkdir -p outputs
uv run paper2html --cache-dir outputs/.cache process \
  https://arxiv.org/pdf/2511.12723 --source-only -o outputs/2511.12723.html
```

No model calls, API keys, mock summaries, or paid annotations were used.

## Source-to-output checks

| Content | Verified result |
|---|---|
| Sections | 29 retained |
| Paragraphs | All 102 match in source order |
| MathML | All 260 elements retained |
| Figures | All 3 embedded and decoded successfully |
| Captions | All 6 match in order (figures and tables) |
| Tables | All 3 retained; row/cell counts 7/42, 13/56, 16/68; table text matches |
| References | All 38 match in order |
| Whole article text | 57,957 characters match after whitespace normalization and removal of redundant TeX annotations from both sides |
| Internal links | No unresolved fragment targets |
| Offline browser | No unexpected resource requests, failed requests, or page errors |

Whole-article comparison includes title, authors, affiliation, contact information, keywords, and the declaration/acknowledgment sections. Original external hyperlinks remain navigable but are not fetched to display the document.

The previously missing Figure 2 uses `<object data="…svg">`; it is now safely converted to a 972×300 PNG. The other two source PNGs retain their original bytes and dimensions (1024×554 and 1814×1574). The native-HTML output was approximately 749 kB.

The same downloaded PDF also passed the source-only fallback check: **22 complete page images and 22 separate searchable text sections**, including vector figures. That output was approximately 10.5 MB and is explicitly a page facsimile, not a semantic reconstruction of equations.

## Scope and limits

These checks establish content coverage for this paper/version, not universal fidelity for every PDF or publisher format. Publisher CSS/typography is not reproduced. Source HTML is reconstructed through the security allowlist; PDF text extraction can have ordering limitations, while its page images preserve the visual source.

The paper, generated HTML, screenshots, downloaded assets and machine-readable validation report remain local under ignored `outputs/`; they are not redistributed in this repository. Synthetic offline regressions cover the SVG-object, source-order, MathML, front-matter, PDF-facsimile and safe-link behaviors.
