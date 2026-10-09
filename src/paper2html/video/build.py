"""Offline HyperFrames-compatible source-page timeline with escaped text."""

from __future__ import annotations

import hashlib
import html
import os
import re
from pathlib import Path
import shutil
import subprocess
import wave

from .core import (
    Blocked,
    approval,
    digest,
    hashes,
    load,
    manifest_source,
    owned,
    put,
    save,
    unchanged,
)
from .script import check
from .source import page_asset

PIN = "0.8.142"


def runtime_tree_hash(root: Path) -> str:
    """Bind every installed dependency byte/path, including non-entrypoint code.

    npm's .bin symlinks are permitted only within node_modules; no external
    or broken references, directory links, generated renders, or project caches.
    """
    base = owned(root, "runtime/node_modules").resolve()
    if not base.is_dir():
        raise Blocked("RUNTIME_MISSING", "Project-local node_modules missing")
    result = hashlib.sha256()
    count = 0
    for parent, dirs, files in os.walk(base, followlinks=False):
        for name in sorted(dirs + files):
            entry = Path(parent) / name
            relative = entry.relative_to(base).as_posix()
            if entry.is_symlink():
                target = entry.resolve()
                if not target.is_relative_to(base) or not target.is_file():
                    raise Blocked(
                        "UNSAFE_PATH",
                        "Runtime dependency symlink escapes tree or targets a directory",
                    )
                # Include link destination and its bytes, not merely its path.
                result.update(
                    f"L:{relative}:{target.relative_to(base).as_posix()}\n".encode()
                )
                source = target
            elif entry.is_file():
                result.update(f"F:{relative}\n".encode())
                source = entry
            elif entry.is_dir():
                result.update(f"D:{relative}\n".encode())
                continue
            else:
                raise Blocked("UNSAFE_PATH", "Special file in runtime dependencies")
            with source.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    result.update(chunk)
            count += 1
    if not count:
        raise Blocked("RUNTIME_MISSING", "Empty project-local runtime dependency tree")
    return result.hexdigest()


def local_binary(root: Path) -> Path:
    """Accept npm's normal .bin symlink only if it targets the pinned local package."""
    binary = owned(root, "runtime/node_modules/.bin") / "hyperframes"
    if binary.is_symlink():
        package_dir = owned(root, "runtime/node_modules/hyperframes")
        if not binary.resolve().is_relative_to(package_dir.resolve()):
            raise Blocked(
                "UNSAFE_PATH", "HyperFrames bin symlink escapes pinned local package"
            )
    elif binary.exists():
        owned(root, "runtime/node_modules/.bin/hyperframes")
    return binary


def runtime(root: Path) -> Path:
    binpath = local_binary(root)
    if not binpath.is_file():
        raise Blocked(
            "RUNTIME_MISSING",
            "Run video runtime-install explicitly; never fetch via check/preview/render",
        )
    package = load(root, "runtime/package.json")
    installed = load(root, "runtime/node_modules/hyperframes/package.json")
    if (
        package.get("dependencies", {}).get("hyperframes") != PIN
        or installed.get("version") != PIN
    ):
        raise Blocked(
            "RUNTIME_VERSION", "Expected exact installed HyperFrames pin 0.8.142"
        )
    return binpath


def install(root: Path) -> None:
    if owned(root, "runtime/node_modules").exists():
        raise Blocked(
            "RUNTIME_EXISTS", "Runtime already installed; no implicit updates"
        )
    if not shutil.which("npm") or not shutil.which("node"):
        raise Blocked("NODE_MISSING", "Node >=22 and npm required")
    node = subprocess.run(
        ["node", "-p", 'process.versions.node.split(".")[0]'],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if node.returncode or int(node.stdout.strip()) < 22:
        raise Blocked("NODE_VERSION", "Node >=22 required")
    save(
        root,
        "runtime/package.json",
        {
            "name": "paper2html-video-local",
            "private": True,
            "version": "1.0.0",
            "dependencies": {"hyperframes": PIN},
        },
    )
    # Only explicitly invoked command fetches dependencies; scripts disabled.
    result = subprocess.run(
        ["npm", "install", "--ignore-scripts", "--no-audit", "--no-fund"],
        cwd=owned(root, "runtime"),
        timeout=300,
    )
    if result.returncode:
        raise Blocked("RUNTIME_INSTALL", "npm install failed; inspect runtime manually")
    runtime(root)


def build(root: Path) -> dict:
    manifest_source(root)
    script = check(root)
    approval(
        root,
        "script",
        hashes(root, ["script.json", "sources/manifest.json", "voice/request.json"]),
    )
    vm = load(root, "voice/manifest.json")
    if vm.get("input_hashes") != hashes(root, ["script.json"]):
        raise Blocked("VOICE_STALE", "Voice generated for a different script")
    clips = vm["clips"]
    if [c["id"] for c in clips] != [s["id"] for s in script["scenes"]]:
        raise Blocked("VOICE_STALE", "Voice clips do not match scenes")
    asset_paths = [
        "script.json",
        "sources/manifest.json",
        "sources/catalog.json",
        "voice/manifest.json",
    ]
    for clip in clips:
        with wave.open(str(owned(root, clip["path"])), "rb") as wav:
            duration = wav.getnframes() / wav.getframerate()
            if (
                wav.getnchannels() != 1
                or wav.getsampwidth() != 2
                or abs(duration - clip["duration_seconds"]) > 0.01
            ):
                raise Blocked("VOICE_STALE", "WAV changed or invalid")
        asset_paths.append(clip["path"])
    approval(root, "audio", hashes(root, asset_paths))
    approval(
        root,
        "voice",
        hashes(root, ["script.json", "sources/manifest.json", "voice/request.json"]),
    )
    asset_paths.extend(
        [
            "voice/request.json",
            "approvals/script.json",
            "approvals/voice.json",
            "approvals/audio.json",
        ]
    )
    binary = runtime(root)
    source = manifest_source(root)
    # Rebuild into existing directory only after approval; render approval becomes stale on any alteration.
    page_names = []
    for scene in script["scenes"]:
        for ref in scene.get("visual_refs", []):
            page_names.append(page_asset(root, ref["pdf_page"]))
    # At least a page per scene; no artificial or model-generated figures.
    for scene in script["scenes"]:
        if not scene.get("visual_refs"):
            page_names.append(page_asset(root, scene["claims"][0]["pdf_page"]))
    page_names = sorted(set(page_names))
    for name in page_names:
        put(
            root,
            "runtime/assets/" + name.removeprefix("assets/"),
            owned(root, name).read_bytes(),
        )
    for clip in clips:
        put(
            root,
            "runtime/assets/" + clip["path"],
            owned(root, clip["path"]).read_bytes(),
        )
    spans = []
    cursor = 0.0
    for scene, clip in zip(script["scenes"], clips):
        duration = clip["duration_seconds"]
        page = (scene.get("visual_refs") or scene["claims"])[0]["pdf_page"]
        spans.append(
            {
                "id": scene["id"],
                "start": round(cursor, 4),
                "duration": duration,
                "page": page,
                "narration": scene["narration"],
                "title": scene["title"],
                "voice": clip["path"],
            }
        )
        cursor += duration

    def esc(value: object) -> str:
        return html.escape(str(value), quote=True)

    elements = []
    for span in spans:
        start, duration = span["start"], span["duration"]
        sid = esc(span["id"])
        page = span["page"]
        elements.append(
            f'<div id="scene-{sid}" class="clip scene" data-hf-id="scene-{sid}" data-composition-id="scene-{sid}" data-no-timeline data-width="1920" data-height="1080" data-start="{start}" '
            f'data-duration="{duration}" data-track-index="0">'
            f'<figure class="paper-panel" data-hf-id="panel-{sid}"><img data-hf-id="page-{sid}" src="assets/pages/page-{page}.png" '
            f'alt="Original PDF page {page}"></figure>'
            f'<aside data-hf-id="aside-{sid}"><p class="eyebrow" data-hf-id="eyebrow-{sid}">Research paper · original source</p>'
            f'<h1 data-hf-id="title-{sid}">{esc(span["title"])}</h1>'
            f'<p class="source-label" data-hf-id="source-{sid}">Original PDF · physical page {page}</p></aside></div>'
        )
        # Audio clips are direct root children, never children of timed scenes.
        elements.append(
            f'<audio id="voice-{sid}" data-hf-id="voice-{sid}" src="assets/{esc(span["voice"])}" '
            f'data-start="{start}" data-duration="{duration}" '
            f'data-track-index="1" data-volume="1"></audio>'
        )
        # Captions use approximate sentence timing, not per-word alignment.
        sentences = [
            line
            for line in re.split(r"(?<=[.!?])\s+", span["narration"].strip())
            if line
        ]
        total_chars = sum(len(line) for line in sentences)
        elapsed = 0.0
        for index, sentence in enumerate(sentences):
            cap_start = start + elapsed
            cap_duration = (
                (duration - elapsed)
                if index == len(sentences) - 1
                else duration * len(sentence) / total_chars
            )
            elapsed += cap_duration
            elements.append(
                f'<div id="cap-{sid}-{index}" class="clip caption" data-hf-id="cap-{sid}-{index}" '
                f'data-track-kind="captions" data-start="{cap_start:.6f}" '
                f'data-duration="{cap_duration:.6f}" data-track-index="2">'
                f'<span data-hf-id="cap-text-{sid}-{index}">{esc(sentence)}</span></div>'
            )
    markup = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=1920,height=1080"><style>
    *{{box-sizing:border-box}}html,body{{margin:0;width:1920px;height:1080px;overflow:hidden;background:#101b2a;color:#f8fafc;font-family:sans-serif}}
    #root{{position:relative;width:100%;height:100%;overflow:hidden;background:#101b2a}}
    .clip{{position:absolute;left:0;top:0;width:100%;height:100%}}
    .scene{{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);align-items:start;gap:66px;padding:68px 90px 240px}}
    .paper-panel{{height:755px;min-width:0;display:flex;align-items:center;justify-content:center;overflow:hidden;background:#fafafa;border-radius:16px}}
    .paper-panel img{{max-width:100%;max-height:100%;width:auto;height:auto;object-fit:contain;transform-origin:50% 50%}}
    aside{{min-width:0;max-height:755px;padding:80px 20px 0 0}}
    .eyebrow{{font-size:24px;color:#bad2ee}}h1{{font-size:58px;line-height:1.12;overflow-wrap:anywhere;margin:30px 0 45px}}
    .source-label{{font-size:26px;color:#d9e6f5}}
    .caption{{height:170px;top:900px;padding:12px 125px;display:flex;align-items:center;justify-content:center;text-align:center;background:#172940;z-index:2}}
    .caption span{{max-width:1630px;font-size:31px;line-height:1.2;overflow-wrap:anywhere}}
    </style></head><body><main id="root" data-hf-id="main" data-composition-id="main" data-no-timeline data-start="0" data-duration="{cursor}" data-width="1920" data-height="1080">{"".join(elements)}</main></body></html>"""
    # HyperFrames' native WAAPI adapter seeks all document animations by frame.
    # Finite paused effects are deterministic; no GSAP/CDN/vendor code or timers.
    markup = markup.replace(
        "</body>",
        """<script>
        document.querySelectorAll('.scene').forEach((node) => {
          // Move the whole source panel slightly; never crop or scale source-page contents.
          const effect = node.querySelector('.paper-panel').animate(
            [{ transform: 'translateY(12px)' }, { transform: 'translateY(0)' }],
            { duration: Number(node.dataset.duration) * 1000,
              delay: Number(node.dataset.start) * 1000,
              fill: 'both', iterations: 1, easing: 'linear' }
          );
          effect.pause();
        });
        </script></body>""",
    )
    save(
        root,
        "runtime/hyperframes.json",
        {
            "$schema": "https://hyperframes.heygen.com/schema/hyperframes.json",
            "paths": {
                "blocks": "compositions",
                "components": "compositions/components",
                "assets": "assets",
            },
        },
    )
    put(root, "runtime/index.html", markup.encode())
    inputs = hashes(
        root,
        asset_paths
        + page_names
        + ["runtime/assets/" + n.removeprefix("assets/") for n in page_names]
        + ["runtime/assets/" + c["path"] for c in clips]
        + [
            "runtime/index.html",
            "runtime/hyperframes.json",
            "runtime/package.json",
            "runtime/package-lock.json",
            "runtime/node_modules/hyperframes/package.json",
        ],
    )
    info = {
        "version": 1,
        "duration_seconds": cursor,
        "spans": spans,
        "caption_timing": "approximate sentence-level, not word-aligned",
        "inputs": inputs,
        "source_identity": source["identity"],
        "runtime_binary_sha256": digest(binary.read_bytes()),
        "runtime_tree_sha256": runtime_tree_hash(root),
    }
    save(root, "build.json", info)
    return info


def checked(root: Path) -> dict:
    manifest_source(root)  # Originals must still match before preview/render/verify.
    b = load(root, "build.json")
    unchanged(root, b["inputs"])
    if digest(runtime(root).read_bytes()) != b.get(
        "runtime_binary_sha256"
    ) or runtime_tree_hash(root) != b.get("runtime_tree_sha256"):
        raise Blocked(
            "RUNTIME_STALE",
            "Installed local runtime dependency code changed after build",
        )
    return b


def invoke(
    root: Path,
    command: str,
    *,
    output: str | None = None,
    flags: tuple[str, ...] = (),
) -> subprocess.CompletedProcess:
    checked(root)
    cmd = [str(runtime(root)), command, *flags]
    if output:
        cmd += ["--output", output]
    return subprocess.run(
        cmd, cwd=owned(root, "runtime"), text=True, capture_output=True, timeout=3600
    )
