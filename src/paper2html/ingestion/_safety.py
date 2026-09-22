"""Bounded image validation and owned-cache writes. No in-process SVG time guarantee."""

import logging
import os
import sqlite3
import tempfile
from io import BytesIO
from pathlib import Path
from PIL import Image
from paper2html.ingestion._limits import LIMITS

logger = logging.getLogger(__name__)


def safe_path(root: Path, *parts: str) -> Path:
    """Trust the caller's cache root, never symlinks in app-owned descendants."""
    trusted = root.resolve()
    target = trusted
    for part in parts:
        relative = Path(part)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Cache path escapes root")
        for component in relative.parts:
            target = target / component
            if target.is_symlink():
                raise ValueError(f"Symlink in ingestion cache path: {target}")
    if not target.resolve().is_relative_to(trusted):
        raise ValueError("Cache path escapes root")
    return target


def check_owned(root: Path, path: Path) -> Path:
    """Recheck a previously constructed owned path against the original root."""
    try:
        relative = path.absolute().relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("Cache path escapes root") from exc
    return safe_path(root, str(relative))


def atomic_write(data: bytes, dest: Path, *, root: Path | None = None) -> None:
    dest = (
        check_owned(root, dest)
        if root is not None
        else safe_path(dest.parent, dest.name)
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(tmp, dest)
        if root is not None:
            from paper2html.cache import register_ingestion_artifact

            try:
                register_ingestion_artifact(root, dest)
            except (sqlite3.Error, OSError) as exc:
                logger.warning(
                    "Published ingestion artifact %s was not registered (%s); "
                    "content remains usable and will be retained by cache clear. "
                    "Check cache catalog permissions/locking before retrying.",
                    dest,
                    exc,
                )
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def raster(data: bytes) -> bytes:
    """Validate without changing the raster format or byte representation."""
    if len(data) > LIMITS.image_bytes:
        raise ValueError("Image byte limit exceeded")
    try:
        with Image.open(BytesIO(data)) as image:
            if (
                image.width * image.height > LIMITS.pixels
                or max(image.size) > LIMITS.dimension
            ):
                raise ValueError("Image pixel limit exceeded")
            if image.format not in {"PNG", "JPEG", "GIF", "WEBP"}:
                raise ValueError("Unsupported raster type")
            if getattr(image, "is_animated", False):
                raise ValueError("Animated raster images are unsupported")
            image.verify()
        with Image.open(BytesIO(data)) as image:
            image.load()
    except Image.DecompressionBombError as exc:
        # Pillow may reject dimensions during open(), before our own checks.
        # Keep the shared invalid-image contract for adapters and cache recovery.
        raise ValueError("Image pixel limit exceeded (decompression bomb)") from exc
    return data


def cached_image(path: Path, *, root: Path | None = None) -> bool:
    path = (
        check_owned(root, path)
        if root is not None
        else safe_path(path.parent, path.name)
    )
    if not path.exists():
        return False
    try:
        if path.stat().st_size > LIMITS.image_bytes:
            return False
        raster(path.read_bytes())
        return True
    except (ValueError, OSError, SyntaxError):
        return False


def svg_png(data: bytes) -> bytes:
    from paper2html.ingestion._svg import render_svg

    return raster(render_svg(data))
