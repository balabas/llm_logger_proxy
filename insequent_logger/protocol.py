from __future__ import annotations

import json
from typing import Any


def extract_model_output(raw_response: str, *, streaming: bool) -> str:
    """Return model text from an OpenAI-compatible JSON or SSE response."""
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
            return raw_response
        if isinstance(value, dict):
            payloads.append(value)

    parts: list[str] = []
    for payload in payloads:
        choices = payload.get("choices") or []
        if not choices:
            continue
        choice = choices[0] or {}
        if isinstance(choice.get("text"), str):
            parts.append(choice["text"])
            continue
        message = choice.get("delta") or choice.get("message") or {}
        for field in ("reasoning_content", "thinking", "content"):
            if isinstance(message.get(field), str):
                parts.append(message[field])
        if message.get("tool_calls"):
            parts.append(json.dumps(message["tool_calls"], ensure_ascii=False))
    return "".join(parts) if parts else raw_response

