"""Offline admission estimates, not verified invoice/tokenizer predictions.

PNG/JPEG/WebP/nonanimated GIF supported, <=5MB Anthropic / <=20MB OpenAI,
<=8000px each side and <=20 images per call.
Anthropic scales to <=1568px and ~1.15MP, then pixels/750. GPT-4o high
 detail scales into 2048px, then a 768px short side; 512px tiles cost 170
(+85 base). GPT-4o-mini uses 5667/tile +2833 base, NOT 4o's token rates.
We round dimensions upwards and add 10% vision overhead. No byte-size proxy
or arbitrary token clamp: unsupported bounds fail before dispatch.
"""

from __future__ import annotations

import io
import json
import math
from dataclasses import asdict
from PIL import Image
from paper2html.pipeline.providers.base import Message, ToolDef


def image_tokens(data: bytes, model: str) -> int:
    try:
        max_bytes = (5 if model.startswith("claude-") else 20) * 1024 * 1024
        if len(data) > max_bytes:
            raise ValueError("Image exceeds supported provider byte bound")
        with Image.open(io.BytesIO(data)) as image:
            w, h = image.size
            if image.format not in {"PNG", "JPEG", "WEBP", "GIF"} or getattr(
                image, "is_animated", False
            ):
                raise ValueError("Unsupported image format or animation")
            if not (0 < w <= 8000 and 0 < h <= 8000):
                raise ValueError("Image exceeds supported 8000px bounds")
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
    except Exception as exc:
        raise ValueError(f"Invalid/unsupported provider image: {exc}") from exc
    if model.startswith("claude-"):
        scale = min(1.0, 1568 / max(w, h), math.sqrt(1_150_000 / (w * h)))
        tokens = math.ceil(w * scale) * math.ceil(h * scale) / 750
    elif model in {"gpt-4o", "gpt-4o-mini"}:
        scale = min(1.0, 2048 / max(w, h))
        w, h = math.ceil(w * scale), math.ceil(h * scale)
        scale = min(1.0, 768 / min(w, h))
        tiles = math.ceil(math.ceil(w * scale) / 512) * math.ceil(
            math.ceil(h * scale) / 512
        )
        base, tile = (2833, 5667) if model == "gpt-4o-mini" else (85, 170)
        tokens = base + tile * tiles
    else:
        raise ValueError(f"No supported vision estimation policy for {model}")
    return math.ceil(tokens * 1.1) + 32


def input_tokens(
    messages: list[Message], tools: list[ToolDef], images: list[bytes], model: str
) -> int:
    # One UTF-8 byte/token is deliberately conservative for text, including
    # system prompts and complete schemas. Envelope overhead is per message.
    if len(images) > 20:
        raise ValueError("At most 20 images supported per admitted call")
    text = 256 + sum(len(m.content.encode()) + 32 for m in messages)
    text += len(json.dumps([asdict(t) for t in tools]).encode())
    return text + sum(image_tokens(data, model) for data in images)
