"""Parsing helpers for xKiro/OpenAI-compatible analysis responses."""

from __future__ import annotations

import json
import re
from typing import Any


def message_content(message: object) -> str:
    """Extract textual content from a provider message payload."""
    if not isinstance(message, dict):
        raise ValueError("message is not an object")
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        parts = [
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and item.get("text")
        ]
        if parts:
            return "\n".join(parts)
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning.strip():
        return reasoning
    raise ValueError("message content is empty")


def parse_json_object(content: object) -> dict[str, Any]:
    """Parse JSON while tolerating code fences and leading provider chatter."""
    if not isinstance(content, str):
        raise ValueError("content is not text")
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
    try:
        result = json.loads(cleaned)
        if isinstance(result, dict):
            return result
        if isinstance(result, list):
            return {"scenes": result}
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for index, character in enumerate(cleaned):
        if character not in "{[":
            continue
        try:
            result, _ = decoder.raw_decode(cleaned[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(result, dict):
            return result
        if isinstance(result, list):
            return {"scenes": result}
    raise ValueError("analysis is not an object")
