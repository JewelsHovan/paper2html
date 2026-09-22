# Offline baseline, not an optimization claim

```bash
uv sync --frozen --group dev
# macOS native SVG support, if needed:
export DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib
uv run python benchmarks/offline_pipeline.py --quick
uv run python benchmarks/offline_pipeline.py --output /tmp/new-baseline.json
uv run python benchmarks/offline_pipeline.py --baseline benchmarks/baseline.json
```

`--quick` runs one small child-process sample and checks correctness, not timing. The default runs three samples of each workload, each in a fresh process and temporary directory outside the repository. Every child has a 60-second timeout; the parent has a six-minute total allowance. No user cache, live arXiv, paid provider, Node toolchain, or root HTML fixture is used. The script uses project dependencies and Python's standard library only.

Workloads are fixed: small = 2 PDF pages / 3 normalized sections / 1,000 words; long = 40 pages / 40 sections / 20,000 words; image-heavy = 8 pages / 16 sections / 4,000 words / 16 distinct seeded, valid 512×512 RGB PNGs. The PDF and normalized document are parallel fixtures: ingestion is measured on the real generated PDF, while pipeline/render use the normalized fixture to keep section/word counts independent of PDF extraction heuristics. No fixture construction time is included in stage timings. RSS includes fixture construction, all measured stages and validation.

For each sample we measure cold/warm ingestion, cold/warm normalized MockProvider pipeline and cold render, output/PDF/text/image bytes, provider calls and section/figure counts. An instrumented MockProvider yields once to observe concurrent admitted calls; this is not remote provider latency. Peak RSS uses `resource.ru_maxrss`, bytes on macOS and KiB converted to bytes on Linux. It is process peak, not incremental stage memory.

Before reporting, checks require expected page/section/word/figure counts, expected rendered section/image counts, complete pipeline results, zero additional warm provider calls, unchanged ingestion artifact mtimes, identical cold/warm/repeated HTML bytes and identical output hashes across repetitions. No dynamic HTML timestamps currently require exclusion. SQLite bookkeeping mtimes are not ingestion artifact mtimes. Source PDF bytes are reported, not compared as deterministic PDFs (PDF metadata may vary).

`baseline.json` contains actual samples, medians, dependency/Python/platform versions, revision, workload parameters, and a **dirty working-tree caveat**. The baseline is not a release measurement. `--baseline` emits advisory ratios only; it never enforces portable timing guarantees. There is intentionally no timing threshold in CI. Any future enforcement must use an explicitly pinned matching runner, dependency/workload versions, and configurable tolerances; an optional `PAPER2HTML_BENCHMARK_RUNNER` label is recorded but does not by itself pin hardware.

## Recorded local medians

macOS 26.6.2 arm64, Python 3.11.14, dirty revision `0b88f7311e4af981b548e4ddcfeadb0bee671319`; three isolated samples per workload. Seconds below are rounded; JSON retains full precision.

| Workload | Ingest cold / warm | Pipeline cold / warm | Render | Peak RSS bytes | HTML bytes | Calls cold / warm | Peak concurrency |
|---|---:|---:|---:|---:|---:|---:|---:|
| small | 0.1353 / 0.0246 | 0.0099 / 0.0025 | 0.0090 | 132,759,552 | 26,039 | 5 / 0 | 3 |
| long | 2.5156 / 0.4756 | 0.0142 / 0.0032 | 0.0625 | 136,151,040 | 181,678 | 42 / 0 | 4 |
| image-heavy | 1.6361 / 0.7059 | 0.1663 / 0.0337 | 0.0956 | 449,216,512 | 16,868,526 | 18 / 0 | 4 |

These are baseline observations, not measured improvements. The capture shared the machine with the offline regression suite; it is not a dedicated performance runner. No production optimization trials or live-provider performance tests were performed.
