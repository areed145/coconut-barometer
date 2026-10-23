"""Pluggable vision providers.

A provider implements one method:

    classify(image: bytes, media_type: str, prompt: str, schema: dict) -> dict

and returns a dict matching `schema`. To add a provider, subclass VisionProvider,
then register it in PROVIDERS below.
"""
from __future__ import annotations

from .base import VisionProvider
from .claude import ClaudeProvider
from .openai_compat import OpenAICompatProvider

PROVIDERS = {
    "claude": ClaudeProvider,
    "anthropic": ClaudeProvider,
    "openai": OpenAICompatProvider,
    "ollama": OpenAICompatProvider,
}


def make_provider(vcfg: dict) -> VisionProvider:
    name = vcfg["provider"].lower()
    if name not in PROVIDERS:
        raise SystemExit(f"Unknown vision provider '{name}'. Options: {', '.join(PROVIDERS)}")
    kwargs = {"model": vcfg["model"], "api_key": vcfg["api_key"] or None}
    if name == "ollama":
        kwargs["base_url"] = vcfg["base_url"] or "http://localhost:11434/v1"
        kwargs["api_key"] = kwargs["api_key"] or "ollama"
    else:
        if not kwargs["api_key"]:
            raise SystemExit(f"Missing the vision API key (vision.api_key), needed for provider '{name}'.")
        if vcfg["base_url"]:
            kwargs["base_url"] = vcfg["base_url"]
    return PROVIDERS[name](**kwargs)


def provider_key(vcfg: dict) -> str:
    """The key make_provider(vcfg) would have (VisionProvider.key), without making it."""
    name = vcfg["provider"].lower()
    if name == "anthropic":
        name = "claude"
    elif name == "ollama" or (name == "openai" and vcfg["base_url"]):
        name = "openai-compat"
    return f"{name}:{vcfg['model']}"


__all__ = ["VisionProvider", "make_provider", "provider_key", "PROVIDERS"]
