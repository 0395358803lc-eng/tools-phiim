"""Credential storage and cookie parsing helpers for Google Flow."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .credentials import CredentialVaultError, EncryptedCredentialVault


class FlowCredentialError(RuntimeError):
    """Raised when Flow credentials cannot be parsed or persisted safely."""


class CookieVault:
    """Encrypted Google Flow cookie vault scoped to the Windows user account."""

    def __init__(self, path: Path) -> None:
        self._vault = EncryptedCredentialVault(path)

    @property
    def path(self) -> Path:
        return self._vault.path

    def save(self, cookies: dict[str, str], raw: list[dict[str, Any]] | None) -> None:
        try:
            self._vault.save({"cookies": cookies, "raw": raw})
        except CredentialVaultError as exc:
            raise FlowCredentialError("Unable to save Google Flow cookie vault") from exc

    def load(self) -> tuple[dict[str, str], list[dict[str, Any]] | None]:
        if not self.path.is_file():
            return {}, None
        try:
            payload = self._vault.load()
            cookies = {
                str(key): str(value) for key, value in payload.get("cookies", {}).items() if value
            }
            raw = payload.get("raw")
            return cookies, raw if isinstance(raw, list) else None
        except (CredentialVaultError, ValueError, TypeError) as exc:
            raise FlowCredentialError("Unable to read Google Flow cookie vault") from exc

    def clear(self) -> None:
        try:
            self._vault.clear()
        except CredentialVaultError as exc:
            raise FlowCredentialError("Unable to clear Google Flow cookie vault") from exc


def parse_cookie_input(value: str) -> tuple[dict[str, str], list[dict[str, Any]] | None]:
    """Parse JSON cookie exports, cookie dictionaries, or a Cookie header string."""
    text = value.strip()
    raw: Any = None
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        pass

    raw_list: list[dict[str, Any]] | None = None
    cookies: dict[str, str] = {}
    if isinstance(raw, dict):
        source = raw.get("cookies", raw)
        if isinstance(source, list):
            raw = source
        elif isinstance(source, dict):
            cookies = {
                str(key): str(item)
                for key, item in source.items()
                if isinstance(item, (str, int, float)) and str(item)
            }
    if isinstance(raw, list):
        raw_list = [item for item in raw if isinstance(item, dict)]
        cookies = {
            str(item["name"]): str(item["value"])
            for item in raw_list
            if item.get("name") and item.get("value") is not None
        }
    if not cookies:
        for part in text.split(";"):
            if "=" not in part:
                continue
            name, item = part.strip().split("=", 1)
            if name and item:
                cookies[name] = item
    if not cookies:
        raise FlowCredentialError("Cookie input is not valid JSON or Cookie header format")
    return cookies, raw_list
