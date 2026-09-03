"""Pure compatibility/retry helpers for xKiro HTTP transport."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def build_completion_variants(base_payload: dict[str, Any]) -> list[dict[str, Any]]:
    legacy_payload = {
        key: value
        for key, value in base_payload.items()
        if key not in {"response_format", "temperature"}
    }
    completion_payload = dict(legacy_payload)
    if "max_tokens" in completion_payload:
        completion_payload["max_completion_tokens"] = completion_payload.pop("max_tokens")
    messages = legacy_payload.get("messages", [])
    combined_prompt = "\n\n".join(
        f"{str(message.get('role', 'user')).upper()}:\n{message.get('content', '')}"
        for message in messages
        if isinstance(message, dict)
    )
    user_only_legacy = {
        **legacy_payload,
        "messages": [{"role": "user", "content": combined_prompt}],
    }
    user_only_completion = {
        **completion_payload,
        "messages": [{"role": "user", "content": combined_prompt}],
    }
    return [
        {**base_payload, "response_format": {"type": "json_object"}},
        legacy_payload,
        completion_payload,
        user_only_legacy,
        user_only_completion,
    ]


def is_duplicate_in_progress(error: str) -> bool:
    folded = error.casefold()
    return "duplicate request" in folded and "processed" in folded


def inject_recovery_token(
    variants: list[dict[str, Any]], recovery_token: str
) -> list[dict[str, Any]]:
    recovered_variants: list[dict[str, Any]] = []
    for variant in variants:
        recovered = deepcopy(variant)
        recovered_messages = recovered.get("messages")
        if isinstance(recovered_messages, list) and recovered_messages:
            last_message = recovered_messages[-1]
            if isinstance(last_message, dict):
                last_message["content"] = (
                    str(last_message.get("content", ""))
                    + "\n\nTRANSPORT RECOVERY TOKEN: "
                    + recovery_token
                    + ". Ignore this token when producing the requested JSON."
                )
        recovered_variants.append(recovered)
    return recovered_variants


def retry_delay(
    base_delay: int,
    *,
    retry_after: str | None,
    maximum: int,
) -> int:
    delay = base_delay
    if retry_after and retry_after.isdigit():
        delay = min(maximum, max(delay, int(retry_after)))
    return delay
