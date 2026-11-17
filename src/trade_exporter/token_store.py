"""Persist desktop broker tokens in the current user's application data."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path


class _DataBlob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(wintypes.BYTE))]


def _windows_dpapi(data: bytes, *, encrypt: bool) -> bytes:
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    blob_pointer = ctypes.POINTER(_DataBlob)
    operation = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    description_type = wintypes.LPCWSTR if encrypt else ctypes.POINTER(wintypes.LPWSTR)
    operation.argtypes = (
        blob_pointer, description_type, blob_pointer, ctypes.c_void_p,
        ctypes.c_void_p, wintypes.DWORD, blob_pointer,
    )
    operation.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p

    buffer = ctypes.create_string_buffer(data)
    source = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(wintypes.BYTE)))
    result = _DataBlob()
    ui_forbidden = 0x01
    if not operation(ctypes.byref(source), None, None, None, None, ui_forbidden, ctypes.byref(result)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel32.LocalFree(result.data)


def load_tokens(path: Path) -> dict[str, str]:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return {}
    if os.name == "nt":
        data = _windows_dpapi(data, encrypt=False)
    tokens = json.loads(data)
    if not isinstance(tokens, dict) or any(
        not isinstance(token, str) for token in tokens.values()
    ):
        raise ValueError("Invalid broker token cache")
    return tokens


def save_tokens(path: Path, tokens: Mapping[str, str]) -> None:
    data = json.dumps(dict(tokens), ensure_ascii=False).encode("utf-8")
    if os.name == "nt":
        data = _windows_dpapi(data, encrypt=True)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        path.parent.chmod(0o700)

    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
        temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
