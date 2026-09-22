from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# (orchestrator, worker); model names supplied by callers remain unrestricted.
PROVIDER_MODEL_DEFAULTS: dict[str, tuple[str, str]] = {
    "claude": ("claude-opus-4-8", "claude-haiku-4-5"),
    "openai": ("gpt-4o", "gpt-4o-mini"),
    "mock": ("mock", "mock"),
}
PROVIDER_DEFAULTS = {
    name: models[0] for name, models in PROVIDER_MODEL_DEFAULTS.items()
}


@dataclass
class Config:
    provider: str
    model: str
    anthropic_api_key: str | None
    openai_api_key: str | None
    cache_dir: Path
    worker_model: str = ""


def load_config(
    provider: str, model: str, cache_dir: str, worker_model: str = ""
) -> Config:
    """Load configuration from env vars and CLI flags."""
    if provider not in PROVIDER_MODEL_DEFAULTS:
        raise ValueError(
            f"Unknown provider '{provider}'. Choose one of: claude, openai, mock"
        )
    for name, value in (("model", model), ("worker-model", worker_model)):
        if value and not value.strip():
            raise ValueError(f"{name} must not be whitespace-only")
    default_model, default_worker = PROVIDER_MODEL_DEFAULTS[provider]
    resolved_model = model or default_model
    resolved_cache_dir = (
        Path(cache_dir) if cache_dir else Path.home() / ".cache" / "paper2html"
    )
    return Config(
        provider=provider,
        model=resolved_model,
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
        openai_api_key=os.environ.get("OPENAI_API_KEY"),
        cache_dir=resolved_cache_dir,
        worker_model=worker_model or default_worker,
    )
