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
        # The tool is asked for, not forced: Sonnet 5.5, Opus 5.5 and Fable 5.1 reject a forced tool_choice
        # ("tool"/"any") with a 400 (#167), so "auto" plus the prompt's last line, on every model.
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=8000,  # every category score, tags, caption and description, and the thinking newer models do first
            tools=[{
                "name": "record_classification",
                "description": "Record the classification of the photo.",
                "input_schema": schema,
            }],
            tool_choice={"type": "auto"},
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                                 "data": base64.b64encode(image).decode()}},
                    {"type": "text", "text": prompt + "\n\nRecord your answer by calling record_classification."},
                ],
            }],
        )
        for block in resp.content:
            if block.type == "tool_use" and block.name == "record_classification":
                out = dict(block.input)
                out["_usage"] = {"input": resp.usage.input_tokens, "output": resp.usage.output_tokens}
                return out
        why = {"refusal": "the model declined this photo", "max_tokens": "it ran out of room (max_tokens)"}
        raise ValueError(f"Model did not return a classification: {why.get(resp.stop_reason, f'stop_reason {resp.stop_reason}')}")
