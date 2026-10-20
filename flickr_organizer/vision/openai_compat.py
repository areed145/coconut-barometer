from __future__ import annotations

import base64
import json

from .base import VisionProvider, parse_json_loose


class OpenAICompatProvider(VisionProvider):
    """OpenAI Chat Completions, or any compatible server (Ollama, LM Studio, vLLM…).

    For Ollama: provider: ollama, model: e.g. qwen2.5vl:7b or llava:13b
    (base_url defaults to http://localhost:11434/v1).
    """

    name = "openai"

    def __init__(self, model: str, api_key: str | None = None, base_url: str | None = None, **_):
        super().__init__(model)
        from openai import OpenAI  # lazy

        kwargs = {"max_retries": 4}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
            self.name = "openai-compat"
        self.client = OpenAI(**kwargs)

    def classify(self, image: bytes, media_type: str, prompt: str, schema: dict) -> dict:
        data_url = f"data:{media_type};base64,{base64.b64encode(image).decode()}"
        full_prompt = (
            f"{prompt}\n\nRespond with ONLY a JSON object matching this JSON Schema:\n"
            f"{json.dumps(schema)}"
        )
        # Newer OpenAI models want max_completion_tokens; compatible servers expect max_tokens.
        limit = {"max_tokens": 600} if self.name == "openai-compat" else {"max_completion_tokens": 600}
        resp = self.client.chat.completions.create(
            model=self.model,
            **limit,
            response_format={"type": "json_object"},
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": full_prompt},
                ],
            }],
        )
        out = parse_json_loose(resp.choices[0].message.content or "")
        if resp.usage:
            out["_usage"] = {"input": resp.usage.prompt_tokens, "output": resp.usage.completion_tokens}
        return out
