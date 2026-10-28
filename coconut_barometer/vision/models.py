"""The models a vision provider offers (#167): Settings' model dropdown, and the check when a model is saved, so a
typo or a retired model is caught there instead of on the first photo classified.

Claude: the Models API (only models that take images). OpenAI or a compatible server (Ollama…): GET {base_url}/models.
"""
from __future__ import annotations

import hashlib
import time

import requests

# When the provider can't be asked (no key yet, offline): the current Claude models, cheapest first.
BUILT_IN = {"claude": [{"id": "claude-haiku-5-5", "label": "Claude Haiku 5.5"},
                       {"id": "claude-sonnet-5-5", "label": "Claude Sonnet 5.5"},
                       {"id": "claude-opus-5-5", "label": "Claude Opus 5.5"}]}
TTL = 600           # seconds a provider's list is kept
TIMEOUT = 10


def _name(vcfg: dict) -> str:
    name = vcfg["provider"].lower()
    return "claude" if name == "anthropic" else name


def _base_url(vcfg: dict) -> str:
    if vcfg["base_url"]:
        return vcfg["base_url"]
    return "http://localhost:11434/v1" if _name(vcfg) == "ollama" else "https://api.openai.com/v1"


class Models:
    """Asks the provider; Runtime keeps one (tests swap in a fake with the same two methods)."""

    def __init__(self):
        self._cache: dict[tuple, tuple[float, list[dict]]] = {}

    def _claude(self, vcfg: dict):
        import anthropic  # lazy

        if not vcfg["api_key"]:
            raise LookupError("no API key yet")
        return anthropic.Anthropic(api_key=vcfg["api_key"], base_url=vcfg["base_url"] or None, max_retries=1, timeout=TIMEOUT)

    def available(self, vcfg: dict) -> list[dict]:
        """[{id, label}] the provider offers. Raises when it can't be asked."""
        key = (_name(vcfg), vcfg["base_url"], hashlib.sha256((vcfg["api_key"] or "").encode()).hexdigest())
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < TTL:
            return hit[1]
        if key[0] == "claude":
            out = [{"id": m.id, "label": m.display_name or m.id} for m in self._claude(vcfg).models.list(limit=100)
                   if getattr(m, "lifecycle", "active") != "retired"
                   and not (m.capabilities and not m.capabilities.image_input.supported)]
        else:
            headers = {"Authorization": f"Bearer {vcfg['api_key']}"} if vcfg["api_key"] else {}
            r = requests.get(_base_url(vcfg).rstrip("/") + "/models", headers=headers, timeout=TIMEOUT)
            r.raise_for_status()
            out = sorted(({"id": m["id"], "label": m["id"]} for m in r.json().get("data", [])), key=lambda m: m["id"])
        self._cache[key] = (time.monotonic(), out)
        return out

    def problem(self, vcfg: dict) -> str | None:
        """Why vision.model won't work with this provider, when the provider says so; None when it's fine or the
        provider can't be asked (no key, offline: the first classify will tell)."""
        model = vcfg["model"]
        if _name(vcfg) == "claude":
            import anthropic

            try:
                m = self._claude(vcfg).models.retrieve(model)      # takes an alias too
            except anthropic.NotFoundError:
                return f"vision.model: Anthropic has no model {model!r}"
            except Exception:
                return None
            if m.capabilities and not m.capabilities.image_input.supported:
                return f"vision.model: {model} doesn't take images"
            return None
        try:
            ids = {m["id"] for m in self.available(vcfg)}
        except Exception:
            return None
        return None if model in ids else f"vision.model: {_base_url(vcfg)} has no model {model!r}"


def choices(models, vcfg: dict) -> dict:
    """For the model dropdown: {models: [{id, label}], source: "provider" | "built-in" | None, error, current}.
    The current model is always among them, marked missing when the provider was asked and doesn't list it."""
    error = None
    try:
        listed, source = models.available(vcfg), "provider"
    except Exception as e:
        error = str(e) or type(e).__name__
        listed, source = BUILT_IN.get(_name(vcfg), []), "built-in" if _name(vcfg) in BUILT_IN else None
    listed = [dict(m) for m in listed]
    current = vcfg["model"]
    if current and current not in {m["id"] for m in listed}:
        listed.insert(0, {"id": current, "label": current, "missing": source == "provider"})
    return {"models": listed if source else [], "source": source, "error": error, "current": current}
