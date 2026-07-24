#!/usr/bin/env python3
"""Record a real seven-message conversation through the Insequent proxy."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from typing import Any

import requests


def assistant_text(body: dict[str, Any]) -> tuple[str, str]:
    message = body["choices"][0]["message"]
    content = message.get("content") or ""
    thoughts = message.get("reasoning_content") or message.get("thinking") or ""
    return str(content), str(thoughts)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8081")
    parser.add_argument("--model", default="gpt-oss-20b-UD-Q4_K_XL.gguf")
    parser.add_argument(
        "--session",
        default=(
            "live-sequential-history:"
            + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        ),
    )
    parser.add_argument("--max-tokens", type=int, default=160)
    args = parser.parse_args()

    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": (
                "You are a concise project memory assistant. Preserve every explicit "
                "project fact across turns. Do not invent facts."
            ),
        }
    ]
    user_messages = [
        (
            "Project Aster uses a cobalt-blue enclosure and controller revision K4. "
            "Acknowledge these facts briefly."
        ),
        (
            "Its thermal limit is 78 C. State the newly added fact and one earlier fact."
        ),
        "List every Project Aster fact retained from this conversation.",
    ]
    headers = {
        "X-LLMTrace-Session": args.session,
        "X-LLMTrace-Branch": "main",
        "X-LLMTrace-Purpose": "sequential-history-example",
    }

    print(f"Session: {args.session}")
    for turn, user_content in enumerate(user_messages, start=1):
        messages.append({"role": "user", "content": user_content})
        response = requests.post(
            f"{args.base_url}/v1/chat/completions",
            headers=headers,
            json={
                "model": args.model,
                "messages": messages,
                "temperature": 0.1,
                "max_tokens": args.max_tokens,
                "stream": False,
            },
            timeout=600,
        )
        response.raise_for_status()
        body = response.json()
        content, thoughts = assistant_text(body)
        messages.append({"role": "assistant", "content": content})

        print(f"\nTurn {turn}")
        print(f"Call: {response.headers.get('X-LLMTrace-Call', '?')}")
        if thoughts:
            print(f"Thoughts:\n{thoughts}")
        print(f"Assistant:\n{content}")

    print("\nFinal stored message order:")
    for index, message in enumerate(messages, start=1):
        preview = message["content"].replace("\n", " ")
        print(f"{index}. {message['role']}: {preview}")
    print(f"\nOpen {args.base_url}/ and select session:\n{args.session}")


if __name__ == "__main__":
    main()
