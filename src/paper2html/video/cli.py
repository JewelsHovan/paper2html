"""Portable staged CLI; no agent-harness dependencies."""

from __future__ import annotations

import json
import os
import re
import secrets
from pathlib import Path
import shutil
import subprocess

import typer

from .core import (
    Blocked,
    approval,
    hashes,
    load,
    locked,
    manifest_source,
    owned,
    progress,
    save,
    state,
    unchanged,
)
from . import api, build as film, source
from .script import check

app = typer.Typer(
    help="Source-grounded video workflow (local project, staged consent).",
    no_args_is_help=True,
)
script_app = typer.Typer(help="Draft or import a cited narration.")
app.add_typer(script_app, name="script")


def project(path: Path) -> Path:
    return path.absolute()


def show(value):
    typer.echo(json.dumps(value, indent=2))


def execute(fn):
    try:
        return fn()
    except typer.Exit:
        raise
    except Blocked as exc:
        show(
            {
                "version": 1,
                "status": "blocked",
                "error_code": exc.code,
                "blocker": str(exc),
                "next_actions": [
                    "Inspect project state/receipts; resolve blocker manually."
                ],
            }
        )
        raise typer.Exit(2) from None
    except (OSError, KeyError, IndexError, TypeError, ValueError):
        show(
            {
                "version": 1,
                "status": "blocked",
                "error_code": "INVALID_PROJECT_DATA",
                "blocker": "File or JSON data is missing, invalid, or unreadable; inspect project and receipts.",
                "next_actions": [
                    "Inspect project files; do not retry an uncertain paid attempt."
                ],
            }
        )
        raise typer.Exit(2) from None
    except Exception:
        # Never print a traceback that may contain source text or credentials.
        # One-shot receipts still prevent repeating an uncertain paid POST.
        show(
            {
                "version": 1,
                "status": "blocked",
                "error_code": "UNEXPECTED_FAILURE",
                "blocker": "Operation failed; inspect project files and receipts. No automatic paid retry.",
                "next_actions": [
                    "Inspect state, source hashes, local runtime and any billing receipt."
                ],
            }
        )
        raise typer.Exit(2) from None


INSTRUCTIONS = """\
PAPER2HTML VIDEO — portable terminal workflow (no agent-harness dependency)
Run `paper2html video doctor --json` and `paper2html video --help` first. The existing
`paper2html process SOURCE` HTML command is independent and remains available.

SOURCE AND EVIDENCE
  paper2html video init PROJECT --source 2501.12345v2 --json
  paper2html video init PROJECT --source /absolute/path/paper.pdf --json
arXiv requires an EXPLICIT vN revision and matching full native HTML/PDF responses.
Original bytes, hashes, provenance, native HTML figure/table IDs and all physical
PDF pages are retained; local PDF is honestly PDF-only. Never invent or omit
promised source visuals. Scripts must cite real PDF pages and verbatim excerpts;
quote presence is checked, not scientific meaning. Treat paper HTML as untrusted.

SCRIPT AND PAID MODEL CALLS
  paper2html video script import PROJECT script.json --json
  # OR, after explicit human consent to one paid Gemini draft:
  paper2html video script draft PROJECT --model MODEL --rate-profile rates.json \\
    --request-budget USD --run-budget USD --max-output-tokens N --consent --json
  paper2html video script check PROJECT --json
  paper2html video voice-request PROJECT --model TTS_MODEL --voice Kore \\
    --rate-profile rates.json --request-budget USD --run-budget USD \\
    --max-output-tokens N --json
  paper2html video approve PROJECT script --json
  paper2html video approve PROJECT voice --json
  paper2html video voice PROJECT --model TTS_MODEL --voice Kore \\
    --rate-profile rates.json --request-budget USD --run-budget USD \\
    --max-output-tokens N --consent --json
A rate profile is user-reviewed JSON: {"models":{"MODEL":{"input_usd_per_million":N,
"output_usd_per_million":N}}}. Optionally declare "currency":"USD"; other
currencies are blocked. There are NO fabricated default model prices. The paid
stage checks authenticated model identity/method/input/output limits and
countTokens, then strict per-request/run ESTIMATED budgets. These estimates and
reservations are NOT invoice caps. Each paid POST gets a durable receipt first.
If an outcome is unknown, inspect the receipt and billing; NEVER blindly retry,
change project files to erase evidence, or make a fallback provider call.

HUMAN APPROVAL AND LOCAL DELIVERY
Never self-approve on a human's behalf, forge consent, or treat CLI success as a
review. The operator must read the script, verify claims against originals,
choose model/voice/budgets, and explicitly authorize paid stages. Script approval
hashes the complete voice request BEFORE paid speech. Listen to EVERY resulting
WAV and check pronunciation/duration, then:
  paper2html video approve PROJECT audio --json
  paper2html video runtime-install PROJECT --consent --json
  paper2html video build PROJECT --json
  paper2html video preview PROJECT --json
  paper2html video preview PROJECT --serve
  paper2html video approve PROJECT preview --json
  paper2html video render PROJECT --json
  paper2html video verify PROJECT --json
Runtime install alone permits npm network bootstrap (scripts disabled); normal
help/doctor/status/build never fetch via npx. Preview requires local pinned
HyperFrames strict lint + actual browser check. The non-serving preview command
reports NO guessed URL; --serve prints HyperFrames' actual URL. Inspect the whole
video, source-page legibility, captions, and audible speech before approving
render. Captions are approximate SENTENCE timing, never claimed word alignment.
Verify probes encoded streams, full decode, audio volume, and scheduled frames.
These generic nonblank/motion samples DO NOT prove original source pages appear in
the encoded frames; source_presence_verified is false with a needs_review_warning.
A human must
compare the preview and final video with the original PDF page assets.
This CLI has NO upload command. Public distribution needs separate human review.

JSON AND RECOVERY CONTRACT
Commands except this textual instructions command emit versioned JSON by default;
--json is accepted for scripting. `status PROJECT --json` recomputes hashes and
next_actions from disk. Blocked commands (including status) exit 2 with status=blocked, error_code,
blocker and next_actions; no secret or source traceback should be printed.
`doctor` is diagnostics-only: it may report blocked capabilities and exit 0.
A project lock is exclusive and a crash lock intentionally fails closed: inspect
receipts before manually resolving it. Changes to script, WAV, assets, runtime
or approvals invalidate downstream gates. Never bypass stale-review blockers.
"""


@app.command()
def instructions():
    """Print full harness-independent operating contract; bundled in the wheel."""
    typer.echo(INSTRUCTIONS.strip())


@app.command()
def doctor(
    path: Path = typer.Argument(Path(".")),
    model: str | None = typer.Option(
        None, "--model", help="Model ID to check against a reviewed rate profile"
    ),
    rate_profile: Path | None = typer.Option(None, "--rate-profile"),
    json_output: bool = typer.Option(
        False, "--json", help="JSON output (also the default)"
    ),
):
    """Offline capabilities and missing prerequisites; never fetch npm or call Gemini."""

    def run():
        r = project(path)
        node = shutil.which("node")
        npm = shutil.which("npm")
        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
        version = None
        if node:
            try:
                checked = subprocess.run(
                    [node, "--version"], capture_output=True, text=True, timeout=10
                )
                if checked.returncode == 0:
                    version = checked.stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                pass
        matched = re.fullmatch(r"v?(\d+)\.\d+\.\d+", version or "")
        node_ok = bool(matched and int(matched.group(1)) >= 22)
        runtime_version = None
        runtime_error = None
        try:
            if film.local_binary(r).is_file():
                runtime_version = load(
                    r, "runtime/node_modules/hyperframes/package.json"
                ).get("version")
                film.runtime(r)
                runtime_ok = True
            else:
                runtime_ok = False
                runtime_error = "RUNTIME_MISSING"
        except Blocked as exc:
            runtime_ok = False
            runtime_error = exc.code
        price = None
        price_error = None
        if model and rate_profile:
            try:
                item = api.rates(
                    rate_profile, model, output_limit=1, budget=1, run_budget=1
                )
                price = {
                    "model": model,
                    "currency": "USD",
                    "input_usd_per_million": item["input"],
                    "output_usd_per_million": item["output"],
                    "profile_sha256": item["profile_sha256"],
                }
            except Blocked as exc:
                price_error = exc.code
        elif model or rate_profile:
            price_error = "MODEL_AND_RATE_PROFILE_REQUIRED"
        else:
            price_error = "UNKNOWN_PRICE_UNTIL_REVIEWED_PROFILE"
        key = bool(os.environ.get("GEMINI_API_KEY"))
        blockers = []
        if not node_ok:
            blockers.append("NODE_22_REQUIRED")
        if not npm:
            blockers.append("NPM_MISSING")
        if not runtime_ok:
            blockers.append(runtime_error)
        if not ffmpeg or not ffprobe:
            blockers.append("FFMPEG_MISSING")
        if not key:
            blockers.append("NO_KEY_FOR_PAID_STAGES")
        if price_error:
            blockers.append(price_error)
        show(
            {
                "version": 1,
                "status": "ready" if not blockers else "blocked",
                "node": node,
                "node_version": version,
                "node_22_or_newer": node_ok,
                "npm": npm,
                "ffmpeg": ffmpeg,
                "ffprobe": ffprobe,
                "GEMINI_API_KEY_present": key,
                "runtime_installed": runtime_ok,
                "runtime_version": runtime_version,
                "pricing": price,
                "pricing_error": price_error,
                "browser_check": "not_run_offline",
                "capabilities": {
                    "source_preparation": True,
                    "runtime_install": node_ok and bool(npm),
                    "paid_generation": key and price is not None,
                    "local_render": runtime_ok and bool(ffmpeg and ffprobe),
                },
                "blockers": blockers,
                "error_code": blockers[0] if blockers else None,
                "blocker": ", ".join(blockers) if blockers else None,
                "next_actions": [
                    "Supply explicit reviewed USD rate profile and model; run runtime-install --consent if missing; preview executes strict browser check."
                ],
            }
        )

    execute(run)


@app.command()
def init(
    path: Path,
    source_input: str = typer.Option(
        ..., "--source", help="Explicit arXiv vN identifier/URL or local PDF path"
    ),
    json_output: bool = typer.Option(
        False, "--json", help="JSON output (also the default)"
    ),
):
    """Prepare full paired arXiv originals or honest local PDF-only source."""

    def run():
        r = project(path)
        if owned(r, "state.json").exists():
            raise Blocked("PROJECT_EXISTS", "Project already initialized")
        with locked(r):
            m = source.prepare(r, source_input)
            save(
                r,
                "state.json",
                {
                    "version": 1,
                    "source_identity": m["identity"],
                    "approval_nonce": secrets.token_hex(32),
                    "status": "ready",
                    "next_actions": ["script import or script draft"],
                    "blocker": None,
                    "error_code": None,
                },
            )
        show(
            {
                "version": 1,
                "status": "ready",
                "identity": m["identity"],
                "next_actions": ["script import or script draft"],
            }
        )

    execute(run)


@app.command()
def status(
    path: Path,
    json_output: bool = typer.Option(
        False, "--json", help="JSON output (also the default)"
    ),
):
    """Re-evaluate on-disk hashes; edits invalidate approvals without a CLI command."""

    def run():
        r = project(path)
        state(r)
        if owned(r, ".video.lock").exists():
            raise Blocked(
                "PROJECT_LOCKED",
                "Project locked; investigate receipts before manually removing .video.lock",
            )
        manifest_source(r)

        def emit(snapshot):
            with locked(r):
                progress(
                    r,
                    snapshot["status"],
                    snapshot["next_actions"],
                    blocker=snapshot.get("blocker"),
                    code=snapshot.get("error_code"),
                )
            show(snapshot)
            if snapshot["status"] == "blocked":
                raise typer.Exit(2)

        result = {
            "version": 1,
            "status": "ready",
            "next_actions": [],
            "blocker": None,
            "error_code": None,
        }
        try:
            check(r)
        except Blocked as exc:
            result.update(
                status="blocked",
                blocker=str(exc),
                error_code=exc.code,
                next_actions=["script import or script draft; then script check"],
            )
            emit(result)
            return
        for stage, paths in [
            ("script", ["script.json", "sources/manifest.json", "voice/request.json"])
        ]:
            try:
                approval(r, stage, hashes(r, paths))
            except Blocked as exc:
                result.update(
                    status="blocked",
                    blocker=str(exc),
                    error_code=exc.code,
                    next_actions=["voice-request then approve script after review"],
                )
                emit(result)
                return
        try:
            approval(
                r,
                "voice",
                hashes(
                    r, ["script.json", "sources/manifest.json", "voice/request.json"]
                ),
            )
        except Blocked as exc:
            result.update(
                status="blocked",
                blocker=str(exc),
                error_code=exc.code,
                next_actions=["voice-request then approve voice before paid synthesis"],
            )
            emit(result)
            return
        try:
            vm = load(r, "voice/manifest.json")
            paths = [
                "script.json",
                "sources/manifest.json",
                "sources/catalog.json",
                "voice/manifest.json",
            ] + [c["path"] for c in vm["clips"]]
            approval(r, "audio", hashes(r, paths))
        except Blocked as exc:
            result.update(
                status="blocked",
                blocker=str(exc),
                error_code=exc.code,
                next_actions=["voice --consent then listen and approve audio"],
            )
            emit(result)
            return
        try:
            b = film.checked(r)
        except Blocked as exc:
            result.update(
                status="blocked",
                blocker=str(exc),
                error_code=exc.code,
                next_actions=["runtime-install --consent if missing; build; preview"],
            )
            emit(result)
            return
        try:
            approval(r, "preview", hashes(r, ["build.json"] + list(b["inputs"])))
        except Blocked as exc:
            result.update(
                status="blocked",
                blocker=str(exc),
                error_code=exc.code,
                next_actions=["preview and approve preview"],
            )
            emit(result)
            return
        if owned(r, "render.json").exists():
            from .core import digest

            record = load(r, "render.json")
            try:
                unchanged(r, record["inputs"])
                movie = owned(r, "runtime/renders/final.mp4")
                if record.get("status") == "completed" and digest(
                    movie.read_bytes()
                ) != record.get("mp4_sha256"):
                    raise Blocked("RENDER_STALE", "Encoded MP4 changed since render")
            except (OSError, KeyError) as exc:
                raise Blocked("RENDER_STALE", "Render incomplete or missing") from exc
            if record.get("status") != "completed":
                result.update(
                    status="blocked",
                    error_code="RENDER_INCOMPLETE",
                    blocker="Render incomplete; inspect local runtime",
                    next_actions=["inspect render.json and runtime"],
                )
            elif owned(r, "verification.json").exists():
                report = load(r, "verification.json")
                if (
                    report.get("mp4_sha256") != record["mp4_sha256"]
                    or report.get("input_hashes") != record["inputs"]
                ):
                    result.update(
                        status="blocked",
                        error_code="REPORT_STALE",
                        blocker="Verification report does not match current render",
                        next_actions=["verify"],
                    )
                else:
                    result.update(
                        status="completed",
                        next_actions=["Human final review; no upload command"],
                    )
            else:
                result["next_actions"] = ["verify (no upload)"]
        else:
            result["next_actions"] = ["render then verify (no upload)"]
        emit(result)

    execute(run)


@script_app.command("import")
def script_import(
    path: Path,
    file: Path,
    json_output: bool = typer.Option(
        False, "--json", help="JSON output (also the default)"
    ),
):
    """Import a reviewed JSON script; never overwrite existing script."""

    def run():
        r = project(path)
        with locked(r):
            state(r)
            if owned(r, "script.json").exists():
                raise Blocked(
                    "SCRIPT_EXISTS",
                    "Script already present; create new project for a new script",
                )
            try:
                obj = json.loads(file.read_text())
            except (OSError, ValueError) as exc:
                raise Blocked("SCRIPT_SCHEMA", "Cannot read script JSON") from exc
            check(r, obj)
            save(r, "script.json", obj)
            progress(
                r, "script_ready", ["script check", "voice-request", "approve script"]
            )
        show(
            {
                "version": 1,
                "status": "ready",
                "next_actions": ["script check", "voice-request", "approve script"],
            }
        )

    execute(run)


@script_app.command("check")
def script_check(
    path: Path,
    json_output: bool = typer.Option(
        False, "--json", help="JSON output (also the default)"
    ),
):
    execute(
        lambda: (
            check(project(path)),
            show(
                {
                    "version": 1,
                    "status": "ready",
                    "next_actions": ["voice-request", "approve script"],
                }
            ),
        )
    )


@script_app.command("draft")
def script_draft(
    path: Path,
    model: str = typer.Option(...),
    rate_profile: Path = typer.Option(...),
    request_budget: float = typer.Option(...),
    run_budget: float = typer.Option(...),
    max_output_tokens: int = typer.Option(...),
    consent: bool = typer.Option(
        False, "--consent", help="Authorize ONE paid Gemini script call, no retry"
    ),
    json_output: bool = typer.Option(
        False, "--json", help="JSON output (also the default)"
    ),
):
    def run():
        if not consent:
            raise Blocked("CONSENT", "Explicit --consent required for paid draft")
        r = project(path)
        with locked(r):
            state(r)
            api.draft(
                r, model, rate_profile, request_budget, run_budget, max_output_tokens
            )
            progress(
                r, "script_ready", ["script check", "voice-request", "approve script"]
            )
        show(
            {
                "version": 1,
                "status": "ready",
                "next_actions": ["script check", "voice-request", "approve script"],
            }
        )

    execute(run)


@app.command()
def approve(
    path: Path,
    stage: str,
    json_output: bool = typer.Option(
        False, "--json", help="JSON output (also the default)"
    ),
):
    """Record hash-bound human approval: script, voice, preview."""

    def run():
        r = project(path)
        with locked(r):
            state(r)
            if stage == "script":
                check(r)
                paths = ["script.json", "sources/manifest.json", "voice/request.json"]
                request = load(r, "voice/request.json")
                if request.get("input_hashes") != hashes(
                    r, ["script.json", "sources/manifest.json"]
                ):
                    raise Blocked(
                        "VOICE_REQUEST_STALE",
                        "Voice request does not match script/source",
                    )
            elif stage == "voice":
                check(r)
                paths = ["script.json", "sources/manifest.json", "voice/request.json"]
                request = load(r, "voice/request.json")
                if request.get("input_hashes") != hashes(
                    r, ["script.json", "sources/manifest.json"]
                ):
                    raise Blocked(
                        "VOICE_REQUEST_STALE",
                        "Voice request no longer matches approved script/source",
                    )
                approval(
                    r,
                    "script",
                    hashes(
                        r,
                        ["script.json", "sources/manifest.json", "voice/request.json"],
                    ),
                )
            elif stage == "audio":
                check(r)
                approval(
                    r,
                    "voice",
                    hashes(
                        r,
                        ["script.json", "sources/manifest.json", "voice/request.json"],
                    ),
                )
                vm = load(r, "voice/manifest.json")
                paths = [
                    "script.json",
                    "sources/manifest.json",
                    "sources/catalog.json",
                    "voice/manifest.json",
                ] + [c["path"] for c in vm["clips"]]
            elif stage == "preview":
                b = film.checked(r)
                vm = load(r, "voice/manifest.json")
                approval(
                    r,
                    "audio",
                    hashes(
                        r,
                        [
                            "script.json",
                            "sources/manifest.json",
                            "sources/catalog.json",
                            "voice/manifest.json",
                        ]
                        + [c["path"] for c in vm["clips"]],
                    ),
                )
                evidence = load(r, "preview.json")
                unchanged(r, evidence["inputs"])
                if evidence["inputs"] != hashes(r, ["build.json"] + list(b["inputs"])):
                    raise Blocked(
                        "PREVIEW_STALE", "Preview was not run for current build"
                    )
                paths = ["build.json"] + list(b["inputs"])
            else:
                raise Blocked("STAGE", "Stage must be script, voice, audio or preview")
            from .core import seal

            values = hashes(r, paths)
            save(
                r,
                f"approvals/{stage}.json",
                {"version": 1, "hashes": values, "seal": seal(r, stage, values)},
            )
            progress(
                r,
                stage + "_approved",
                {
                    "script": ["approve voice"],
                    "voice": ["voice --consent"],
                    "audio": ["build"],
                    "preview": ["render"],
                }[stage],
            )
        show({"version": 1, "status": "approved", "stage": stage})

    execute(run)


@app.command("voice-request")
def voice_request(
    path: Path,
    model: str = typer.Option(...),
    voice_name: str = typer.Option(..., "--voice"),
    rate_profile: Path = typer.Option(...),
    request_budget: float = typer.Option(...),
    run_budget: float = typer.Option(...),
    max_output_tokens: int = typer.Option(...),
    json_output: bool = typer.Option(
        False, "--json", help="JSON output (also the default)"
    ),
):
    """Prepare a hash-bound voice/model/budget request for approval, with NO paid calls."""

    def run():
        r = project(path)
        with locked(r):
            state(r)
            check(r)
            if owned(r, "voice/request.json").exists():
                raise Blocked(
                    "VOICE_REQUEST_EXISTS",
                    "Voice request exists; create a new project to change it",
                )
            if not voice_name.isascii() or not voice_name.isalpha():
                raise Blocked("VOICE", "Explicit Gemini prebuilt voice name required")
            price = api.rates(
                rate_profile,
                model,
                output_limit=max_output_tokens,
                budget=request_budget,
                run_budget=run_budget,
            )
            save(
                r,
                "voice/request.json",
                {
                    "model": model,
                    "voice": voice_name,
                    "request_budget": request_budget,
                    "run_budget": run_budget,
                    "max_output_tokens": max_output_tokens,
                    "rate_profile_sha256": price["profile_sha256"],
                    "input_hashes": hashes(r, ["script.json", "sources/manifest.json"]),
                },
            )
            progress(
                r,
                "voice_request_ready",
                [
                    "Review voice request; approve script then voice BEFORE paid synthesis"
                ],
            )
        show(
            {
                "version": 1,
                "status": "ready",
                "next_actions": ["approve script", "approve voice", "voice --consent"],
            }
        )

    execute(run)


@app.command()
def voice(
    path: Path,
    model: str = typer.Option(...),
    voice_name: str = typer.Option(..., "--voice"),
    rate_profile: Path = typer.Option(...),
    request_budget: float = typer.Option(...),
    run_budget: float = typer.Option(...),
    max_output_tokens: int = typer.Option(...),
    consent: bool = typer.Option(False, "--consent"),
    json_output: bool = typer.Option(
        False, "--json", help="JSON output (also the default)"
    ),
):
    def run():
        if not consent:
            raise Blocked("CONSENT", "Explicit --consent required for paid voice stage")
        r = project(path)
        with locked(r):
            state(r)
            api.voice(
                r,
                model,
                voice_name,
                rate_profile,
                request_budget,
                run_budget,
                max_output_tokens,
            )
            progress(r, "voice_ready", ["listen to WAVs", "approve audio"])
        show(
            {
                "version": 1,
                "status": "ready",
                "next_actions": ["listen to WAVs; approve audio"],
            }
        )

    execute(run)


@app.command("runtime-install")
def runtime_install(
    path: Path,
    consent: bool = typer.Option(
        False, "--consent", help="Permit npm network bootstrap"
    ),
    json_output: bool = typer.Option(
        False, "--json", help="JSON output (also the default)"
    ),
):
    def run():
        if not consent:
            raise Blocked("CONSENT", "Explicit network bootstrap consent required")
        with locked(project(path)):
            state(project(path))
            film.install(project(path))
            progress(project(path), "runtime_installed", ["build"])
        show({"version": 1, "status": "ready", "next_actions": ["build"]})

    execute(run)


@app.command("build")
def build_cmd(
    path: Path,
    json_output: bool = typer.Option(
        False, "--json", help="JSON output (also the default)"
    ),
):
    def run():
        with locked(project(path)):
            state(project(path))
            info = film.build(project(path))
            progress(project(path), "built", ["preview"])
        show(
            {
                "version": 1,
                "status": "ready",
                "duration_seconds": info["duration_seconds"],
                "next_actions": ["preview"],
            }
        )

    execute(run)


@app.command()
def preview(
    path: Path,
    serve: bool = typer.Option(
        False, "--serve", help="Run local preview server until Ctrl-C"
    ),
    json_output: bool = typer.Option(
        False, "--json", help="JSON output (also the default)"
    ),
):
    """Run local HyperFrames check; optionally serve local preview, never approve."""

    def run():
        r = project(path)
        with locked(r):
            state(r)
            b = film.checked(r)
            # `check` includes the static linter and the real browser audits.
            result = film.invoke(
                r,
                "check",
                flags=("--json", "--strict", "--samples", "9", "--at-transitions"),
            )
            try:
                audit = json.loads(result.stdout)
                layout = audit["layout"]
                contrast = audit["contrast"]
                valid = (
                    result.returncode == 0
                    and audit.get("ok") is True
                    and audit.get("strict") is True
                    and audit.get("browserSkipped") is False
                    and all(
                        audit[part]["ok"] is True
                        for part in ("lint", "runtime", "layout", "motion", "contrast")
                    )
                    and audit["lint"]["filesScanned"] > 0
                    and len(layout["samples"]) > 0
                    and layout["duration"] > 0
                    and abs(layout["duration"] - b["duration_seconds"]) <= 0.5
                    and contrast["checked"] > 0
                )
            except (ValueError, KeyError, TypeError):
                valid = False
            if not valid:
                raise Blocked(
                    "BUILD_CHECK",
                    "Strict local HyperFrames lint/browser check failed or skipped/zero-sample; inspect with local hyperframes check --json --strict",
                )
            inputs = hashes(r, ["build.json"] + list(b["inputs"]))
            save(
                r,
                "preview.json",
                {
                    "version": 1,
                    "inputs": inputs,
                    "check": "passed",
                    "audit": {
                        "browserSkipped": False,
                        "lint_files": audit["lint"]["filesScanned"],
                        "layout_samples": len(layout["samples"]),
                        "contrast_checked": contrast["checked"],
                    },
                    "url": None,
                    "command": str(film.runtime(r))
                    + " preview (cwd: "
                    + str(owned(r, "runtime"))
                    + ")",
                    "note": "Run preview --serve to see HyperFrames' actual local URL, inspect full timeline/audio, then approve preview. No URL is assumed from a check alone.",
                },
            )
            progress(r, "preview_ready", ["Inspect --serve preview", "approve preview"])
        show(load(r, "preview.json"))
        if serve:
            typer.echo(
                "Local preview server prints its actual URL below. Ctrl-C to stop; approval requires separate command."
            )
            try:
                subprocess.run(
                    [str(film.runtime(r)), "preview"],
                    cwd=owned(r, "runtime"),
                    check=False,
                )
            except KeyboardInterrupt:
                pass

    execute(run)


@app.command()
def render(
    path: Path,
    json_output: bool = typer.Option(
        False, "--json", help="JSON output (also the default)"
    ),
):
    """Local final encode only after current preview approval."""

    def run():
        r = project(path)
        with locked(r):
            state(r)
            b = film.checked(r)
            approval(r, "preview", hashes(r, ["build.json"] + list(b["inputs"])))
            if (
                owned(r, "render.json").exists()
                or owned(r, "runtime/renders/final.mp4").exists()
            ):
                raise Blocked(
                    "RENDER_EXISTS",
                    "Render already attempted; inspect output, no overwrite",
                )
            save(
                r,
                "render.json",
                {
                    "version": 1,
                    "status": "started",
                    "inputs": hashes(r, ["build.json"] + list(b["inputs"])),
                },
            )
            result = film.invoke(r, "render", output="renders/final.mp4")
            if result.returncode or not owned(r, "runtime/renders/final.mp4").exists():
                raise Blocked(
                    "RENDER_FAILED",
                    "Local render failed; inspect runtime; no automatic retry",
                )
            from .core import digest

            save(
                r,
                "render.json",
                {
                    "version": 1,
                    "status": "completed",
                    "inputs": hashes(r, ["build.json"] + list(b["inputs"])),
                    "mp4_sha256": digest(
                        owned(r, "runtime/renders/final.mp4").read_bytes()
                    ),
                },
            )
            progress(r, "rendered", ["verify"])
        show({"version": 1, "status": "completed", "next_actions": ["verify"]})

    execute(run)


@app.command()
def verify(
    path: Path,
    json_output: bool = typer.Option(
        False, "--json", help="JSON output (also the default)"
    ),
):
    """Validate actual encoded video/audio, full decode and timed nonblank sample frames."""

    def run():
        from PIL import Image, ImageStat, ImageChops
        import re
        from .core import digest

        r = project(path)
        with locked(r):
            state(r)
            b = film.checked(r)
            record = load(r, "render.json")
            if record.get("status") != "completed":
                raise Blocked("RENDER_MISSING", "No completed render")
            unchanged(r, record["inputs"])
            mp4 = owned(r, "runtime/renders/final.mp4")
            if digest(mp4.read_bytes()) != record["mp4_sha256"]:
                raise Blocked("RENDER_STALE", "Rendered MP4 modified")
            if not shutil.which("ffprobe") or not shutil.which("ffmpeg"):
                raise Blocked("FFMPEG_MISSING", "ffprobe and ffmpeg required")
            probe = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_streams",
                    "-show_format",
                    "-of",
                    "json",
                    str(mp4),
                ],
                capture_output=True,
                text=True,
                timeout=90,
            )
            if probe.returncode:
                raise Blocked("PROBE_FAILED", "ffprobe failed")
            data = json.loads(probe.stdout)
            video = next(
                (s for s in data["streams"] if s["codec_type"] == "video"), None
            )
            audio = next(
                (s for s in data["streams"] if s["codec_type"] == "audio"), None
            )
            duration = float(data["format"]["duration"])
            if (
                not video
                or not audio
                or (video["width"], video["height"]) != (1920, 1080)
                or abs(duration - b["duration_seconds"]) > 0.5
            ):
                raise Blocked(
                    "STREAM_MISMATCH",
                    "Encoded dimensions, duration or audio/video streams differ",
                )
            decode = subprocess.run(
                ["ffmpeg", "-v", "error", "-i", str(mp4), "-f", "null", "-"],
                capture_output=True,
                timeout=3600,
            )
            if decode.returncode or decode.stderr:
                raise Blocked("DECODE_FAILED", "Full encoded decode failed")
            volume = subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-i",
                    str(mp4),
                    "-vn",
                    "-af",
                    "volumedetect",
                    "-f",
                    "null",
                    "-",
                ],
                capture_output=True,
                timeout=3600,
            )
            matched = re.search(rb"mean_volume:\s*(-?[\d.]+) dB", volume.stderr)
            if volume.returncode or not matched or float(matched.group(1)) <= -35:
                raise Blocked("AUDIO_SILENT", "Audio absent or effectively silent")
            samples = []
            for span in b["spans"]:
                frames = []
                for fraction in (0.2, 0.8):
                    sec = span["start"] + span["duration"] * fraction
                    out = subprocess.run(
                        [
                            "ffmpeg",
                            "-v",
                            "error",
                            "-ss",
                            str(sec),
                            "-i",
                            str(mp4),
                            "-frames:v",
                            "1",
                            "-f",
                            "image2pipe",
                            "-vcodec",
                            "png",
                            "-",
                        ],
                        capture_output=True,
                        timeout=90,
                    )
                    if out.returncode:
                        raise Blocked("PROOF_FAILED", "Frame extraction failed")
                    import io

                    with Image.open(io.BytesIO(out.stdout)) as image:
                        if (
                            image.size != (1920, 1080)
                            or ImageStat.Stat(image.convert("L")).stddev[0] < 5
                        ):
                            raise Blocked(
                                "PROOF_FAILED", "Encoded frame blank or wrong size"
                            )
                        frames.append(image.convert("RGB"))
                    samples.append(
                        {
                            "scene": span["id"],
                            "second": sec,
                            "nonblank": True,
                            "source_page_presence": "unverified",
                        }
                    )
                diff = ImageStat.Stat(ImageChops.difference(*frames)).mean
                if sum(diff) / 3 < 0.25:
                    raise Blocked(
                        "MOTION_MISSING",
                        "Encoded scene is static at proof times; check seekable timeline",
                    )
            save(
                r,
                "verification.json",
                {
                    "version": 1,
                    "status": "passed",
                    "mp4_sha256": record["mp4_sha256"],
                    "input_hashes": record["inputs"],
                    "duration_seconds": duration,
                    "streams": {
                        "video": video["codec_name"],
                        "audio": audio["codec_name"],
                    },
                    "full_decode": True,
                    "audio_mean_db": float(matched.group(1)),
                    "proof_schedule": samples,
                    "verification_scope": "container, full decode, audio volume, nonblank/motion samples",
                    "source_presence_verified": False,
                    "needs_review_warning": "Original source-page presence in encoded scenes is UNVERIFIED; compare final frames against the original PDF assets manually.",
                    "source_visuals": {
                        "status": "unverified",
                        "reason": "Nonblank/motion samples cannot establish that the original PDF page is visible in the encoded scene; human comparison required.",
                    },
                    "limitations": "No encoded source-page presence or scientific truth claim; samples do not prove every pose. Human preview/final-video review required.",
                },
            )
            progress(r, "completed", ["Human final review; no upload command"])
        show(load(r, "verification.json"))

    execute(run)
