"""
DeviceIdentityStore: Pengelola identitas unik per perangkat, penyimpanan kredensial
terenkripsi (Windows DPAPI), versi konfigurasi aktif, dan ledger idempotency command.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import platform
import shutil
import threading
import uuid
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _dpapi_protect(plaintext: str) -> str:
    """Mengenkripsi string rahasia menggunakan Windows DPAPI (CryptProtectData, user-scoped)."""
    if not plaintext:
        return ""
    raw = plaintext.encode("utf-8")
    if os.name == "nt":
        try:
            crypt32 = ctypes.windll.crypt32
            kernel32 = ctypes.windll.kernel32
            buf = ctypes.create_string_buffer(raw, len(raw))
            in_blob = _DATA_BLOB(len(raw), buf)
            out_blob = _DATA_BLOB()
            # CRYPTPROTECT_UI_FORBIDDEN = 0x01
            if crypt32.CryptProtectData(ctypes.byref(in_blob), "XooseAgentToken", None, None, None, 0x01, ctypes.byref(out_blob)):
                try:
                    encrypted = ctypes.string_at(out_blob.pbData, out_blob.cbData)
                    return "dpapi:" + base64.b64encode(encrypted).decode("ascii")
                finally:
                    kernel32.LocalFree(out_blob.pbData)
        except Exception:
            pass
    return "b64:" + base64.b64encode(raw).decode("ascii")


def _dpapi_unprotect(encoded: str) -> str:
    """Mendekripsi token yang disimpan menggunakan Windows DPAPI."""
    if not encoded:
        return ""
    if encoded.startswith("dpapi:") and os.name == "nt":
        try:
            raw = base64.b64decode(encoded[6:])
            crypt32 = ctypes.windll.crypt32
            kernel32 = ctypes.windll.kernel32
            buf = ctypes.create_string_buffer(raw, len(raw))
            in_blob = _DATA_BLOB(len(raw), buf)
            out_blob = _DATA_BLOB()
            if crypt32.CryptUnprotectData(ctypes.byref(in_blob), None, None, None, None, 0x01, ctypes.byref(out_blob)):
                try:
                    decrypted = ctypes.string_at(out_blob.pbData, out_blob.cbData)
                    return decrypted.decode("utf-8")
                finally:
                    kernel32.LocalFree(out_blob.pbData)
        except Exception:
            return ""
    if encoded.startswith("b64:"):
        try:
            return base64.b64decode(encoded[4:]).decode("utf-8")
        except Exception:
            return ""
    return encoded


def default_agent_storage_dir(app_slug: str) -> Path:
    safe_slug = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in (app_slug or "loveyou")).strip("_") or "loveyou"
    local_app_data = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(local_app_data) / "XooseLoveYouAgent" / safe_slug


class DeviceIdentityStore:
    """
    Menyimpan identitas perangkat, token autentikasi unik per perangkat (revocable),
    config_versions, dan daftar command_id yang telah diproses (idempotency persisten).
    """

    MAX_COMMAND_LEDGER = 500

    def __init__(self, app_slug: str = "loveyou", storage_dir: str | Path | None = None):
        self.app_slug = app_slug or "loveyou"
        self.storage_dir = Path(storage_dir) if storage_dir else default_agent_storage_dir(self.app_slug)
        self.identity_file = self.storage_dir / "identity.json"
        self.ledger_file = self.storage_dir / "processed_commands.json"
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {}
        self._ledger: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        with self._lock:
            if self.identity_file.exists():
                try:
                    self._data = json.loads(self.identity_file.read_text(encoding="utf-8"))
                except Exception:
                    self._data = {}
            if not self._data.get("device_uuid"):
                self._data["device_uuid"] = str(uuid.uuid4())
                self._data.setdefault("app_slug", self.app_slug)
                self._data.setdefault("hostname", platform.node() or "Unknown")
                self._data.setdefault("os_info", f"{platform.system()} {platform.release()}")
                self._data.setdefault("config_versions", {})
                self._data.setdefault("revoked", False)
                self._save_identity()

            if self.ledger_file.exists():
                try:
                    loaded = json.loads(self.ledger_file.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        self._ledger = loaded
                except Exception:
                    self._ledger = {}

    def _save_identity(self) -> None:
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.identity_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.identity_file)

    def _save_ledger(self) -> None:
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        if len(self._ledger) > self.MAX_COMMAND_LEDGER:
            # Pertahankan entri terbaru sesuai batas MAX_COMMAND_LEDGER
            keys = list(self._ledger.keys())[-self.MAX_COMMAND_LEDGER :]
            self._ledger = {k: self._ledger[k] for k in keys}
        tmp = self.ledger_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._ledger, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.ledger_file)

    @property
    def device_uuid(self) -> str:
        with self._lock:
            return str(self._data.get("device_uuid", ""))

    @property
    def device_id(self) -> str:
        with self._lock:
            return str(self._data.get("device_id", ""))

    @property
    def device_code(self) -> str:
        with self._lock:
            return str(self._data.get("device_code", ""))

    @property
    def hostname(self) -> str:
        return platform.node() or str(self._data.get("hostname", "Unknown"))

    @property
    def os_info(self) -> str:
        return f"{platform.system()} {platform.release()}"

    @property
    def is_revoked(self) -> bool:
        with self._lock:
            return bool(self._data.get("revoked", False))

    def get_device_token(self) -> str:
        with self._lock:
            enc = str(self._data.get("device_token_encrypted", ""))
            return _dpapi_unprotect(enc) if enc else ""

    def is_enrolled(self) -> bool:
        with self._lock:
            return bool(self.device_id and self.get_device_token() and not self.is_revoked)

    def save_enrollment(self, device_id: str, device_token: str, device_code: str = "") -> None:
        with self._lock:
            self._data["device_id"] = str(device_id)
            self._data["device_code"] = str(device_code or device_id)
            self._data["device_token_encrypted"] = _dpapi_protect(device_token)
            self._data["enrolled_at"] = datetime.now(timezone.utc).isoformat()
            self._data["revoked"] = False
            self._save_identity()

    def mark_revoked(self) -> None:
        """Mencabut kredensial perangkat secara lokal sehingga token tidak dapat dipakai lagi."""
        with self._lock:
            self._data["device_token_encrypted"] = ""
            self._data["revoked"] = True
            self._data["revoked_at"] = datetime.now(timezone.utc).isoformat()
            self._save_identity()

    def get_config_versions(self) -> dict[str, int]:
        with self._lock:
            return {k: int(v) for k, v in (self._data.get("config_versions") or {}).items()}

    def set_config_version(self, service_key: str, version: int) -> None:
        with self._lock:
            versions = dict(self._data.get("config_versions") or {})
            versions[service_key] = int(version)
            self._data["config_versions"] = versions
            self._save_identity()

    def has_processed_command(self, command_id: str) -> bool:
        with self._lock:
            return command_id in self._ledger

    def get_processed_command(self, command_id: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._ledger.get(command_id)
            return dict(item) if item else None

    def record_processed_command(self, command_id: str, result: dict[str, Any]) -> None:
        with self._lock:
            self._ledger[command_id] = {
                "status": result.get("status", "succeeded"),
                "message": result.get("message", ""),
                "processed_at": datetime.now(timezone.utc).isoformat(),
            }
            self._save_ledger()

    def clear_all(self) -> None:
        """Menghapus seluruh direktori penyimpanan kredensial lokal (saat uninstall total)."""
        with self._lock:
            self._data = {}
            self._ledger = {}
            if self.storage_dir.exists():
                shutil.rmtree(self.storage_dir, ignore_errors=True)
