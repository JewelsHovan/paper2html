"""Ingestion budgets; PDF normalization also shares the image dimension guard.

Defaults: 32 MiB HTTP bodies, 128 MiB PDF sources/downloads, 16 MiB images,
25 megapixels and 16,384 pixels per side, 500 PDF pages at 150 DPI.
HTTP uses a 30-second per-operation timeout and a 120-second checked deadline
per GET/redirect chain, with at most four redirects and no automatic retries.
Encoded and decoded HTTP bodies each have the byte budget; gzip and zlib
deflate are supported, but concatenated members/trailing bytes are rejected.
The deadline is checked between blocking operations/chunks, not a process kill.
Native PDF/SVG work is in-process: dimensions/bytes are bounded, CPU time is not.
"""

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class IngestionLimits:
    body_bytes: int = 32 * 1024 * 1024
    source_bytes: int = 128 * 1024 * 1024
    image_bytes: int = 16 * 1024 * 1024
    pixels: int = 25_000_000
    dimension: int = 16_384
    pages: int = 500
    redirects: int = 4
    request_seconds: float = 30.0
    total_seconds: float = 120.0

    def __post_init__(self):
        for name, value in vars(self).items():
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"Invalid ingestion limit: {name}")
            if not name.endswith("seconds") and not isinstance(value, int):
                raise ValueError(f"Limit must be integer: {name}")


LIMITS = IngestionLimits()


def validate_image_dimensions(
    width: int, height: int, *, limits: IngestionLimits = LIMITS
) -> None:
    """Reject unsafe declared dimensions before native image extraction."""
    if (
        type(width) is not int
        or type(height) is not int
        or min(width, height) <= 0
        or max(width, height) > limits.dimension
        or width * height > limits.pixels
    ):
        raise ValueError("Extracted image pixel limit exceeded")
