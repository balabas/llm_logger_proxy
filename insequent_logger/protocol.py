from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ModelResponse:
    content: str
    thoughts: str
    usage: dict[str, int]


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    return float(value) if isinstance(value, (int, float)) else None


def _normalized_usage(payload: dict[str, Any]) -> dict[str, int]:
    raw = payload.get("usage")
    usage = raw if isinstance(raw, dict) else {}
    timings = payload.get("timings")
    timings = timings if isinstance(timings, dict) else {}
    input_tokens = _integer(usage.get("prompt_tokens"))
    if input_tokens is None:
        input_tokens = _integer(usage.get("input_tokens"))
    if input_tokens is None:
        input_tokens = _integer(timings.get("prompt_n"))
    output_tokens = _integer(usage.get("completion_tokens"))
    if output_tokens is None:
        output_tokens = _integer(usage.get("output_tokens"))
    if output_tokens is None:
        output_tokens = _integer(timings.get("predicted_n"))
    total_tokens = _integer(usage.get("total_tokens"))
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    # Generation speed as the server measured it. predicted_per_second is the
    # decode rate (tokens/s); fall back to predicted_n / predicted_ms if absent.
    output_per_second = _number(timings.get("predicted_per_second"))
    if output_per_second is None:
        predicted_ms = _number(timings.get("predicted_ms"))
        predicted_n = _number(timings.get("predicted_n"))
        if predicted_ms and predicted_n:
            output_per_second = predicted_n / (predicted_ms / 1000)
    input_per_second = _number(timings.get("prompt_per_second"))
    return {
        key: value
        for key, value in (
            ("input_tokens", input_tokens),
            ("output_tokens", output_tokens),
            ("total_tokens", total_tokens),
            ("output_per_second", output_per_second),
            ("input_per_second", input_per_second),
        )
        if value is not None
    }


def _merge_tool_call_deltas(
    calls: dict[int, dict[str, Any]],
    deltas: Any,
    *,
    streaming: bool,
) -> None:
    if not isinstance(deltas, list):
        return
    for position, delta in enumerate(deltas):
        if not isinstance(delta, dict):
            continue
        index = delta.get("index", position)
        if not isinstance(index, int):
            index = position
        call = calls.setdefault(
            index,
            {"index": index, "id": "", "type": "", "function": {
                "name": "", "arguments": "",
            }},
        )
        for field in ("id", "type"):
            value = delta.get(field)
            if isinstance(value, str):
                if streaming:
                    if not call[field]:
                        call[field] = value
                else:
                    call[field] = value
        function = delta.get("function")
        if not isinstance(function, dict):
            continue
        for field in ("name", "arguments"):
            value = function.get(field)
            if isinstance(value, str):
                if streaming:
                    call["function"][field] += value
                else:
                    call["function"][field] = value


def _readable_tool_calls(calls: dict[int, dict[str, Any]]) -> str:
    readable = []
    for index in sorted(calls):
        call = calls[index]
        function = call["function"]
        raw_arguments = function["arguments"]
        try:
            arguments = json.loads(raw_arguments)
        except (json.JSONDecodeError, TypeError):
            arguments = raw_arguments
        rendered: dict[str, Any] = {
            "index": index,
            "type": call["type"] or "function",
            "function": {
                "name": function["name"],
                "arguments": arguments,
            },
        }
        if call["id"]:
            rendered["id"] = call["id"]
        readable.append(rendered)
    return json.dumps(readable, ensure_ascii=False, indent=2)


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
            return ModelResponse(raw_response, "", {})
        if isinstance(value, dict):
            payloads.append(value)

    content_parts: list[str] = []
    thought_parts: list[str] = []
    tool_calls: dict[int, dict[str, Any]] = {}
    usage: dict[str, int] = {}
    for payload in payloads:
        current_usage = _normalized_usage(payload)
        if current_usage:
            usage.update(current_usage)
        choices = payload.get("choices") or []
        if not choices:
            # Native (non-OpenAI) completion endpoints, such as llama.cpp's
            # `/completion`, put the text at the top level with no `choices`.
            for field in ("reasoning_content", "thinking"):
                if isinstance(payload.get(field), str):
                    thought_parts.append(payload[field])
                    break
            if isinstance(payload.get("content"), str):
                content_parts.append(payload["content"])
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
        _merge_tool_call_deltas(
            tool_calls,
            message.get("tool_calls"),
            streaming=streaming,
        )
    if tool_calls:
        if content_parts and not content_parts[-1].endswith("\n"):
            content_parts.append("\n")
        content_parts.append(_readable_tool_calls(tool_calls))
    if not content_parts and not thought_parts:
        return ModelResponse(raw_response, "", usage)
    return ModelResponse("".join(content_parts), "".join(thought_parts), usage)


def extract_model_output(raw_response: str, *, streaming: bool) -> str:
    """Backward-compatible final-content extractor."""
    return extract_model_response(raw_response, streaming=streaming).content
