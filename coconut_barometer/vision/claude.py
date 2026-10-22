from __future__ import annotations

import base64

from .base import VisionProvider


class ClaudeProvider(VisionProvider):
    """Anthropic Messages API. Uses forced tool use so the output is always valid JSON."""

    name = "claude"

    def __init__(self, model: str, api_key: str | None = None, base_url: str | None = None, **_):
        super().__init__(model)
        import anthropic  # lazy

        kwargs = {"max_retries": 4}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        self.client = anthropic.Anthropic(**kwargs)

    def classify(self, image: bytes, media_type: str, prompt: str, schema: dict) -> dict:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=1000,  # room for every category score, tags, caption and description
            tools=[{
                "name": "record_classification",
                "description": "Record the classification of the photo.",
                "input_schema": schema,
            }],
            tool_choice={"type": "tool", "name": "record_classification"},
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                                 "data": base64.b64encode(image).decode()}},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        for block in resp.content:
            if block.type == "tool_use":
                out = dict(block.input)
                out["_usage"] = {"input": resp.usage.input_tokens, "output": resp.usage.output_tokens}
                return out
        raise ValueError("Model did not return a classification")
