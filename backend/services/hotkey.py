"""
HotkeyService: Layanan modular pengelola pintasan keyboard darurat (Global Hotkey Windows)
untuk menonaktifkan layanan, mencabut Windows Startup/Registry, dan membersihkan aplikasi
secara menyeluruh hingga ke akarnya, dengan dukungan perubahan keybind secara runtime.
"""

from __future__ import annotations

import ctypes
import threading
from typing import Any, Callable

try:
    from backend.schemas.service_schemas import get_service_json_schema, validate_service_config
    from backend.services.base import (
        STATUS_FAILED,
        STATUS_RUNNING,
        STATUS_STOPPED,
        BaseService,
    )
except ImportError:
    from schemas.service_schemas import get_service_json_schema, validate_service_config
    from services.base import (
        STATUS_FAILED,
        STATUS_RUNNING,
        STATUS_STOPPED,
        BaseService,
    )


def parse_hotkey_combo(combo_str: str) -> tuple[int, int]:
    """Mengurai kombinasi keybind (cth: 'Ctrl+Shift+F12') menjadi (fs_modifiers, vk_code)."""
    parts = [p.strip().upper() for p in (combo_str or "").replace(" ", "").split("+") if p.strip()]
    modifiers = 0
    vk = None

    vk_map = {
        "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74, "F6": 0x75,
        "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
        "END": 0x23, "HOME": 0x24, "DELETE": 0x2E, "DEL": 0x2E, "ESC": 0x1B, "ESCAPE": 0x1B,
        "SPACE": 0x20, "TAB": 0x09, "BACKSPACE": 0x08,
    }

    for p in parts:
        if p in ("CTRL", "CONTROL"):
            modifiers |= 0x0002
        elif p in ("ALT", "MENU"):
            modifiers |= 0x0001
        elif p == "SHIFT":
            modifiers |= 0x0004
        elif p in ("WIN", "WINDOWS", "SUPER"):
            modifiers |= 0x0008
        elif p in vk_map:
            vk = vk_map[p]
        elif len(p) == 1 and (p.isalpha() or p.isdigit()):
            vk = ord(p)

    if not modifiers:
        modifiers = 0x0002 | 0x0004
    if not vk:
        vk = 0x7B

    return modifiers, vk


class HotkeyService(BaseService):
    """Layanan modular untuk global emergency-uninstall hotkey Windows yang dapat diubah live."""

    service_key = "hotkey_service"
    display_name = "Emergency Hotkey"
    supported_actions = ["start", "stop", "restart"]
    configuration_schema = get_service_json_schema("hotkey_service")
    permission_requirements = ["global_hotkey", "local_uninstall_control"]

    def __init__(
        self,
        initial_config: dict[str, Any] | None = None,
        on_trigger: Callable[[dict[str, Any]], None] | None = None,
        on_config_updated: Callable[[dict[str, Any]], None] | None = None,
        register_loop_fn: Callable[[dict[str, Any], threading.Event, Callable[[], None]], None] | None = None,
        use_win32_listener: bool = True,
    ):
        defaults: dict[str, Any] = {
            "enabled": True,
            "hotkey_combo": "Ctrl+Shift+F12",
            "keep_backup_hotkey": True,
            "clean_local_screenshots": True,
            "unregister_startup": True,
        }
        if initial_config:
            defaults.update({k: v for k, v in initial_config.items() if k in defaults})
        super().__init__(initial_config=defaults)

        self._on_trigger = on_trigger
        self._on_config_updated = on_config_updated
        self._register_loop_fn = register_loop_fn
        self._use_win32_listener = use_win32_listener

        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._win32_thread_id: int | None = None
        self.trigger_count: int = 0

    def _fire_trigger(self) -> None:
        with self._lock:
            self.trigger_count += 1
            cfg_snapshot = dict(self.config)
        if self._on_trigger is not None:
            self._on_trigger(cfg_snapshot)

    def _run_win32_listener(self, cfg: dict[str, Any], stop_event: threading.Event) -> None:
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        with self._lock:
            self._win32_thread_id = int(kernel32.GetCurrentThreadId())

        combo_str = str(cfg.get("hotkey_combo", "Ctrl+Shift+F12"))
        keep_backup = bool(cfg.get("keep_backup_hotkey", True))
        modifiers, vk = parse_hotkey_combo(combo_str)

        HOTKEY_ID_CUSTOM = 0x5841
        HOTKEY_ID_BACKUP = 0x5842
        WM_HOTKEY = 0x0312

        reg_custom = False
        reg_backup = False
        try:
            reg_custom = bool(user32.RegisterHotKey(None, HOTKEY_ID_CUSTOM, modifiers | 0x4000, vk))
            if not reg_custom:
                reg_custom = bool(user32.RegisterHotKey(None, HOTKEY_ID_CUSTOM, modifiers, vk))

            if keep_backup and (modifiers, vk) != (0x0006, 0x7B):
                reg_backup = bool(user32.RegisterHotKey(None, HOTKEY_ID_BACKUP, 0x0006 | 0x4000, 0x7B))
                if not reg_backup:
                    reg_backup = bool(user32.RegisterHotKey(None, HOTKEY_ID_BACKUP, 0x0006, 0x7B))

            print(f"[HOTKEY] Pintasan darurat aktif: [{combo_str}] untuk nonaktifkan & bersihkan total.")

            msg = wintypes.MSG()
            while not stop_event.is_set() and user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY and msg.wParam in (HOTKEY_ID_CUSTOM, HOTKEY_ID_BACKUP):
                    print(f"\n[HOTKEY TRIGGERED] Pintasan darurat [{combo_str}] terdeteksi! Memulai pembersihan...")
                    if reg_custom:
                        user32.UnregisterHotKey(None, HOTKEY_ID_CUSTOM)
                        reg_custom = False
                    if reg_backup:
                        user32.UnregisterHotKey(None, HOTKEY_ID_BACKUP)
                        reg_backup = False
                    self._fire_trigger()
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:
            print(f"[WARN] Emergency hotkey listener error: {exc}")
        finally:
            try:
                if reg_custom:
                    user32.UnregisterHotKey(None, HOTKEY_ID_CUSTOM)
                if reg_backup:
                    user32.UnregisterHotKey(None, HOTKEY_ID_BACKUP)
            except Exception:
                pass
            with self._lock:
                self._win32_thread_id = None

    def _worker_entry(self, cfg: dict[str, Any], stop_event: threading.Event) -> None:
        if self._register_loop_fn is not None:
            self._register_loop_fn(cfg, stop_event, self._fire_trigger)
        elif self._use_win32_listener:
            self._run_win32_listener(cfg, stop_event)

    def start(self) -> dict[str, Any]:
        with self._lock:
            if not self.config.get("enabled", True):
                self._set_status(STATUS_STOPPED)
                return self._build_response(True, "start", "HotkeyService dalam kondisi nonaktif (enabled=false).")
            if self.current_status == STATUS_RUNNING and self._worker is not None and self._worker.is_alive():
                return self._build_response(True, "start", "HotkeyService sudah aktif.")

            stop_ev = threading.Event()
            self._stop_event = stop_ev
            cfg_copy = dict(self.config)

            if self._register_loop_fn is None and not self._use_win32_listener:
                self._set_status(STATUS_RUNNING)
                return self._build_response(True, "start", f"HotkeyService aktif ({cfg_copy['hotkey_combo']}).")

            worker = threading.Thread(
                target=self._worker_entry,
                args=(cfg_copy, stop_ev),
                daemon=True,
                name="EmergencyHotkeyService",
            )
            self._worker = worker
            try:
                worker.start()
                self._set_status(STATUS_RUNNING)
                return self._build_response(True, "start", f"HotkeyService aktif ({cfg_copy['hotkey_combo']}).")
            except Exception as exc:
                self._worker = None
                self._set_status(STATUS_FAILED, error=str(exc))
                return self._build_response(False, "start", f"HotkeyService gagal dijalankan: {exc}")

    def stop(self) -> dict[str, Any]:
        with self._lock:
            stop_ev = self._stop_event
            worker = self._worker
            tid = self._win32_thread_id
            self._worker = None
            self._win32_thread_id = None

        stop_ev.set()

        if tid is not None:
            try:
                WM_QUIT = 0x0012
                ctypes.windll.user32.PostThreadMessageW(tid, WM_QUIT, 0, 0)
            except Exception:
                pass

        if worker is not None and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=2.0)

        self._set_status(STATUS_STOPPED)
        return self._build_response(True, "stop", "HotkeyService dihentikan secara normal.")

    def pause(self) -> dict[str, Any]:
        return self._build_response(False, "pause", "HotkeyService tidak mendukung aksi pause.")

    def resume(self) -> dict[str, Any]:
        return self._build_response(False, "resume", "HotkeyService tidak mendukung aksi resume.")

    def restart(self) -> dict[str, Any]:
        self.stop()
        res = self.start()
        res["action"] = "restart"
        res["message"] = f"HotkeyService berhasil di-restart ({self.config.get('hotkey_combo')})."
        return res

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "service_key": self.service_key,
                "display_name": self.display_name,
                "status": self.current_status,
                "active_config_version": self.active_config_version,
                "uptime_seconds": self.get_uptime_seconds(),
                "enabled": bool(self.config.get("enabled", True)),
                "hotkey_combo": self.config.get("hotkey_combo", "Ctrl+Shift+F12"),
                "keep_backup_hotkey": bool(self.config.get("keep_backup_hotkey", True)),
                "clean_local_screenshots": bool(self.config.get("clean_local_screenshots", True)),
                "unregister_startup": bool(self.config.get("unregister_startup", True)),
                "trigger_count": self.trigger_count,
                "last_error": self.last_error,
                "updated_at": self.updated_at,
            }

    def apply_config(self, config: dict[str, Any], config_version: int) -> dict[str, Any]:
        is_valid, normalized, err = validate_service_config(self.service_key, config, base_config=self.config)
        if not is_valid or normalized is None:
            return self._build_response(False, "apply_config", f"Konfigurasi HotkeyService tidak valid: {err}")

        with self._lock:
            prev_enabled = bool(self.config.get("enabled", True))
            prev_combo = self.config.get("hotkey_combo")
            prev_backup = bool(self.config.get("keep_backup_hotkey", True))
            was_running = self.current_status == STATUS_RUNNING

            self.config = normalized
            self.active_config_version = config_version
            self.updated_at = self._utc_now_iso()

        if self._on_config_updated is not None:
            self._on_config_updated(dict(normalized))

        new_enabled = bool(normalized.get("enabled", True))
        binding_changed = (
            prev_combo != normalized.get("hotkey_combo")
            or prev_backup != bool(normalized.get("keep_backup_hotkey", True))
            or prev_enabled != new_enabled
        )

        # Terapkan perubahan keybind secara langsung pada runtime tanpa restart EXE
        if not new_enabled and was_running:
            self.stop()
        elif new_enabled and (binding_changed or not was_running):
            if was_running:
                self.restart()
            else:
                self.start()

        return self._build_response(
            True,
            "apply_config",
            f"Konfigurasi HotkeyService v{config_version} diterapkan ({normalized.get('hotkey_combo')}).",
        )
