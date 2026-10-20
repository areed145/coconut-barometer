from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod


class VisionProvider(ABC):
    name = "base"

    def __init__(self, model: str, **kwargs):
        self.model = model

    @property
    def key(self) -> str:
        return f"{self.name}:{self.model}"

    @abstractmethod
    def classify(self, image: bytes, media_type: str, prompt: str, schema: dict) -> dict:
        ...


def parse_json_loose(text: str) -> dict:
    """Pull a JSON object out of model text (handles ```json fences and preambles)."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"No JSON object in model output: {text[:200]!r}")
    return json.loads(m.group(0))
