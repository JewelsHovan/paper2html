"""Fast correctness smoke: no machine-dependent timing assertions."""

import json
from pathlib import Path
import subprocess
import sys


def test_small_offline_benchmark_correctness():
    script = Path(__file__).resolve().parents[1] / "benchmarks" / "offline_pipeline.py"
    result = subprocess.run(
        [sys.executable, str(script), "--quick"],
        capture_output=True,
        text=True,
        check=True,
        timeout=65,
    )
    report = json.loads(result.stdout)
    sample = report["results"]["small"]["samples"][0]
    assert sample["warm_provider_calls"] == 0
    assert sample["sections"] == 3 and sample["figures"] == 0
    assert 1 <= sample["max_observed_concurrency"] <= 4
    assert sample["peak_rss_bytes"] > 0 and sample["output_bytes"] > 0
