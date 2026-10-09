"""Fail-closed, project-local video state and evidence utilities."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile
from datetime import datetime, timezone

VERSION = 1


class Blocked(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def owned(root: Path, name: str) -> Path:
    """Never follow a symlink under the trusted project root, including outputs."""
    base = root.absolute()
    if base.is_symlink():
        raise Blocked("UNSAFE_PATH", "Project root is a symlink")
    relative = Path(name)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(x in ("..", ".") for x in relative.parts)
    ):
        raise Blocked("UNSAFE_PATH", "Path must be contained in project")
    target = base
    for part in relative.parts:
        target = target / part
        if target.is_symlink():
            raise Blocked("UNSAFE_PATH", "Symlink in project path")
    if not target.resolve().is_relative_to(base.resolve()):
        raise Blocked("UNSAFE_PATH", "Path escapes project")
    return target


def put(root: Path, name: str, data: bytes) -> None:
    target = owned(root, name)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, target)
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def save(root: Path, name: str, obj: object) -> None:
    put(root, name, (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode())


def load(root: Path, name: str) -> dict:
    try:
        value = json.loads(owned(root, name).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise Blocked("MISSING_OR_INVALID", f"Missing or invalid {name}") from exc
    if not isinstance(value, dict):
        raise Blocked("MISSING_OR_INVALID", f"Invalid {name}")
    return value


def state(root: Path) -> dict:
    value = load(root, "state.json")
    if value.get("version") != VERSION:
        raise Blocked("STATE_VERSION", "Unsupported state version")
    return value


@contextmanager
def locked(root: Path):
    """Persistent crash locks. Only operator may clear after investigating receipts."""
    root = root.absolute()
    if root.is_symlink():
        raise Blocked("UNSAFE_PATH", "Project root is a symlink")
    root.mkdir(parents=True, exist_ok=True)
    lock = owned(root, ".video.lock")
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise Blocked(
            "PROJECT_LOCKED",
            "Project locked; investigate receipts before manually removing .video.lock",
        ) from exc
    try:
        put(
            root,
            ".video.lock/owner.json",
            (json.dumps({"pid": os.getpid(), "at": now()}) + "\n").encode(),
        )
        yield
    except Exception:
        # Handled failures release lock; immutable receipts still block a repeat POST.
        owned(root, ".video.lock/owner.json").unlink()
        lock.rmdir()
        raise
    else:
        owned(root, ".video.lock/owner.json").unlink()
        lock.rmdir()


def hashes(root: Path, paths: list[str]) -> dict[str, str]:
    result = {}
    for path in paths:
        try:
            result[path] = digest(owned(root, path).read_bytes())
        except OSError as exc:
            raise Blocked("MISSING_ASSET", f"Missing asset: {path}") from exc
    return result


def unchanged(root: Path, expected: dict[str, str]) -> None:
    if hashes(root, list(expected)) != expected:
        raise Blocked("STALE_INPUT", "Approved input changed; review and approve again")


def approval(root: Path, stage: str, current: dict[str, str]) -> None:
    record = load(root, f"approvals/{stage}.json")
    if (
        record.get("version") != VERSION
        or record.get("hashes") != current
        or record.get("seal") != seal(root, stage, current)
    ):
        raise Blocked("APPROVAL_STALE", f"{stage} approval absent or stale")
    unchanged(root, current)


def manifest_source(root: Path) -> dict:
    m = load(root, "sources/manifest.json")
    if m.get("version") != VERSION:
        raise Blocked("SOURCE_VERSION", "Unsupported source manifest")
    unchanged(root, m["hashes"])
    return m


def receipt(root: Path, stage: str, info: dict) -> str:
    """Immutable one-shot reservation persisted BEFORE attempting paid POST."""
    directory = owned(root, f"receipts/{stage}")
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        raise Blocked(
            "BILLING_UNKNOWN",
            f"{stage} receipt exists; never automatically retry. Inspect billing and use a new project",
        )
    name = f"receipts/{stage}/attempt.json"
    save(
        root,
        name,
        {"version": VERSION, "status": "reserved_unknown_billing", "at": now(), **info},
    )
    return name


def seal(root: Path, stage: str, values: dict[str, str]) -> str:
    # Detect accidental approval-record edits. Project owner can edit the nonce too;
    # this is not an adversarial signature or substitute for human review.
    nonce = state(root).get("approval_nonce")
    if not isinstance(nonce, str):
        raise Blocked("STATE_VERSION", "Missing approval nonce")
    return digest((nonce + stage + json.dumps(values, sort_keys=True)).encode())


def progress(
    root: Path,
    status: str,
    actions: list[str],
    *,
    blocker: str | None = None,
    code: str | None = None,
) -> None:
    """Atomic last-known state; `video status` always recomputes on-disk truth."""
    value = state(root)
    value.update(status=status, next_actions=actions, blocker=blocker, error_code=code)
    save(root, "state.json", value)
