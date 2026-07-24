from __future__ import annotations

from difflib import SequenceMatcher
import re
from typing import Any


def _message_key(message: dict[str, Any]) -> tuple[str, str, str]:
    content = message.get("content", {})
    content_hash = content.get("$blob", "") if isinstance(content, dict) else str(content)
    extra = message.get("extra", {})
    extra_hash = extra.get("$blob", "") if isinstance(extra, dict) else str(extra)
    return str(message.get("role", "")), content_hash, extra_hash


def diff_manifests(base: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any]:
    if not base or base.get("kind") != current.get("kind"):
        return {"mode": "snapshot", "value": current}

    result: dict[str, Any] = {"mode": "diff", "base_kind": base.get("kind")}
    if current.get("kind") == "chat":
        old = base.get("messages", [])
        new = current.get("messages", [])
        matcher = SequenceMatcher(
            None,
            [_message_key(item) for item in old],
            [_message_key(item) for item in new],
            autojunk=False,
        )
        operations: list[dict[str, Any]] = []
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                operations.append({"op": "=", "old": [i1, i2], "new": [j1, j2]})
            else:
                old_messages = old[i1:i2]
                new_messages = new[j1:j2]
                operations.append(
                    {
                        "op": {"insert": "+", "delete": "-", "replace": "~"}[tag],
                        "old": [i1, i2],
                        "new": [j1, j2],
                        "old_messages": old_messages,
                        "new_messages": new_messages,
                        "messages": new_messages,
                    }
                )
        result["messages"] = operations
    elif current.get("kind") == "completion":
        if base.get("prompt") == current.get("prompt"):
            result["prompt"] = {"op": "=", "value": "same prompt"}
        else:
            result["prompt"] = {"op": "~"}

    parameter_diff = _mapping_diff(base.get("parameters", {}), current.get("parameters", {}))
    if parameter_diff:
        result["parameters"] = parameter_diff
    return result


def _mapping_diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    for key in sorted(set(old) | set(new)):
        if key not in old:
            changes[key] = {"op": "+", "value": new[key]}
        elif key not in new:
            changes[key] = {"op": "-", "value": old[key]}
        elif old[key] != new[key]:
            changes[key] = _value_diff(old[key], new[key])
    return changes


def _value_diff(old: Any, new: Any) -> dict[str, Any]:
    if isinstance(old, dict) and isinstance(new, dict):
        return {"op": "~", "fields": _mapping_diff(old, new)}
    return {"op": "~", "old": old, "new": new}


def diff_values(old: Any, new: Any) -> Any:
    if isinstance(old, dict) and isinstance(new, dict):
        changes = _mapping_diff(old, new)
        return {"mode": "diff", "fields": changes} if changes else {"mode": "unchanged"}
    if old == new:
        return {"mode": "unchanged"}
    return {"mode": "diff", "old": old, "new": new}


def compact_text_diff(old: str, new: str) -> dict[str, Any]:
    """Line-oriented change hunks that never repeat unchanged text."""
    if old == new:
        return {"op": "=", "unchanged": f"{len(new.splitlines())} lines"}
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    matcher = SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    hunks: list[dict[str, Any]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            hunks.append({"=": f"{i2 - i1} unchanged lines"})
            continue
        hunk: dict[str, Any] = {"at_old": i1 + 1, "at_new": j1 + 1}
        if tag in ("delete", "replace"):
            removed = old_lines[i1:i2]
            # `preview` stays a compact one-line label; `text` is the full removed
            # content so Mixed can show what was removed in full, attributed to
            # the call that removed it, instead of borrowing it from the call that
            # added it. The diff is computed on read, so this costs no storage.
            hunk["-"] = {
                "lines": len(removed),
                "preview": removed[0][:180] if removed else "",
                "text": "\n".join(removed),
            }
        if tag in ("insert", "replace"):
            hunk["+"] = "\n".join(new_lines[j1:j2])
        hunks.append(hunk)
    return {"op": "~", "hunks": hunks}


def compact_token_diff(old: str, new: str) -> dict[str, Any]:
    """Changed token fragments with offsets into the current text.

    Equal text is represented only by its ranges, so large overlapping model
    responses are not repeated in the Updates pane.
    """
    if old == new:
        return {"mode": "unchanged", "similarity": 1.0, "changes": []}
    old_matches = list(re.finditer(r"\w+|[^\w\s]", old, flags=re.UNICODE))
    new_matches = list(re.finditer(r"\w+|[^\w\s]", new, flags=re.UNICODE))
    old_tokens = [match.group(0) for match in old_matches]
    new_tokens = [match.group(0) for match in new_matches]
    matcher = SequenceMatcher(None, old_tokens, new_tokens, autojunk=False)

    def span(matches: list[re.Match[str]], start: int, end: int, text: str) -> tuple[int, int]:
        if start == end:
            position = matches[start].start() if start < len(matches) else len(text)
            return position, position
        return matches[start].start(), matches[end - 1].end()

    changes: list[dict[str, Any]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        old_start, old_end = span(old_matches, i1, i2, old)
        new_start, new_end = span(new_matches, j1, j2, new)
        changes.append(
            {
                "op": {"insert": "+", "delete": "-", "replace": "~"}[tag],
                "old": old[old_start:old_end],
                "new": new[new_start:new_end],
                "old_line": old.count("\n", 0, old_start) + 1,
                "new_line": new.count("\n", 0, new_start) + 1,
                "new_start": new_start,
                "new_end": new_end,
            }
        )
    return {"mode": "diff", "similarity": matcher.ratio(), "changes": changes}
