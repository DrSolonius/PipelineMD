"""Per-user encrypted SSH passwords using Windows DPAPI."""
from __future__ import annotations
import ctypes
from ctypes import wintypes
from pathlib import Path

class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

CRYPT32 = ctypes.WinDLL("crypt32", use_last_error=True)
KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
CRYPT32.CryptProtectData.argtypes = [ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR,
    ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(DATA_BLOB)]
CRYPT32.CryptProtectData.restype = wintypes.BOOL
CRYPT32.CryptUnprotectData.argtypes = [ctypes.POINTER(DATA_BLOB), ctypes.c_void_p,
    ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(DATA_BLOB)]
CRYPT32.CryptUnprotectData.restype = wintypes.BOOL
KERNEL32.LocalFree.argtypes = [ctypes.c_void_p]
KERNEL32.LocalFree.restype = ctypes.c_void_p

def _blob(data: bytes):
    buffer = ctypes.create_string_buffer(data)
    return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer

def _crypt(data: bytes, protect: bool) -> bytes:
    source, keepalive = _blob(data)
    destination = DATA_BLOB()
    function = CRYPT32.CryptProtectData if protect else CRYPT32.CryptUnprotectData
    arguments = (ctypes.byref(source), "MD Pipeline SSH", None, None, None, 0, ctypes.byref(destination)) if protect else (ctypes.byref(source), None, None, None, None, 0, ctypes.byref(destination))
    if not function(*arguments):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(destination.pbData, destination.cbData)
    finally:
        KERNEL32.LocalFree(destination.pbData)

class CredentialStore:
    def __init__(self, directory: Path): self.directory = directory
    def _path(self, connection_id: str) -> Path:
        if not connection_id.isalnum(): raise ValueError("Identificador inválido.")
        return self.directory / f"{connection_id}.bin"
    def save(self, connection_id: str, password: str) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(connection_id); temporary = path.with_suffix(".tmp")
        temporary.write_bytes(_crypt(password.encode(), True)); temporary.replace(path)
    def load(self, connection_id: str) -> str | None:
        path = self._path(connection_id)
        return _crypt(path.read_bytes(), False).decode() if path.exists() else None
    def delete(self, connection_id: str) -> None: self._path(connection_id).unlink(missing_ok=True)
