"""Encrypted credential storage for desktop and web runtimes."""

from __future__ import annotations

import base64
import ctypes
import json
import os
from ctypes import wintypes
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


class CredentialVaultError(RuntimeError):
    """Raised when encrypted credentials cannot be read or written."""


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _dpapi(data: bytes, *, protect: bool) -> bytes:
    """Protect bytes with the current Windows user's DPAPI key."""
    source_buffer = ctypes.create_string_buffer(data)
    source = _DataBlob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_byte)))
    target = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    description = "TH Media credentials" if protect else None
    if not function(
        ctypes.byref(source), description, None, None, None, 0x01, ctypes.byref(target)
    ):
        raise OSError(ctypes.get_last_error(), "Windows DPAPI failed")
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)


def _server_fernet() -> Fernet:
    """Build the Linux/web vault cipher from a server-only key."""
    raw = os.getenv("TH_MEDIA_SECRET_KEY", "").strip()
    key_file = os.getenv("TH_MEDIA_SECRET_KEY_FILE", "").strip()
    if not raw and key_file:
        try:
            raw = Path(key_file).expanduser().read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise CredentialVaultError("Không thể đọc TH_MEDIA_SECRET_KEY_FILE") from exc
    if not raw:
        raise CredentialVaultError(
            "Web/Linux credential vault cần TH_MEDIA_SECRET_KEY hoặc TH_MEDIA_SECRET_KEY_FILE"
        )
    try:
        return Fernet(raw.encode("ascii"))
    except (ValueError, TypeError):
        # Accept a high-entropy passphrase while storing only a derived Fernet key in memory.
        import hashlib

        derived = base64.urlsafe_b64encode(hashlib.sha256(raw.encode("utf-8")).digest())
        return Fernet(derived)


class EncryptedCredentialVault:
    """Atomic credential vault: DPAPI on Windows, Fernet on Linux/web."""

    _FERNET_PREFIX = b"THMF1:"
    _DPAPI_PREFIX = b"THMD1:"

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()

    def _protect(self, data: bytes) -> bytes:
        if os.name == "nt":
            return self._DPAPI_PREFIX + _dpapi(data, protect=True)
        return self._FERNET_PREFIX + _server_fernet().encrypt(data)

    def _unprotect(self, data: bytes) -> bytes:
        if data.startswith(self._FERNET_PREFIX):
            try:
                return _server_fernet().decrypt(data[len(self._FERNET_PREFIX) :])
            except InvalidToken as exc:
                raise CredentialVaultError("Credential vault key không hợp lệ") from exc
        if data.startswith(self._DPAPI_PREFIX):
            if os.name != "nt":
                raise CredentialVaultError("Windows DPAPI vault không thể đọc trên Linux")
            return _dpapi(data[len(self._DPAPI_PREFIX) :], protect=False)
        if os.name == "nt":
            # Backward compatibility with legacy desktop DPAPI files.
            return _dpapi(data, protect=False)
        raise CredentialVaultError(
            "Phát hiện credential vault Linux legacy không mã hóa; hãy nhập lại credential"
        )

    def save(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            temporary.write_bytes(self._protect(encoded))
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        except (OSError, CredentialVaultError) as exc:
            temporary.unlink(missing_ok=True)
            if isinstance(exc, CredentialVaultError):
                raise
            raise CredentialVaultError("Không thể lưu thông tin xác thực đã mã hóa") from exc

    def load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            payload = json.loads(self._unprotect(self.path.read_bytes()).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("credential payload is not an object")
            return payload
        except CredentialVaultError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise CredentialVaultError("Không thể đọc thông tin xác thực đã lưu") from exc

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            raise CredentialVaultError("Không thể xóa thông tin xác thực đã lưu") from exc
