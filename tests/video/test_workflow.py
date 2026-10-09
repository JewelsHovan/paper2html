"""Offline video staging tests; no paid calls, npm installs or final encode."""

import io
import json
from pathlib import Path
import wave

import httpx
import pymupdf
import pytest
from typer.testing import CliRunner

from paper2html.cli import app
from paper2html.video import api, source
from paper2html.video.core import (
    Blocked,
    hashes,
    load,
    locked,
    owned,
    put,
    save,
)

runner = CliRunner()


def model_available(req, input_limit=100000, output_limit=10000):
    name = req.url.path.rsplit("/models/", 1)[-1]
    return httpx.Response(
        200,
        json={
            "name": "models/" + name,
            "inputTokenLimit": input_limit,
            "outputTokenLimit": output_limit,
            "supportedGenerationMethods": ["generateContent"],
        },
    )


def pdf(path: Path, title="Another Research Topic"):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((70, 70), f"{title}: validated fact")
    doc.save(str(path))
    doc.close()


def setup(tmp_path, title="Another Research Topic"):
    file = tmp_path / "paper.pdf"
    pdf(file, title)
    root = tmp_path / "project"
    assert (
        runner.invoke(
            app, ["video", "init", str(root), "--source", str(file)]
        ).exit_code
        == 0
    )
    script = {
        "source_revision": None,
        "scenes": [
            {
                "id": "opening",
                "title": title,
                "narration": "Evidence, <script>alert(1)</script> and further context.",
                "claims": [
                    {
                        "claim": "fact",
                        "pdf_page": 1,
                        "quote": f"{title}: validated fact",
                        "source": "pdf",
                    }
                ],
                "visual_refs": [{"pdf_page": 1}],
            }
        ],
    }
    original = tmp_path / "script.json"
    original.write_text(json.dumps(script))
    assert (
        runner.invoke(
            app, ["video", "script", "import", str(root), str(original)]
        ).exit_code
        == 0
    )
    return root, script


def fake_runtime(root):
    binary = owned(root, "runtime/node_modules/.bin/hyperframes")
    binary.parent.mkdir(parents=True)
    binary.write_text(
        '#!/bin/sh\nif [ "$1" = check ]; then\ncat <<\'JSON\'\n{"ok":true,"strict":true,"browserSkipped":false,"lint":{"ok":true,"filesScanned":1},"runtime":{"ok":true},"layout":{"ok":true,"duration":1,"samples":[0.5]},"motion":{"ok":true},"contrast":{"ok":true,"checked":1}}\nJSON\nfi\n'
    )
    binary.chmod(0o755)
    save(root, "runtime/package.json", {"dependencies": {"hyperframes": "0.8.142"}})
    save(root, "runtime/node_modules/hyperframes/package.json", {"version": "0.8.142"})
    save(root, "runtime/package-lock.json", {"lockfileVersion": 3})


def request_voice(root, profile=None, model="explicit-tts"):
    if profile is None:
        profile = root.parent / "rates.json"
        profile.write_text(
            json.dumps(
                {
                    "models": {
                        model: {"input_usd_per_million": 1, "output_usd_per_million": 1}
                    }
                }
            )
        )
    result = runner.invoke(
        app,
        [
            "video",
            "voice-request",
            str(root),
            "--model",
            model,
            "--voice",
            "Kore",
            "--rate-profile",
            str(profile),
            "--request-budget",
            "1",
            "--run-budget",
            "2",
            "--max-output-tokens",
            "100",
        ],
    )
    assert result.exit_code == 0, result.output
    approve(root, "script")
    approve(root, "voice")
    return profile


def fake_voice(root):
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x01\x02" * 16000)
    put(root, "voice/opening.wav", out.getvalue())
    save(
        root,
        "voice/manifest.json",
        {
            "clips": [
                {"id": "opening", "path": "voice/opening.wav", "duration_seconds": 1.0}
            ],
            "input_hashes": hashes(root, ["script.json"]),
        },
    )


def approve(root, stage):
    result = runner.invoke(app, ["video", "approve", str(root), stage])
    assert result.exit_code == 0, result.output


def test_generic_build_approval_and_tamper(tmp_path):
    root, _ = setup(tmp_path, "Totally Different Paper")
    assert load(root, "sources/manifest.json")["identity"]["coverage"].startswith(
        "PDF only"
    )
    request_voice(root)
    fake_voice(root)
    approve(root, "audio")
    fake_runtime(root)
    assert runner.invoke(app, ["video", "build", str(root)]).exit_code == 0
    markup = owned(root, "runtime/index.html").read_text()
    assert "Totally Different Paper" in markup and "AutoSciBench" not in markup
    assert "<script>alert(1)</script>" not in markup and "&lt;script&gt;" in markup
    assert load(root, "build.json")["duration_seconds"] == 1
    assert runner.invoke(app, ["video", "preview", str(root)]).exit_code == 0
    approve(root, "preview")
    assert runner.invoke(app, ["video", "status", str(root)]).exit_code == 0
    # Editing a source asset invalidates build and downstream approval, even without CLI edits.
    put(root, "runtime/assets/voice/opening.wav", b"tampered")
    status = runner.invoke(app, ["video", "status", str(root)]).output
    assert "STALE_INPUT" in status and "blocked" in status
    assert runner.invoke(app, ["video", "render", str(root)]).exit_code == 2


def test_approval_record_edit_and_script_edit_block(tmp_path):
    root, _ = setup(tmp_path)
    request_voice(root)
    record = load(root, "approvals/script.json")
    record["seal"] = "fake"
    save(root, "approvals/script.json", record)
    assert "APPROVAL_STALE" in runner.invoke(app, ["video", "status", str(root)]).output
    approve(root, "script")
    obj = load(root, "script.json")
    obj["scenes"][0]["narration"] = "changed"
    save(root, "script.json", obj)
    assert "APPROVAL_STALE" in runner.invoke(app, ["video", "status", str(root)]).output
    assert (
        runner.invoke(
            app,
            [
                "video",
                "voice",
                str(root),
                "--model",
                "any",
                "--voice",
                "Kore",
                "--rate-profile",
                "missing",
                "--request-budget",
                "1",
                "--run-budget",
                "1",
                "--max-output-tokens",
                "100",
                "--consent",
            ],
        ).exit_code
        == 2
    )


def test_crash_lock_symlink_no_overwrite_and_source_identity(tmp_path):
    root, _ = setup(tmp_path)
    owned(root, ".video.lock").mkdir()
    result = runner.invoke(app, ["video", "approve", str(root), "script"])
    assert "PROJECT_LOCKED" in result.output
    owned(root, ".video.lock").rmdir()  # explicit operator action in fixture
    with pytest.raises(Blocked, match="Symlink"):
        owned(root, "voice").symlink_to(tmp_path, target_is_directory=True)
        owned(root, "voice/out.wav")
    script = load(root, "script.json")
    script["source_revision"] = "2610.05140v1"
    with pytest.raises(Blocked, match="revision"):
        from paper2html.video.script import check

        check(root, script)
    assert runner.invoke(app, ["process", "--help"]).exit_code == 0


def test_arxiv_exact_pair_revision_and_preserved_bytes(tmp_path):
    file = tmp_path / "paper.pdf"
    pdf(file)
    html = b'<html><body><section id="S1">Example</section><figure id="F1">Figure</figure></body></html>'

    def handler(req):
        if req.url.path.endswith("/pdf/2501.12345v2"):
            return httpx.Response(200, content=file.read_bytes())
        if req.url.path.endswith("/html/2501.12345v2"):
            return httpx.Response(200, content=html)
        return httpx.Response(404)

    root = tmp_path / "arxiv"
    root.mkdir()
    manifest = source.prepare(
        root, "2501.12345v2", transport=httpx.MockTransport(handler)
    )
    assert manifest["identity"]["revision"] == "2501.12345v2"
    assert owned(root, "sources/original.html").read_bytes() == html
    assert owned(root, "sources/original.pdf").read_bytes() == file.read_bytes()
    other = tmp_path / "mismatch"
    other.mkdir()

    def redirect(req):
        if req.url.path.endswith("v2"):
            return httpx.Response(
                302, headers={"location": "https://arxiv.org/html/2501.12345v3"}
            )
        return httpx.Response(200, content=html)

    with pytest.raises(Blocked, match="explicit"):
        source.prepare(other, "2501.12345v2", transport=httpx.MockTransport(redirect))
    assert not owned(other, "sources/manifest.json").exists()


def test_budget_receipt_unknown_billing_no_second_post_no_secret(tmp_path, monkeypatch):
    root, _ = setup(tmp_path)
    profile = tmp_path / "rate.json"
    profile.write_text(
        json.dumps(
            {
                "models": {
                    "test-model": {
                        "input_usd_per_million": 2,
                        "output_usd_per_million": 4,
                    }
                }
            }
        )
    )
    price = api.rates(
        profile, "test-model", output_limit=100, budget=0.001, run_budget=0.002
    )
    seen = []

    def handler(req):
        seen.append(str(req.url))
        if req.method == "GET":
            return model_available(req)
        if str(req.url).endswith(":countTokens"):
            return httpx.Response(200, json={"totalTokens": 100})
        raise httpx.ReadTimeout("timeout")

    monkeypatch.setenv("GEMINI_API_KEY", "supersecret")
    transport = httpx.MockTransport(handler)
    body = {"contents": [{"parts": [{"text": "example"}]}]}
    with pytest.raises(Blocked, match="no automatic retry") as exc:
        with locked(root):
            api.call(root, "script", "test-model", body, price, transport=transport)
    assert "supersecret" not in str(exc.value)
    assert (
        load(root, "receipts/script/attempt.json")["status"]
        == "reserved_unknown_billing"
    )
    with pytest.raises(Blocked, match="never automatically retry"):
        with locked(root):
            api.call(root, "script", "test-model", body, price, transport=transport)
    assert sum(":generateContent" in url for url in seen) == 1
    with pytest.raises(Blocked, match="budget"):
        api.call(
            root,
            "voice-opening",
            "test-model",
            body,
            {**price, "budget": 0.00001},
            transport=transport,
        )
    assert not owned(root, "receipts/voice-opening").exists()


def test_verify_uses_ffprobe_decode_volume_and_dynamic_samples(tmp_path, monkeypatch):
    from PIL import Image, ImageDraw
    from types import SimpleNamespace
    from paper2html.video.core import digest
    import paper2html.video.cli as video_cli

    root, _ = setup(tmp_path)
    request_voice(root)
    fake_voice(root)
    approve(root, "audio")
    fake_runtime(root)
    assert runner.invoke(app, ["video", "build", str(root)]).exit_code == 0
    assert runner.invoke(app, ["video", "preview", str(root)]).exit_code == 0
    approve(root, "preview")
    put(root, "runtime/renders/final.mp4", b"placeholder, no actual video encoded")
    b = load(root, "build.json")
    save(
        root,
        "render.json",
        {
            "version": 1,
            "status": "completed",
            "inputs": hashes(root, ["build.json"] + list(b["inputs"])),
            "mp4_sha256": digest(owned(root, "runtime/renders/final.mp4").read_bytes()),
        },
    )
    seen = []

    def png(x):
        image = Image.new("RGB", (1920, 1080), "white")
        ImageDraw.Draw(image).rectangle((x, 100, x + 220, 350), fill="navy")
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()

    def fake_run(args, **kwargs):
        seen.append(args)
        if args[0] == "ffprobe":
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "streams": [
                            {
                                "codec_type": "video",
                                "codec_name": "h264",
                                "width": 1920,
                                "height": 1080,
                            },
                            {"codec_type": "audio", "codec_name": "aac"},
                        ],
                        "format": {"duration": 1},
                    }
                ),
            )
        if "volumedetect" in args:
            return SimpleNamespace(returncode=0, stderr=b"mean_volume: -21.0 dB")
        if "image2pipe" in args:
            return SimpleNamespace(
                returncode=0,
                stdout=png(200 if float(args[args.index("-ss") + 1]) < 0.5 else 700),
            )
        if "--version" in args:
            return SimpleNamespace(returncode=0, stdout="0.8.142", stderr="")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    real_which = video_cli.shutil.which
    # FFmpeg itself is stubbed above, so this test must not depend on the
    # host having it installed (CI runners do not).
    monkeypatch.setattr(
        video_cli.shutil,
        "which",
        lambda name: (
            f"/stub/{name}" if name in ("ffmpeg", "ffprobe") else real_which(name)
        ),
    )
    monkeypatch.setattr(video_cli.subprocess, "run", fake_run)
    response = runner.invoke(app, ["video", "verify", str(root)])
    assert response.exit_code == 0, response.output
    report = load(root, "verification.json")
    assert report["status"] == "passed"
    assert report["source_visuals"]["status"] == "unverified"
    assert report["source_presence_verified"] is False
    assert "UNVERIFIED" in report["needs_review_warning"]
    assert all(
        sample["source_page_presence"] == "unverified"
        for sample in report["proof_schedule"]
    )
    assert any("volumedetect" in args for args in seen)
    assert len([args for args in seen if "image2pipe" in args]) == 2


def test_gemini_voice_pcm_duration_and_no_second_paid_call(tmp_path, monkeypatch):
    import base64

    root, _ = setup(tmp_path)
    profile = tmp_path / "rates.json"
    profile.write_text(
        json.dumps(
            {
                "models": {
                    "explicit-tts": {
                        "input_usd_per_million": 1,
                        "output_usd_per_million": 1,
                    }
                }
            }
        )
    )
    monkeypatch.setenv("GEMINI_API_KEY", "never-in-receipt")
    with pytest.raises(Blocked, match="voice/request.json"):
        api.voice(root, "explicit-tts", "Kore", profile, 1, 2, 100)
    assert not owned(root, "receipts").exists()
    request_voice(root, profile)
    seen = []

    def handler(req):
        seen.append(str(req.url))
        if req.method == "GET":
            return model_available(req)
        if ":countTokens" in str(req.url):
            return httpx.Response(200, json={"totalTokens": 20})
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "inlineData": {
                                        "mimeType": "audio/L16;rate=24000",
                                        "data": base64.b64encode(
                                            b"\x11\x00" * 24000
                                        ).decode(),
                                    }
                                }
                            ]
                        }
                    }
                ]
            },
        )

    api.voice(
        root,
        "explicit-tts",
        "Kore",
        profile,
        1,
        2,
        100,
        transport=httpx.MockTransport(handler),
    )
    voice = load(root, "voice/manifest.json")
    assert voice["clips"][0]["duration_seconds"] == 1.0
    assert owned(root, "voice/opening.wav").read_bytes().startswith(b"RIFF")
    assert (
        "never-in-receipt"
        not in owned(root, "receipts/voice-opening/attempt.json").read_text()
    )
    with pytest.raises(Blocked, match="no overwrite"):
        api.voice(
            root,
            "explicit-tts",
            "Kore",
            profile,
            1,
            2,
            100,
            transport=httpx.MockTransport(handler),
        )
    assert sum(":generateContent" in url for url in seen) == 1


def test_script_post_contains_projection_and_complete_pdf(tmp_path, monkeypatch):
    import base64

    file = tmp_path / "article.pdf"
    pdf(file, "Cross Domain Discovery")
    html = b'<html><body><script>alert(7)</script><section id="S1">Cross Domain Discovery explains evidence.</section><figure id="fig2">Native Figure 2</figure></body></html>'

    def fetch(req):
        return httpx.Response(
            200, content=html if "/html/" in req.url.path else file.read_bytes()
        )

    root = tmp_path / "arxiv"
    root.mkdir()
    source.prepare(root, "2501.12345v2", transport=httpx.MockTransport(fetch))
    profile = tmp_path / "rates.json"
    profile.write_text(
        json.dumps(
            {
                "models": {
                    "explicit-script": {
                        "input_usd_per_million": 1,
                        "output_usd_per_million": 1,
                    }
                }
            }
        )
    )
    monkeypatch.setenv("GEMINI_API_KEY", "not-logged")
    script = {
        "source_revision": "2501.12345v2",
        "scenes": [
            {
                "id": "one",
                "title": "Cross Domain Discovery",
                "narration": "Cross Domain Discovery explains evidence.",
                "claims": [
                    {
                        "claim": "Evidence",
                        "source": "html",
                        "html_id": "S1",
                        "pdf_page": 1,
                        "quote": "Cross Domain Discovery explains evidence.",
                    }
                ],
                "visual_refs": [{"pdf_page": 1, "html_id": "fig2"}],
            }
        ],
    }
    seen = []

    def handler(req):
        if req.method == "GET":
            return model_available(req)
        if ":countTokens" in str(req.url):
            return httpx.Response(200, json={"totalTokens": 100})
        seen.append(req)
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": json.dumps(script)}]}}]
            },
        )

    api.draft(
        root,
        "explicit-script",
        profile,
        1,
        1,
        100,
        transport=httpx.MockTransport(handler),
    )
    assert len(seen) == 1
    payload = json.loads(seen[0].content)
    parts = payload["contents"][0]["parts"]
    assert base64.b64decode(parts[-1]["inlineData"]["data"]) == file.read_bytes()
    assert "Cross Domain Discovery" in parts[1]["text"]
    assert "alert(7)" not in parts[1]["text"]
    assert load(root, "script.json")["scenes"][0]["visual_refs"][0]["html_id"] == "fig2"


def test_paid_voice_request_tampering_blocks_before_count_or_post(
    tmp_path, monkeypatch
):
    root, _ = setup(tmp_path)
    profile = request_voice(root)
    req = load(root, "voice/request.json")
    req["voice"] = "Other"
    save(root, "voice/request.json", req)
    monkeypatch.setenv("GEMINI_API_KEY", "not-logged")
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"totalTokens": 5})

    with pytest.raises(Blocked, match="approval absent or stale"):
        api.voice(
            root,
            "explicit-tts",
            "Other",
            profile,
            1,
            2,
            100,
            transport=httpx.MockTransport(handler),
        )
    assert seen == []
    assert "APPROVAL_STALE" in runner.invoke(app, ["video", "status", str(root)]).output


@pytest.mark.parametrize(
    "defect", ["browserSkipped", "no_samples", "no_contrast", "not_strict"]
)
def test_preview_rejects_skipped_or_zero_browser_audit(tmp_path, monkeypatch, defect):
    from types import SimpleNamespace
    import paper2html.video.build as video_build

    root, _ = setup(tmp_path)
    request_voice(root)
    fake_voice(root)
    approve(root, "audio")
    fake_runtime(root)
    assert runner.invoke(app, ["video", "build", str(root)]).exit_code == 0
    report = {
        "ok": True,
        "strict": True,
        "browserSkipped": False,
        "lint": {"ok": True, "filesScanned": 1},
        "runtime": {"ok": True},
        "layout": {"ok": True, "duration": 1, "samples": [0.5]},
        "motion": {"ok": True},
        "contrast": {"ok": True, "checked": 1},
    }
    if defect == "browserSkipped":
        report["browserSkipped"] = True
    if defect == "no_samples":
        report["layout"]["samples"] = []
    if defect == "no_contrast":
        report["contrast"]["checked"] = 0
    if defect == "not_strict":
        report["strict"] = False
    monkeypatch.setattr(
        video_build,
        "invoke",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json.dumps(report)),
    )
    result = runner.invoke(app, ["video", "preview", str(root)])
    assert result.exit_code == 2 and "BUILD_CHECK" in result.output
    assert not owned(root, "preview.json").exists()


def test_hyperframes_root_and_separate_timed_clips(tmp_path):
    from bs4 import BeautifulSoup

    root, _ = setup(tmp_path)
    request_voice(root)
    fake_voice(root)
    approve(root, "audio")
    fake_runtime(root)
    assert runner.invoke(app, ["video", "build", str(root)]).exit_code == 0
    doc = BeautifulSoup(owned(root, "runtime/index.html").read_text(), "html.parser")
    main = doc.select_one('[data-composition-id="main"]')
    assert (
        main.name == "main"
        and main["data-width"] == "1920"
        and main["data-height"] == "1080"
    )
    assert "<template" not in str(doc)
    clips = main.find_all(attrs={"data-hf-id": True}, recursive=False)
    assert len({c["data-hf-id"] for c in clips}) == len(clips)
    audio = main.find("audio")
    assert audio.parent == main and "crossorigin" not in audio.attrs
    assert (
        len(main.select(".clip.scene")) == 1 and len(main.select(".clip.caption")) >= 1
    )
    css = doc.style.get_text()
    assert "width:100%;height:100%" in css and "top:900px" in css
    assert "@font-face" not in css and "url(" not in css
    assert "cdn" not in str(doc).lower() and "gsap" not in str(doc).lower()


def test_npm_local_bin_symlink_allowed_only_inside_pinned_package(tmp_path):
    import paper2html.video.build as video_build

    root, _ = setup(tmp_path)
    save(root, "runtime/package.json", {"dependencies": {"hyperframes": "0.8.142"}})
    save(root, "runtime/node_modules/hyperframes/package.json", {"version": "0.8.142"})
    script = owned(root, "runtime/node_modules/hyperframes/dist/cli.js")
    script.parent.mkdir(parents=True)
    script.write_text("#!/usr/bin/env node\n")
    binary = owned(root, "runtime/node_modules/.bin") / "hyperframes"
    binary.parent.mkdir(parents=True)
    binary.symlink_to("../hyperframes/dist/cli.js")
    assert video_build.runtime(root) == binary
    assert (
        '"runtime_installed": true'
        in runner.invoke(app, ["video", "doctor", str(root)]).output
    )
    binary.unlink()
    binary.symlink_to(tmp_path / "paper.pdf")
    with pytest.raises(Blocked, match="escapes"):
        video_build.runtime(root)


def test_sentence_captions_are_timed_clips_not_claimed_word_alignment(tmp_path):
    from bs4 import BeautifulSoup

    root, script = setup(tmp_path)
    script["scenes"][0]["narration"] = "First grounded idea. Second grounded idea."
    save(root, "script.json", script)
    request_voice(root)
    fake_voice(root)
    approve(root, "audio")
    fake_runtime(root)
    assert runner.invoke(app, ["video", "build", str(root)]).exit_code == 0
    doc = BeautifulSoup(owned(root, "runtime/index.html").read_text(), "html.parser")
    captions = doc.select(".clip.caption")
    assert [c.get_text() for c in captions] == [
        "First grounded idea.",
        "Second grounded idea.",
    ]
    assert all(c.parent["data-composition-id"] == "main" for c in captions)
    assert abs(sum(float(c["data-duration"]) for c in captions) - 1.0) < 0.00001
    assert (
        load(root, "build.json")["caption_timing"]
        == "approximate sentence-level, not word-aligned"
    )


def test_script_approval_binds_voice_model_profile_before_synthesis(
    tmp_path, monkeypatch
):
    root, _ = setup(tmp_path)
    no_request = runner.invoke(app, ["video", "approve", str(root), "script", "--json"])
    assert no_request.exit_code == 2 and "voice/request.json" in no_request.output
    profile = request_voice(root, model="explicit-tts")
    script_approval = load(root, "approvals/script.json")
    assert "voice/request.json" in script_approval["hashes"]
    monkeypatch.setenv("GEMINI_API_KEY", "redacted-value")
    request = load(root, "voice/request.json")
    request["model"] = "different-model"
    save(root, "voice/request.json", request)
    with pytest.raises(Blocked, match="approval absent or stale"):
        api.voice(root, "different-model", "Kore", profile, 1, 2, 100)
    assert not owned(root, "receipts").exists()
    assert (
        "APPROVAL_STALE"
        in runner.invoke(app, ["video", "status", str(root), "--json"]).output
    )


def test_json_suffix_local_pdf_init_and_offline_doctor_capabilities(
    tmp_path, monkeypatch
):
    file = tmp_path / "other.pdf"
    pdf(file, "Non Benchmark Paper")
    root = tmp_path / "new-project"
    initiated = runner.invoke(
        app, ["video", "init", str(root), "--source", str(file), "--json"]
    )
    assert initiated.exit_code == 0, initiated.output
    assert json.loads(initiated.output)["identity"]["coverage"].startswith("PDF only")
    result = runner.invoke(app, ["video", "status", str(root), "--json"])
    assert json.loads(result.output)["error_code"] == "MISSING_OR_INVALID"
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    import paper2html.video.cli as video_cli

    original = video_cli.shutil.which
    monkeypatch.setattr(
        video_cli.shutil,
        "which",
        lambda name: None if name in ("node", "npm") else original(name),
    )
    output = runner.invoke(app, ["video", "doctor", str(root), "--json"])
    assert output.exit_code == 0
    report = json.loads(output.output)
    assert report["status"] == "blocked" and "NODE_22_REQUIRED" in report["blockers"]
    assert report["GEMINI_API_KEY_present"] is False
    assert (
        report["pricing"] is None
        and report["pricing_error"] == "UNKNOWN_PRICE_UNTIL_REVIEWED_PROFILE"
    )
    assert (
        report["runtime_version"] is None
        and report["browser_check"] == "not_run_offline"
    )


def test_doctor_explicit_usd_rates_and_currency_rejection(tmp_path, monkeypatch):
    profile = tmp_path / "reviewed.json"
    profile.write_text(
        json.dumps(
            {
                "currency": "USD",
                "models": {
                    "chosen-model": {
                        "input_usd_per_million": 0.5,
                        "output_usd_per_million": 2,
                    }
                },
            }
        )
    )
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    command = [
        "video",
        "doctor",
        str(tmp_path),
        "--model",
        "chosen-model",
        "--rate-profile",
        str(profile),
        "--json",
    ]
    result = runner.invoke(app, command)
    report = json.loads(result.output)
    assert (
        report["pricing"]["currency"] == "USD"
        and report["pricing"]["output_usd_per_million"] == 2
    )
    assert not report["capabilities"]["paid_generation"]
    profile.write_text(
        json.dumps(
            {
                "currency": "EUR",
                "models": {
                    "chosen-model": {
                        "input_usd_per_million": 0.5,
                        "output_usd_per_million": 2,
                    }
                },
            }
        )
    )
    rejected = json.loads(runner.invoke(app, command).output)
    assert rejected["pricing"] is None and rejected["pricing_error"] == "UNKNOWN_PRICE"


def test_preview_no_assumed_url_and_corrupt_review_json_safe_failure(tmp_path):
    root, _ = setup(tmp_path)
    request_voice(root)
    fake_voice(root)
    approve(root, "audio")
    fake_runtime(root)
    assert runner.invoke(app, ["video", "build", str(root)]).exit_code == 0
    result = runner.invoke(app, ["video", "preview", str(root), "--json"])
    assert result.exit_code == 0, result.output
    preview = load(root, "preview.json")
    assert preview["url"] is None and preview["audit"]["browserSkipped"] is False
    save(root, "preview.json", {"version": 1})
    failed = runner.invoke(app, ["video", "approve", str(root), "preview", "--json"])
    assert failed.exit_code == 2
    assert json.loads(failed.output)["error_code"] == "INVALID_PROJECT_DATA"
    assert "Traceback" not in failed.output


@pytest.mark.parametrize(
    "scenario",
    ["input_context", "output_limit", "unsupported_method", "wrong_identity"],
)
def test_model_limits_and_method_block_before_generation(
    tmp_path, monkeypatch, scenario
):
    root, _ = setup(tmp_path)
    profile = tmp_path / "rate.json"
    profile.write_text(
        json.dumps(
            {
                "models": {
                    "chosen": {"input_usd_per_million": 1, "output_usd_per_million": 1}
                }
            }
        )
    )
    price = api.rates(profile, "chosen", output_limit=100, budget=1, run_budget=1)
    monkeypatch.setenv("GEMINI_API_KEY", "hidden")
    seen = []

    def handler(req):
        seen.append((req.method, str(req.url)))
        if req.method == "GET":
            details = model_available(
                req,
                input_limit=50 if scenario == "input_context" else 1000,
                output_limit=99 if scenario == "output_limit" else 1000,
            ).json()
            if scenario == "unsupported_method":
                details["supportedGenerationMethods"] = []
            if scenario == "wrong_identity":
                details["name"] = "models/other"
            return httpx.Response(200, json=details)
        if ":countTokens" in str(req.url):
            return httpx.Response(200, json={"totalTokens": 51})
        raise AssertionError("Generation MUST NOT be sent")

    with pytest.raises(Blocked) as error:
        api.call(
            root,
            "script",
            "chosen",
            {"contents": [{"parts": [{"text": "entire source"}]}]},
            price,
            transport=httpx.MockTransport(handler),
        )
    assert error.value.code == (
        "MODEL_CONTEXT" if scenario == "input_context" else "MODEL_PREFLIGHT"
    )
    assert not any(":generateContent" in url for _, url in seen)
    assert not owned(root, "receipts/script").exists()
    if scenario != "input_context":
        assert not any(":countTokens" in url for _, url in seen)


def test_instructions_is_substantive_and_harness_neutral():
    output = runner.invoke(app, ["video", "instructions"])
    assert output.exit_code == 0
    text = output.output
    for phrase in (
        "no agent-harness dependency",
        "voice-request PROJECT",
        "approve PROJECT script",
        "NEVER blindly retry",
        "Never self-approve",
        "originals",
        "runtime-install PROJECT --consent",
        "NO upload command",
        "status=blocked",
        "SENTENCE timing",
    ):
        assert phrase in text
    assert len(text.splitlines()) > 55


def test_full_pdf_script_generation_blocks_on_authenticated_context_limit(
    tmp_path, monkeypatch
):
    file = tmp_path / "full.pdf"
    pdf(file, "Context Boundary Paper")
    html = b'<html><body><section id="section1">Context Boundary Paper methods.</section></body></html>'
    root = tmp_path / "source"
    root.mkdir()

    def fetch(req):
        return httpx.Response(
            200, content=html if "/html/" in req.url.path else file.read_bytes()
        )

    source.prepare(root, "2501.12345v2", transport=httpx.MockTransport(fetch))
    profile = tmp_path / "rates.json"
    profile.write_text(
        json.dumps(
            {
                "currency": "USD",
                "models": {
                    "explicit-draft": {
                        "input_usd_per_million": 1,
                        "output_usd_per_million": 1,
                    }
                },
            }
        )
    )
    monkeypatch.setenv("GEMINI_API_KEY", "secret-not-in-output")
    seen = []

    def handler(req):
        seen.append((req.method, str(req.url)))
        if req.method == "GET":
            return model_available(req, input_limit=49)
        if ":countTokens" in str(req.url):
            supplied = json.loads(req.content)["generateContentRequest"]["contents"][0][
                "parts"
            ]
            assert supplied[-1]["inlineData"]["mimeType"] == "application/pdf"
            return httpx.Response(200, json={"totalTokens": 50})
        raise AssertionError("paid generation forbidden for over-context full source")

    with pytest.raises(Blocked) as error:
        api.draft(
            root,
            "explicit-draft",
            profile,
            1,
            1,
            100,
            transport=httpx.MockTransport(handler),
        )
    assert error.value.code == "MODEL_CONTEXT"
    assert not any(":generateContent" in url for _, url in seen)
    assert not owned(root, "receipts/script").exists()


def test_original_pdf_edit_blocks_status_preview_render_verify(tmp_path):
    root, _ = setup(tmp_path)
    request_voice(root)
    fake_voice(root)
    approve(root, "audio")
    fake_runtime(root)
    assert runner.invoke(app, ["video", "build", str(root)]).exit_code == 0
    assert runner.invoke(app, ["video", "preview", str(root)]).exit_code == 0
    approve(root, "preview")
    original = owned(root, "sources/original.pdf")
    put(root, "sources/original.pdf", original.read_bytes() + b"changed-original")
    for command in ("status", "preview", "render", "verify"):
        response = runner.invoke(app, ["video", command, str(root), "--json"])
        assert response.exit_code == 2, (command, response.output)
        assert json.loads(response.output)["error_code"] == "STALE_INPUT"
    assert not owned(root, "render.json").exists()


def test_nonentrypoint_runtime_dependency_edit_blocks_downstream(tmp_path):
    from paper2html.video import build as video_build

    root, _ = setup(tmp_path)
    request_voice(root)
    fake_voice(root)
    approve(root, "audio")
    fake_runtime(root)
    helper = "runtime/node_modules/hyperframes/dist/renderer-helper.js"
    put(root, helper, b"export const proof = 1;\n")
    assert runner.invoke(app, ["video", "build", str(root)]).exit_code == 0
    assert video_build.checked(root)["runtime_tree_sha256"]
    put(root, helper, b"export const proof = 2;\n")
    with pytest.raises(Blocked) as exc:
        video_build.checked(root)
    assert exc.value.code == "RUNTIME_STALE"
    for command in ("preview", "render", "verify"):
        response = runner.invoke(app, ["video", command, str(root), "--json"])
        assert response.exit_code == 2
        assert json.loads(response.output)["error_code"] == "RUNTIME_STALE"


def test_arxiv_redirect_off_origin_never_dispatched_and_stream_bounded(
    tmp_path, monkeypatch
):
    import gzip

    seen = []

    def off_origin(req):
        seen.append(str(req.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    with pytest.raises(Blocked) as exc:
        source.prepare(
            tmp_path / "offorigin",
            "2501.12345v2",
            transport=httpx.MockTransport(off_origin),
        )
    assert exc.value.code == "SOURCE_REVISION"
    assert len(seen) == 1 and seen[0].startswith("https://arxiv.org/html/")
    assert not (tmp_path / "offorigin/sources/manifest.json").exists()

    monkeypatch.setattr(source, "SOURCE_LIMIT", 64)
    seen.clear()

    class ChunkStream(httpx.SyncByteStream):
        def __iter__(self):
            seen.append("first")
            yield b"<html>" + b"x" * 66
            seen.append("second")
            yield b"extra"

    def oversized(req):
        return httpx.Response(200, stream=ChunkStream())

    with pytest.raises(Blocked) as exc:
        source.prepare(
            tmp_path / "oversized",
            "2501.12345v2",
            transport=httpx.MockTransport(oversized),
        )
    assert exc.value.code == "SOURCE_SIZE"
    assert seen == ["first"]
    compressed = gzip.compress(b"<html>" + b"x" * 66)

    def expanded(req):
        return httpx.Response(
            200, headers={"content-encoding": "gzip"}, content=compressed
        )

    with pytest.raises(Blocked) as exc:
        source.prepare(
            tmp_path / "expanded",
            "2501.12345v2",
            transport=httpx.MockTransport(expanded),
        )
    assert exc.value.code == "SOURCE_SIZE"
    assert not (tmp_path / "expanded/sources/original.html").exists()


def test_invalid_provider_draft_keeps_redacted_original_before_parse(
    tmp_path, monkeypatch
):
    root, _ = setup(tmp_path)
    owned(
        root, "script.json"
    ).unlink()  # fixture had a human script; exercise paid-draft failure only
    profile = tmp_path / "rate.json"
    profile.write_text(
        json.dumps(
            {
                "models": {
                    "chosen": {"input_usd_per_million": 1, "output_usd_per_million": 1}
                }
            }
        )
    )
    monkeypatch.setenv("GEMINI_API_KEY", "verysecretvalue")
    raw = b'{"candidates":[{"content":{"parts":[{"text":"not a cited script verysecretvalue"}]}}]}'

    def handler(req):
        if req.method == "GET":
            return model_available(req)
        if ":countTokens" in str(req.url):
            return httpx.Response(200, json={"totalTokens": 100})
        return httpx.Response(200, content=raw)

    body = {"contents": [{"parts": [{"text": "full source"}]}]}
    price = api.rates(profile, "chosen", output_limit=100, budget=1, run_budget=1)
    response = api.call(
        root, "script", "chosen", body, price, transport=httpx.MockTransport(handler)
    )
    assert "verysecretvalue" not in json.dumps(response)
    evidence = owned(root, "receipts/script/response-redacted.bin").read_bytes()
    assert b"verysecretvalue" not in evidence and b"[REDACTED_API_KEY]" in evidence
    result = load(root, "receipts/script/result.json")
    from paper2html.video.core import digest

    assert result["response_sha256"] == digest(raw)
    assert result["redaction_notice"] == "provider echoed credential"
    assert not owned(root, "script.json").exists()
    # Same malformed candidate through draft also leaves response evidence before validation.
    other = tmp_path / "draft-project"
    other.mkdir()
    file = tmp_path / "paper.pdf"
    pdf(file)
    source.prepare(other, str(file))
    with pytest.raises(Blocked) as exc:
        api.draft(
            other, "chosen", profile, 1, 1, 100, transport=httpx.MockTransport(handler)
        )
    assert exc.value.code == "SCRIPT_INVALID"
    assert owned(other, "receipts/script/response-redacted.bin").exists()
    assert not owned(other, "script.json").exists()
    assert (
        b"verysecretvalue"
        not in owned(other, "receipts/script/response-redacted.bin").read_bytes()
    )


def test_status_blocked_json_exit_two_and_doctor_diagnostic_exception(tmp_path):
    root, _ = setup(tmp_path)
    response = runner.invoke(app, ["video", "status", str(root), "--json"])
    assert response.exit_code == 2
    payload = json.loads(response.output)
    assert payload["status"] == "blocked" and payload["error_code"]
    assert payload["blocker"] and payload["next_actions"]
    diagnostic = runner.invoke(app, ["video", "doctor", str(root), "--json"])
    assert diagnostic.exit_code == 0 and json.loads(diagnostic.output)["blockers"]


def test_malformed_provider_json_preserved_redacted_without_second_post(
    tmp_path, monkeypatch
):
    root, _ = setup(tmp_path)
    profile = tmp_path / "price.json"
    profile.write_text(
        json.dumps(
            {
                "models": {
                    "chosen": {"input_usd_per_million": 1, "output_usd_per_million": 1}
                }
            }
        )
    )
    monkeypatch.setenv("GEMINI_API_KEY", "private-api-key")
    posted = []
    raw = b"not JSON; provider echoed private-api-key"

    def handler(req):
        if req.method == "GET":
            return model_available(req)
        if ":countTokens" in str(req.url):
            return httpx.Response(200, json={"totalTokens": 10})
        posted.append(req)
        return httpx.Response(200, content=raw)

    price = api.rates(profile, "chosen", output_limit=100, budget=1, run_budget=1)
    body = {"contents": [{"parts": [{"text": "source"}]}]}
    with pytest.raises(Blocked) as exc:
        api.call(
            root,
            "draft-failed",
            "chosen",
            body,
            price,
            transport=httpx.MockTransport(handler),
        )
    assert exc.value.code == "BILLING_UNKNOWN" and len(posted) == 1
    evidence = owned(root, "receipts/draft-failed/response-redacted.bin").read_bytes()
    from paper2html.video.core import digest

    assert b"private-api-key" not in evidence and b"[REDACTED_API_KEY]" in evidence
    assert load(root, "receipts/draft-failed/result.json")["response_sha256"] == digest(
        raw
    )
    with pytest.raises(Blocked) as exc:
        api.call(
            root,
            "draft-failed",
            "chosen",
            body,
            price,
            transport=httpx.MockTransport(handler),
        )
    assert exc.value.code == "BILLING_UNKNOWN" and len(posted) == 1


def test_runtime_dependency_symlink_outside_tree_is_rejected(tmp_path):
    from paper2html.video import build as film

    root, _ = setup(tmp_path)
    fake_runtime(root)
    target = tmp_path / "outside.js"
    target.write_text("outside")
    escape = owned(root, "runtime/node_modules/hyperframes/escape.js")
    escape.symlink_to(target)
    with pytest.raises(Blocked) as exc:
        film.runtime_tree_hash(root)
    assert exc.value.code == "UNSAFE_PATH"


def test_echoed_key_redacted_in_immutable_draft_and_editable_script(
    tmp_path, monkeypatch
):
    root, _ = setup(tmp_path)
    owned(root, "script.json").unlink()
    original = owned(root, "sources/original.pdf").read_bytes()
    profile = tmp_path / "rate.json"
    profile.write_text(
        json.dumps(
            {
                "models": {
                    "draft-model": {
                        "input_usd_per_million": 1,
                        "output_usd_per_million": 1,
                    }
                }
            }
        )
    )
    key = "credential-cannot-leak"
    monkeypatch.setenv("GEMINI_API_KEY", key)
    candidate = {
        "source_revision": None,
        "scenes": [
            {
                "id": "one",
                "title": key,
                "narration": "Scientific explanation " + key,
                "claims": [
                    {
                        "claim": "A fact",
                        "source": "pdf",
                        "pdf_page": 1,
                        "quote": "Another Research Topic: validated fact",
                    }
                ],
                "visual_refs": [{"pdf_page": 1}],
            }
        ],
    }
    raw = json.dumps(
        {"candidates": [{"content": {"parts": [{"text": json.dumps(candidate)}]}}]}
    ).encode()

    def handler(req):
        if req.method == "GET":
            return model_available(req)
        if ":countTokens" in str(req.url):
            return httpx.Response(200, json={"totalTokens": 10})
        return httpx.Response(200, content=raw)

    api.draft(
        root, "draft-model", profile, 1, 1, 100, transport=httpx.MockTransport(handler)
    )
    for name in (
        "receipts/script/response-redacted.bin",
        "receipts/script/draft-original-redacted.txt",
        "script.json",
    ):
        data = owned(root, name).read_bytes()
        assert key.encode() not in data and b"[REDACTED_API_KEY]" in data
    assert (
        load(root, "receipts/script/result.json")["redaction_notice"]
        == "provider echoed credential"
    )
    assert owned(root, "sources/original.pdf").read_bytes() == original
    # The immutable draft copy remains distinct from independently editable script.json.
    immutable = owned(root, "receipts/script/draft-original-redacted.txt").read_bytes()
    from paper2html.video.core import digest

    assert load(root, "receipts/script/draft-evidence.json")["draft_sha256"] == digest(
        immutable
    )
    current = load(root, "script.json")
    current["scenes"][0]["title"] = "Human revised title"
    save(root, "script.json", current)
    assert (
        owned(root, "receipts/script/draft-original-redacted.txt").read_bytes()
        == immutable
    )


def test_provider_http_error_body_redacted_and_original_hash_retained(
    tmp_path, monkeypatch
):
    root, _ = setup(tmp_path)
    profile = tmp_path / "rate.json"
    profile.write_text(
        json.dumps(
            {
                "models": {
                    "model": {"input_usd_per_million": 1, "output_usd_per_million": 1}
                }
            }
        )
    )
    monkeypatch.setenv("GEMINI_API_KEY", "do-not-store-this-key")
    raw = b'{"error":"do-not-store-this-key was refused"}'

    def handler(req):
        if req.method == "GET":
            return model_available(req)
        if ":countTokens" in str(req.url):
            return httpx.Response(200, json={"totalTokens": 10})
        return httpx.Response(403, content=raw)

    price = api.rates(profile, "model", output_limit=100, budget=1, run_budget=1)
    with pytest.raises(Blocked) as exc:
        api.call(
            root,
            "script",
            "model",
            {"contents": [{"parts": [{"text": "source"}]}]},
            price,
            transport=httpx.MockTransport(handler),
        )
    assert exc.value.code == "BILLING_UNKNOWN"
    retained = owned(root, "receipts/script/response-redacted.bin").read_bytes()
    assert (
        b"do-not-store-this-key" not in retained and b"[REDACTED_API_KEY]" in retained
    )
    from paper2html.video.core import digest

    metadata = load(root, "receipts/script/result.json")
    assert metadata["response_sha256"] == digest(raw)
    assert (
        metadata["http_status"] == 403
        and metadata["redaction_notice"] == "provider echoed credential"
    )


@pytest.mark.parametrize(
    "width,height",
    [
        (12000.0, 800.0),  # predicted side exceeds 16,384
        (8000.0, 8000.0),  # predicted raster exceeds 25 megapixels
        (float("inf"), 100.0),
        (100.0, 0.0),
    ],
)
def test_pdf_page_asset_rejects_unsafe_geometry_before_pixmap(
    tmp_path, monkeypatch, width, height
):
    from types import SimpleNamespace

    root, _ = setup(tmp_path)
    calls = []

    class FakePage:
        rect = SimpleNamespace(
            x0=0.0, y0=0.0, x1=width, y1=height, width=width, height=height
        )

        def get_pixmap(self, **kwargs):
            calls.append(kwargs)
            raise AssertionError("Native raster allocation must not be reached")

    class FakeDocument:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def __getitem__(self, index):
            assert index == 0
            return FakePage()

    monkeypatch.setattr(source.pymupdf, "open", lambda *args, **kwargs: FakeDocument())
    with pytest.raises(Blocked) as exc:
        source.page_asset(root, 1)
    assert exc.value.code == "SOURCE_PAGE_SIZE"
    assert calls == []
    assert not owned(root, "assets/pages/page-1.png").exists()


def test_nested_unicode_escaped_key_echo_is_redacted_before_draft_artifacts(
    tmp_path, monkeypatch
):
    from paper2html.video.core import digest

    root, _ = setup(tmp_path)
    owned(root, "script.json").unlink()
    original = owned(root, "sources/original.pdf").read_bytes()
    profile = tmp_path / "rates.json"
    profile.write_text(
        json.dumps(
            {
                "models": {
                    "draft-model": {
                        "input_usd_per_million": 1,
                        "output_usd_per_million": 1,
                    }
                }
            }
        )
    )
    key = "credential-cannot-leak"
    monkeypatch.setenv("GEMINI_API_KEY", key)
    draft = {
        "source_revision": None,
        "scenes": [
            {
                "id": "one",
                "title": key,
                "narration": "Grounded explanation.",
                "claims": [
                    {
                        "claim": "Fact",
                        "source": "pdf",
                        "pdf_page": 1,
                        "quote": "Another Research Topic: validated fact",
                    }
                ],
                "visual_refs": [{"pdf_page": 1}],
            }
        ],
    }
    unicode_escaped = "".join(f"\\u{ord(char):04x}" for char in key)
    candidate = json.dumps(draft).replace(key, unicode_escaped)
    assert json.loads(candidate)["scenes"][0]["title"] == key
    raw = json.dumps(
        {"candidates": [{"content": {"parts": [{"text": candidate}]}}]}
    ).encode()
    assert (
        key.encode() not in raw
    )  # literal-byte scrubbing alone cannot detect this echo

    def handler(req):
        if req.method == "GET":
            return model_available(req)
        if ":countTokens" in str(req.url):
            return httpx.Response(200, json={"totalTokens": 10})
        return httpx.Response(200, content=raw)

    api.draft(
        root, "draft-model", profile, 1, 1, 100, transport=httpx.MockTransport(handler)
    )
    current = load(root, "script.json")
    assert current["scenes"][0]["title"] == "[REDACTED_API_KEY]"
    assert key not in json.dumps(current)
    retained = owned(root, "receipts/script/response-redacted.bin").read_bytes()
    nested = json.loads(retained)["candidates"][0]["content"]["parts"][0]["text"]
    assert json.loads(nested)["scenes"][0]["title"] == "[REDACTED_API_KEY]"
    immutable = owned(root, "receipts/script/draft-original-redacted.txt").read_bytes()
    assert json.loads(immutable)["scenes"][0]["title"] == "[REDACTED_API_KEY]"
    for name in (
        "receipts/script/response-redacted.bin",
        "receipts/script/draft-original-redacted.txt",
        "receipts/script/draft-evidence.json",
        "script.json",
    ):
        assert key.encode() not in owned(root, name).read_bytes()
    metadata = load(root, "receipts/script/result.json")
    assert metadata["response_sha256"] == digest(raw)
    assert metadata["redaction_notice"] == "provider echoed credential"
    assert owned(root, "sources/original.pdf").read_bytes() == original
