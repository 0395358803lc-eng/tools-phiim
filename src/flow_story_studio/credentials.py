"""Encrypted, user-scoped credential storage for the desktop application."""

from __future__ import annotations

import base64
import ctypes
import json
import os
from ctypes import wintypes
from pathlib import Path
from typing import Any


class CredentialVaultError(RuntimeError):
    """Raised when an encrypted credential cannot be read or written."""


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _dpapi(data: bytes, *, protect: bool) -> bytes:
    """Protect bytes with the current Windows user's DPAPI key."""
    if os.name != "nt":
        return base64.b64encode(data) if protect else base64.b64decode(data)
    source_buffer = ctypes.create_string_buffer(data)
    source = _DataBlob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_byte)))
    target = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    description = "Flow Story Studio credentials" if protect else None
    if not function(
        ctypes.byref(source), description, None, None, None, 0x01, ctypes.byref(target)
    ):
        raise OSError(ctypes.get_last_error(), "Windows DPAPI failed")
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)


class EncryptedCredentialVault:
    """Atomic JSON vault encrypted for the current Windows account."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()

    def save(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            temporary.write_bytes(_dpapi(encoded, protect=True))
            os.replace(temporary, self.path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise CredentialVaultError("Không thể lưu thông tin xác thực đã mã hóa") from exc

    def load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            payload = json.loads(_dpapi(self.path.read_bytes(), protect=False).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("credential payload is not an object")
            return payload
        except (OSError, ValueError, TypeError) as exc:
            raise CredentialVaultError("Không thể đọc thông tin xác thực đã mã hóa") from exc

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            raise CredentialVaultError("Không thể xóa thông tin xác thực đã lưu") from exc
