#!/usr/bin/env python3
"""Bounded deterministic offline baseline. Each sample owns a fresh child and tmpdir."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import random
import resource
import statistics
import subprocess
import sys
import tempfile
import time
from io import BytesIO

import pymupdf
from PIL import Image
from bs4 import BeautifulSoup

from paper2html.ingestion.pdf import ingest_pdf
from paper2html.models.document import (
    DocumentMetadata,
    Figure,
    IngestionPath,
    NormalizedDocument,
    Section,
)
from paper2html.pipeline.providers import MockProvider
from paper2html.pipeline.runner import run_pipeline
from paper2html.pipeline.schemas import PipelineConfig
from paper2html.renderer import render_html
from paper2html.review.glue import attach_reading_aids
from paper2html.review.schemas import AnnotationBundle

WORKLOADS = {
    "small": {"pages": 2, "sections": 3, "words": 1000, "images": 0},
    "long": {"pages": 40, "sections": 40, "words": 20000, "images": 0},
    "image-heavy": {"pages": 8, "sections": 16, "words": 4000, "images": 16},
}


class ObservedMock(MockProvider):
    active = 0
    peak = 0

    async def complete(self, *args, **kwargs):
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0)
            return await super().complete(*args, **kwargs)
        finally:
            self.active -= 1


def timed(function):
    start = time.perf_counter()
    value = function()
    return value, time.perf_counter() - start


def fixture(root, params):
    rng = random.Random(1729)
    figures = {}
    for i in range(params["images"]):
        stream = BytesIO()
        Image.frombytes("RGB", (512, 512), rng.randbytes(512 * 512 * 3)).save(
            stream, format="PNG"
        )
        fid = f"fig-{i}"
        figures[fid] = Figure(
            id=fid,
            caption=f"Seeded figure {i}",
            image_bytes=stream.getvalue(),
            image_mime_type="image/png",
        )
    sections = []
    for i in range(params["sections"]):
        count = params["words"] // params["sections"] + (
            i < params["words"] % params["sections"]
        )
        sections.append(
            Section(
                id=f"sec-{i + 1}",
                title=f"Section {i + 1}",
                level=1,
                text_content=" ".join(["term_a"] * count),
                figure_ids=[f"fig-{i}"] if i < len(figures) else [],
            )
        )
    doc = NormalizedDocument(
        metadata=DocumentMetadata(
            title="Offline benchmark", authors=[], ingestion_path=IngestionPath.PDF
        ),
        sections=sections,
        figures=figures,
    )
    pdf = root / "fixture.pdf"
    with pymupdf.open() as source:
        for i in range(params["pages"]):
            page = source.new_page()
            # Fit bounded text in the page; normalized workload independently fixes word count.
            page.insert_textbox(
                pymupdf.Rect(40, 40, 550, 760),
                " ".join(["term_a"] * (params["words"] // params["pages"])),
                fontsize=9,
            )
            for j, figure in enumerate(list(figures.values())[i * 2 : i * 2 + 2]):
                page.insert_image(
                    pymupdf.Rect(40 + j * 260, 480, 280 + j * 260, 720),
                    stream=figure.image_bytes,
                )
        source.save(pdf)
    return pdf, doc


def sample(name):
    params = WORKLOADS[name]
    with tempfile.TemporaryDirectory(prefix="paper2html-benchmark-") as directory:
        root = Path(directory)
        pdf, doc = fixture(root, params)
        cache = root / "cache"
        cold, ingest_cold = timed(lambda: ingest_pdf(pdf, cache))
        artifacts = {
            p: p.stat().st_mtime_ns
            for p in cache.rglob("*")
            if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".pdf"}
        }
        assert artifacts
        warm, ingest_warm = timed(lambda: ingest_pdf(pdf, cache))
        assert cold.page_count == warm.page_count == params["pages"]
        assert all(p.stat().st_mtime_ns == mtime for p, mtime in artifacts.items())
        provider = ObservedMock()
        config = PipelineConfig(provider_name="mock")
        result, pipeline_cold = timed(
            lambda: run_pipeline(doc, config, provider, cache)
        )
        cold_calls = provider.call_count
        cached, pipeline_warm = timed(
            lambda: run_pipeline(doc, config, provider, cache)
        )
        assert provider.call_count == cold_calls and cold_calls > 0
        assert result.complete and cached.complete
        assert (
            len(result.section_analyses)
            == len(cached.section_analyses)
            == params["sections"]
        )
        assert len(doc.figures) == params["images"]
        assert (
            sum(len(section.text_content.split()) for section in doc.sections)
            == params["words"]
        )
        bundle = attach_reading_aids(AnnotationBundle(items=[]), result)
        output, render = timed(lambda: render_html(doc, bundle, root / "paper.html"))
        soup = BeautifulSoup(output.read_bytes(), "html.parser")
        assert len(soup.select("main section")) == params["sections"]
        assert len(soup.select("img")) == params["images"]
        digest = hashlib.sha256(output.read_bytes()).hexdigest()
        again = render_html(
            doc,
            attach_reading_aids(AnnotationBundle(items=[]), cached),
            root / "again.html",
        )
        assert again.read_bytes() == output.read_bytes(), "cold/warm render changed"
        # A third render isolates renderer determinism from pipeline cache replay.
        assert (
            render_html(doc, bundle, root / "third.html").read_bytes()
            == output.read_bytes()
        )
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return {
            "ingestion_cold_seconds": ingest_cold,
            "ingestion_warm_seconds": ingest_warm,
            "pipeline_cold_seconds": pipeline_cold,
            "pipeline_warm_seconds": pipeline_warm,
            "render_seconds": render,
            "peak_rss_bytes": rss if sys.platform == "darwin" else rss * 1024,
            "output_bytes": output.stat().st_size,
            "pdf_bytes": pdf.stat().st_size,
            "image_payload_bytes": sum(
                len(f.image_bytes or b"") for f in doc.figures.values()
            ),
            "normalized_payload_bytes": sum(
                len(s.text_content.encode()) for s in doc.sections
            ),
            "max_observed_concurrency": provider.peak,
            "provider_calls": cold_calls,
            "warm_provider_calls": provider.call_count - cold_calls,
            "sections": len(doc.sections),
            "figures": len(doc.figures),
            "sha256": digest,
        }


def metadata():
    def git(*args):
        return subprocess.check_output(["git", *args], text=True).strip()

    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "revision": git("rev-parse", "HEAD"),
        "dirty_tree": bool(git("status", "--porcelain")),
        "caveat": "Working-tree baseline, not a release or live-provider performance claim. Portable timing comparisons are advisory only.",
        "versions": {
            name: importlib.metadata.version(name)
            for name in ("paper2html", "pymupdf", "pillow", "pydantic")
        },
        "runner_id": os.environ.get("PAPER2HTML_BENCHMARK_RUNNER", "un-pinned"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--quick", action="store_true", help="one small correctness sample"
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--baseline", type=Path, help="advisory comparison; no portable timing gate"
    )
    parser.add_argument("--sample", choices=WORKLOADS, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.sample:
        print(json.dumps(sample(args.sample)))
        return
    started = time.monotonic()
    report = {"metadata": metadata(), "workloads": WORKLOADS, "results": {}}
    for name in ["small"] if args.quick else WORKLOADS:
        samples = []
        for _ in range(1 if args.quick else 3):
            remaining = 360 - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError("six minute benchmark limit exceeded")
            process = subprocess.run(
                [sys.executable, __file__, "--sample", name],
                capture_output=True,
                text=True,
                timeout=min(60, remaining),
                check=True,
            )
            samples.append(json.loads(process.stdout))
        assert len({s["sha256"] for s in samples}) == 1, (
            "sample output is not deterministic"
        )
        report["results"][name] = {
            "samples": samples,
            "medians": {
                key: statistics.median(s[key] for s in samples)
                for key in samples[0]
                if key != "sha256"
            },
        }
    if args.baseline:
        previous = json.loads(args.baseline.read_text())
        report["advisory_ratios"] = {
            name: {
                key: values["medians"][key] / previous["results"][name]["medians"][key]
                for key in values["medians"]
                if key.endswith("_seconds")
            }
            for name, values in report["results"].items()
            if name in previous["results"]
        }
    text = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(text)
    print(text, end="")


if __name__ == "__main__":
    main()
