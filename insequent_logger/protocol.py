from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ModelResponse:
    content: str
    thoughts: str


def extract_model_response(raw_response: str, *, streaming: bool) -> ModelResponse:
    """Return final content and private reasoning as separate text streams."""
    payloads: list[dict[str, Any]] = []
    if streaming:
        for line in raw_response.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                value = json.loads(data)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                payloads.append(value)
    else:
        try:
            value = json.loads(raw_response)
        except json.JSONDecodeError:
            return ModelResponse(raw_response, "")
        if isinstance(value, dict):
            payloads.append(value)

    content_parts: list[str] = []
    thought_parts: list[str] = []
    for payload in payloads:
        choices = payload.get("choices") or []
        if not choices:
            continue
        choice = choices[0] or {}
        if isinstance(choice.get("text"), str):
            content_parts.append(choice["text"])
            continue
        message = choice.get("delta") or choice.get("message") or {}
        for field in ("reasoning_content", "thinking"):
            if isinstance(message.get(field), str):
                thought_parts.append(message[field])
                break
        if isinstance(message.get("content"), str):
            content_parts.append(message["content"])
        if message.get("tool_calls"):
            content_parts.append(json.dumps(message["tool_calls"], ensure_ascii=False))
    if not content_parts and not thought_parts:
        return ModelResponse(raw_response, "")
    return ModelResponse("".join(content_parts), "".join(thought_parts))


def extract_model_output(raw_response: str, *, streaming: bool) -> str:
    """Backward-compatible final-content extractor."""
    return extract_model_response(raw_response, streaming=streaming).content
