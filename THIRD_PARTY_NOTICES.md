# Third-party notices

The project code is licensed under **AGPL-3.0-or-later**. Third-party components retain their own licenses; this project's license does not replace them.

## Vendored KaTeX

`src/paper2html/assets/katex/` contains KaTeX **0.17.0**, including its JavaScript, CSS and font data embedded in the CSS.

- Upstream: https://github.com/KaTeX/KaTeX/tree/v0.17.0
- License: MIT
- Copyright: © 2013–2020 Khan Academy and other contributors
- Full notice: [`src/paper2html/assets/katex/LICENSE`](src/paper2html/assets/katex/LICENSE)
- Modifications: font resources are embedded into the CSS for self-contained, offline HTML output.

## Installed dependencies

Dependencies are installed separately and pinned in `uv.lock`; consult each distribution's license before redistribution. In particular, PyMuPDF/MuPDF is offered under AGPL and commercial licensing terms, and CairoSVG is LGPL-3.0-or-later. Using a different license for application code would not waive those dependency obligations.

## Optional video renderer

The staged video CLI can explicitly install the published **HyperFrames 0.8.142** npm package into a user project. Its published package metadata declares **Apache-2.0**, with Node **22+** required. It is an installed dependency, not vendored code or an embedded agent skill. Consult its package license and its transitive dependencies before redistribution; the Python package's AGPL license does not replace their terms. Renderer/bootstrap network behavior is separate from source conversion.

The native video implementation does not copy globally installed HyperFrames/media-use skills or vendor GSAP/MotionPathPlugin from the experiments. Source-first templates and project assets are owned by this package/run; paper-content rights remain separate.

## Papers and generated documents

Paper text, figures, and other source material retain the rights and licenses of their respective authors/publishers. The software license does **not** grant permission to redistribute a paper. Generated papers and downloaded PDFs are excluded from this repository by default.
