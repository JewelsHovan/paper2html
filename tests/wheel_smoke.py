"""Run with an isolated wheel-installed Python, not the editable checkout.

No model calls, source downloads, browser provisioning, or video encoding.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

import pymupdf


def main() -> None:
    executable = Path(sys.executable).parent / "paper2html"
    env = os.environ.copy()
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        env.pop(key, None)
    # Typer forces styled terminal output under GitHub Actions; ANSI codes would
    # split option names such as --source-only in the plain-text help checks.
    for key in ("GITHUB_ACTIONS", "FORCE_COLOR", "PY_COLORS"):
        env.pop(key, None)
    env.update(NO_COLOR="1", TERM="dumb", COLUMNS="200")
    ansi = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

    def cli(*args: str, expected_exit: int = 0) -> str:
        result = subprocess.run(
            [str(executable), *args],
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        assert result.returncode == expected_exit, (result.returncode, result.stderr)
        return ansi.sub("", result.stdout)

    assert "process" in cli("--help")
    assert "source-only" in cli("process", "--help")
    assert "doctor" in cli("video", "--help")
    assert cli("video", "instructions").strip()
    with tempfile.TemporaryDirectory(prefix="paper2html-wheel-smoke-") as temporary:
        root = Path(temporary)
        source = root / "source.pdf"
        target = root / "source.html"
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=480, height=640)
            page.insert_text((36, 50), "Wheel installation source proof")
            page.draw_rect(pymupdf.Rect(36, 80, 180, 140), color=(0, 0, 1))
            pdf.save(source)
        cli(
            "--cache-dir",
            str(root / "cache"),
            "process",
            str(source),
            "--source-only",
            "-o",
            str(target),
        )
        reader = target.read_text()
        assert "Wheel installation source proof" in reader
        assert "data:image/" in reader
        project = root / "video"
        initialized = json.loads(
            cli("video", "init", str(project), "--source", str(source), "--json")
        )
        assert initialized["status"] == "ready"
        assert (project / "sources" / "manifest.json").is_file()
        status = json.loads(
            cli("video", "status", str(project), "--json", expected_exit=2)
        )
        assert status["status"] == "blocked"  # No script and no implicit paid work.
        assert status["next_actions"]
    print(
        "Installed-wheel help, instructions, free PDF conversion and video project/status passed."
    )


if __name__ == "__main__":
    main()
