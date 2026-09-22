"""Response namespace and conservative, explicitly tracked ingestion eviction.

Legacy root diskcache entries are deliberately not migrated or deleted. Ingestion
files predating the catalog are not adopted merely because their names look ours.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import stat

import diskcache

DEFAULT_CACHE_DIR: Path = Path.home() / ".cache" / "paper2html"
_CATALOG = "ingestion-artifacts.sqlite3"
_GENERATED = re.compile(
    r"ingestion/(?:pdf/[0-9a-f]{64}/(?:pages/[0-9]{4,}\.jpg|figures/fig_[0-9]+_[0-9]+\.(?:png|jpg|jpeg|gif|webp))"
    r"|downloads/(?:[0-9]{4}\.[0-9]{4,5}|[a-z-]+(?:\.[A-Z]{2})?_[0-9]{7})(?:v[1-9][0-9]*)?\.pdf)"
)


def _safe(root: Path, relative: str) -> Path:
    # Lazy import avoids making the cache API depend on ingestion initialization.
    from paper2html.ingestion._safety import safe_path

    return safe_path(root, relative)


def get_cache(cache_dir: Path | None = None) -> diskcache.Cache:
    """Open response entries in cache_dir/responses; never open legacy root DBs.

    Keep values in SQLite so eviction never unlinks user-replaced .val files.
    Refuse symlinks within this namespace, including SQLite sidecars.
    """
    root = (cache_dir if cache_dir is not None else DEFAULT_CACHE_DIR).resolve()
    directory = _safe(root, "responses")
    if directory.exists():
        for parent, dirs, files in os.walk(directory, followlinks=False):
            for name in dirs + files:
                if (Path(parent) / name).is_symlink():
                    raise ValueError("Symlink in response cache namespace")
    return diskcache.Cache(str(directory), disk_min_file_size=2**63 - 1)


def make_cache_key(model: str, prompt_hash: str) -> str:
    """Return a deterministic cache key for a model/prompt pair."""
    return f"{model}:{prompt_hash}"


def _catalog(root: Path) -> sqlite3.Connection:
    for suffix in ("", "-wal", "-shm", "-journal"):
        _safe(root, _CATALOG + suffix)
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(root / _CATALOG))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS artifacts (path TEXT PRIMARY KEY, identity TEXT NOT NULL)"
        )
        conn.commit()
        return conn
    except BaseException:
        conn.close()
        raise


def _identity_fd(fd: int) -> str:
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValueError("Not an exclusively owned regular file")
    digest = hashlib.sha256()
    while chunk := os.read(fd, 1024 * 1024):
        digest.update(chunk)
    after = os.fstat(fd)
    if _stamp(before) != _stamp(after):
        raise ValueError("Artifact changed while checking ownership")
    return f"{_stamp(before)}:{digest.hexdigest()}"


def _stamp(info: os.stat_result) -> str:
    return f"{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}:{info.st_ctime_ns}"


def _identity(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        return _identity_fd(fd)
    finally:
        os.close(fd)


def _evict_registered(root: Path, relative: str, identity: str) -> None:
    """Anchor deletion to no-follow directory descriptors, never symlink targets.

    Concurrent replacement of the final filename after its last identity check
    cannot be made atomic with unlink on POSIX; callers should not modify cache
    artifacts concurrently with clearing.
    """
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        parts = Path(relative).parts
        for part in parts[:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = child
        fd = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        try:
            if _identity_fd(fd) != identity:
                raise ValueError("Artifact replaced or modified")
            current = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
            if _stamp(current) != _stamp(os.fstat(fd)) or current.st_nlink != 1:
                raise ValueError("Artifact replaced while checking ownership")
            os.unlink(parts[-1], dir_fd=directory)
        finally:
            os.close(fd)
    finally:
        os.close(directory)


def register_ingestion_artifact(root: Path, path: Path) -> None:
    """Register only freshly published generated artifacts, never arbitrary reuse.

    Content plus filesystem identity protects even same-content user replacements.
    Unknown/legacy files reused by ingestion intentionally remain unregistered.
    """
    root = root.resolve()
    relative = path.absolute().relative_to(root).as_posix()
    if not _GENERATED.fullmatch(relative):
        return
    identity = _identity(_safe(root, relative))
    with closing(_catalog(root)) as conn, conn:
        conn.execute(
            "INSERT OR REPLACE INTO artifacts VALUES (?, ?)", (relative, identity)
        )


@dataclass(frozen=True)
class ClearCounts:
    responses: int = 0
    ingestion: int = 0
    skipped: int = 0


def _clear_ingestion(root: Path) -> tuple[int, int]:
    removed = 0
    protected: set[str] = set()
    if _safe(root, _CATALOG).exists():
        with closing(_catalog(root)) as conn, conn:
            for relative, identity in conn.execute(
                "SELECT path, identity FROM artifacts"
            ).fetchall():
                try:
                    if not _GENERATED.fullmatch(relative):
                        raise ValueError("Unrecognized artifact path")
                    path = _safe(root, relative)
                    if not path.exists():
                        conn.execute(
                            "DELETE FROM artifacts WHERE path = ?", (relative,)
                        )
                        continue
                    _evict_registered(root, relative, identity)
                    removed += 1
                    conn.execute("DELETE FROM artifacts WHERE path = ?", (relative,))
                except (ValueError, OSError):
                    protected.add(relative)
    # Report remaining files and symlinks, without following symlink directories.
    ingestion = root / "ingestion"
    if ingestion.is_symlink():
        protected.add("ingestion")
    elif ingestion.exists():
        for parent, dirs, files in os.walk(ingestion, followlinks=False):
            for name in files + [
                name for name in dirs if (Path(parent) / name).is_symlink()
            ]:
                protected.add((Path(parent) / name).relative_to(root).as_posix())
    return removed, len(protected)


def clear_cache_scoped(
    cache_dir: Path | None = None, *, scope: str = "responses"
) -> ClearCounts:
    """Clear a validated scope, returning entry/file counts and protected leftovers.

    Does not delete directories, library/output files, unknown ingestion files,
    or legacy root cache entries. Invalid scopes have no filesystem side effects.
    """
    if scope not in ("responses", "ingestion", "all"):
        raise ValueError("Cache scope must be responses, ingestion, or all")
    root = (cache_dir if cache_dir is not None else DEFAULT_CACHE_DIR).resolve()
    responses = ingestion = skipped = 0
    if scope in ("responses", "all"):
        with closing(get_cache(root)) as cache:
            responses = cache.clear()
    if scope in ("ingestion", "all"):
        ingestion, skipped = _clear_ingestion(root)
    return ClearCounts(responses, ingestion, skipped)


def clear_cache(cache_dir: Path | None = None, *, scope: str = "responses") -> int:
    """Compatibility count API: default clears only response entries."""
    counts = clear_cache_scoped(cache_dir, scope=scope)
    return counts.responses + counts.ingestion
