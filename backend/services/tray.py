"""
TrayService: Layanan modular pengelola ikon System Tray Windows (Pystray)
dengan dukungan tampilan minimalis (tooltip kosong), ikon kustom/Chrome,
dan kontrol lokal Pause/Stop/Restart/Uninstall.
"""

from __future__ import annotations

import ctypes
import threading
import time
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


class TrayService(BaseService):
    """Layanan modular untuk System Tray Windows."""

    service_key = "tray_service"
    display_name = "Tray Service"
    supported_actions = ["start", "stop", "restart"]
    configuration_schema = get_service_json_schema("tray_service")
    permission_requirements = ["system_tray"]

    def __init__(
        self,
        initial_config: dict[str, Any] | None = None,
        tray_factory: Callable[[dict[str, Any]], Any] | None = None,
        wait_for_taskbar: bool = False,
    ):
        defaults = {
            "icon": "heart.ico",
            "app_name": "LoveYou",
            "minimalist_tooltip": True,
        }
        if initial_config:
            defaults.update({k: v for k, v in initial_config.items() if k in defaults})
        super().__init__(initial_config=defaults)

        self._tray_factory = tray_factory
        self._wait_for_taskbar = wait_for_taskbar
        self._tray_icon: Any = None
        self._stop_event = threading.Event()

    @property
    def tray_icon(self) -> Any:
        return self._tray_icon

    def _start_icon_instance(self) -> None:
        if self._wait_for_taskbar:
            try:
                user32 = ctypes.windll.user32
                for _ in range(30):
                    if self._stop_event.is_set():
                        return
                    if user32.FindWindowW("Shell_TrayWnd", None):
                        break
                    time.sleep(0.5)
            except Exception:
                time.sleep(1.0)

        if self._stop_event.is_set():
            return

        if self._tray_factory is None:
            self._set_status(STATUS_RUNNING)
            return

        icon = self._tray_factory(dict(self.config))
        with self._lock:
            self._tray_icon = icon
        try:
            if hasattr(icon, "run_detached"):
                icon.run_detached()
            self._set_status(STATUS_RUNNING)
        except Exception as exc:
            self._set_status(STATUS_FAILED, error=str(exc))

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self.current_status == STATUS_RUNNING and self._tray_icon is not None:
                return self._build_response(True, "start", "TrayService sudah aktif.")
            self._stop_event.clear()

        if self._wait_for_taskbar:
            threading.Thread(target=self._start_icon_instance, daemon=True, name="TrayServiceStarter").start()
            self._set_status(STATUS_RUNNING)
            return self._build_response(True, "start", "TrayService dijadwalkan aktif setelah taskbar siap.")

        self._start_icon_instance()
        ok = self.current_status == STATUS_RUNNING
        return self._build_response(
            ok,
            "start",
            "TrayService berhasil dijalankan." if ok else f"TrayService gagal dijalankan: {self.last_error}",
        )

    def stop(self) -> dict[str, Any]:
        self._stop_event.set()
        with self._lock:
            icon = self._tray_icon
            self._tray_icon = None
        if icon is not None and hasattr(icon, "stop"):
            try:
                icon.stop()
            except Exception:
                pass
        self._set_status(STATUS_STOPPED)
        return self._build_response(True, "stop", "TrayService dihentikan secara normal.")

    def pause(self) -> dict[str, Any]:
        return self._build_response(False, "pause", "TrayService tidak mendukung aksi pause.")

    def resume(self) -> dict[str, Any]:
        return self._build_response(False, "resume", "TrayService tidak mendukung aksi resume.")

    def restart(self) -> dict[str, Any]:
        self.stop()
        res = self.start()
        res["action"] = "restart"
        res["message"] = "TrayService berhasil di-restart."
        return res

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "service_key": self.service_key,
                "display_name": self.display_name,
                "status": self.current_status,
                "active_config_version": self.active_config_version,
                "uptime_seconds": self.get_uptime_seconds(),
                "icon": self.config.get("icon", "heart.ico"),
                "minimalist_tooltip": self.config.get("minimalist_tooltip", True),
                "last_error": self.last_error,
                "updated_at": self.updated_at,
            }

    def apply_config(self, config: dict[str, Any], config_version: int) -> dict[str, Any]:
        is_valid, normalized, err = validate_service_config(self.service_key, config, base_config=self.config)
        if not is_valid or normalized is None:
            return self._build_response(False, "apply_config", f"Konfigurasi TrayService tidak valid: {err}")

        with self._lock:
            self.config = normalized
            self.active_config_version = config_version
            if self._tray_icon is not None and normalized.get("minimalist_tooltip", True):
                try:
                    self._tray_icon.title = " "
                except Exception:
                    pass
            self.updated_at = self._utc_now_iso()
        return self._build_response(True, "apply_config", f"Konfigurasi TrayService v{config_version} diterapkan.")
