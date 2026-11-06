"""Token storage in the OS credential store. Never falls back to plain text."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path

from .storage import default_cache_root

SERVICE = "cyango-lichtfeld"
ACCOUNT = "external-publish-token"


def load_token() -> str:
    try:
        import keyring  # type: ignore[import-not-found]

        value = keyring.get_password(SERVICE, ACCOUNT)
        if value:
            return value.strip()
    except Exception:
        pass
    if os.name != "nt":
        return ""
    try:
        return _dpapi(_credential_path().read_bytes(), protect=False).decode("utf-8").strip()
    except Exception:
        return ""


def save_token(token: str) -> bool:
    token = token.strip()
    if not token:
        clear_token()
        return True
    try:
        import keyring  # type: ignore[import-not-found]

        keyring.set_password(SERVICE, ACCOUNT, token)
        return True
    except Exception:
        pass
    if os.name != "nt":
        return False
    try:
        path = _credential_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_dpapi(token.encode("utf-8"), protect=True))
        return True
    except Exception:
        return False


def clear_token() -> None:
    try:
        import keyring  # type: ignore[import-not-found]

        keyring.delete_password(SERVICE, ACCOUNT)
    except Exception:
        pass
    try:
        _credential_path().unlink(missing_ok=True)
    except OSError:
        pass


def _credential_path() -> Path:
    return default_cache_root() / "credentials.dpapi"


def _dpapi(value: bytes, *, protect: bool) -> bytes:
    """Windows DPAPI: only this Windows user can decrypt the file."""
    if os.name != "nt":
        raise OSError("Windows DPAPI is unavailable")
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    source = (ctypes.c_byte * len(value)).from_buffer_copy(value)
    blob_in = _Blob(len(value), ctypes.cast(source, ctypes.POINTER(ctypes.c_byte)))
    blob_out = _Blob()
    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    if protect:
        ok = crypt32.CryptProtectData(
            ctypes.byref(blob_in), "LichtFeld Cyango", None, None, None, 0, ctypes.byref(blob_out)
        )
    else:
        ok = crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise OSError("Windows DPAPI failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)  # type: ignore[attr-defined]
