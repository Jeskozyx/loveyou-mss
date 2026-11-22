"""
GameService: Fondasi modular untuk ekstensi pengembangan game pada ekosistem Xoose-LoveYou.
Mendukung start, stop, pause, resume, restart, dan apply_config secara runtime.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

try:
    from backend.schemas.service_schemas import get_service_json_schema, validate_service_config
    from backend.services.base import (
        STATUS_PAUSED,
        STATUS_RUNNING,
        STATUS_STOPPED,
        BaseService,
    )
except ImportError:
    from schemas.service_schemas import get_service_json_schema, validate_service_config
    from services.base import (
        STATUS_PAUSED,
        STATUS_RUNNING,
        STATUS_STOPPED,
        BaseService,
    )


class GameService(BaseService):
    """Layanan modular fondasi untuk modul game dengan tick loop yang dapat dikontrol remote."""

    service_key = "game_service"
    display_name = "Game Service"
    supported_actions = ["start", "stop", "pause", "resume", "restart"]
    configuration_schema = get_service_json_schema("game_service")
    permission_requirements = ["game_runtime"]

    def __init__(
        self,
        initial_config: dict[str, Any] | None = None,
        tick_handler: Callable[[dict[str, Any]], None] | None = None,
    ):
        defaults = {
            "enabled": False,
            "game_mode": "idle",
            "tick_rate_hz": 1,
            "auto_save_interval_seconds": 60,
            "custom_params": {},
        }
        if initial_config:
            defaults.update({k: v for k, v in initial_config.items() if k in defaults})
        super().__init__(initial_config=defaults)

        self._tick_handler = tick_handler
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._worker: threading.Thread | None = None
        self.ticks_executed: int = 0

    def register_tick_handler(self, handler: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            self._tick_handler = handler

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            if not self._pause_event.is_set():
                with self._lock:
                    handler = self._tick_handler
                    cfg_snapshot = dict(self.config)
                try:
                    if handler is not None:
                        handler(cfg_snapshot)
                    with self._lock:
                        self.ticks_executed += 1
                except Exception as exc:
                    self.last_error = str(exc)

            hz = max(1, int(self.config.get("tick_rate_hz", 1)))
            self._stop_event.wait(timeout=1.0 / hz)

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self.current_status == STATUS_RUNNING and self._worker and self._worker.is_alive():
                return self._build_response(True, "start", "GameService sudah berjalan.")
            self._stop_event.clear()
            self._pause_event.clear()
            self._worker = threading.Thread(target=self._worker_loop, daemon=True, name="GameServiceWorker")
            self._worker.start()
            self._set_status(STATUS_RUNNING)
            return self._build_response(True, "start", "GameService berhasil dijalankan.")

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._stop_event.set()
            self._pause_event.clear()
            worker = self._worker
            self._worker = None
        if worker and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=2.0)
        self._set_status(STATUS_STOPPED)
        return self._build_response(True, "stop", "GameService dihentikan secara normal.")

    def pause(self) -> dict[str, Any]:
        with self._lock:
            if self.current_status == STATUS_STOPPED:
                return self._build_response(False, "pause", "GameService sedang berhenti.")
            self._pause_event.set()
            self._set_status(STATUS_PAUSED)
            return self._build_response(True, "pause", "GameService dijeda.")

    def resume(self) -> dict[str, Any]:
        with self._lock:
            if self.current_status == STATUS_STOPPED:
                return self.start()
            self._pause_event.clear()
            self._set_status(STATUS_RUNNING)
            return self._build_response(True, "resume", "GameService dilanjutkan.")

    def restart(self) -> dict[str, Any]:
        self.stop()
        res = self.start()
        res["action"] = "restart"
        res["message"] = "GameService berhasil di-restart."
        return res

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "service_key": self.service_key,
                "display_name": self.display_name,
                "status": self.current_status,
                "active_config_version": self.active_config_version,
                "uptime_seconds": self.get_uptime_seconds(),
                "game_mode": self.config.get("game_mode", "idle"),
                "tick_rate_hz": self.config.get("tick_rate_hz", 1),
                "ticks_executed": self.ticks_executed,
                "last_error": self.last_error,
                "updated_at": self.updated_at,
            }

    def apply_config(self, config: dict[str, Any], config_version: int) -> dict[str, Any]:
        is_valid, normalized, err = validate_service_config(self.service_key, config, base_config=self.config)
        if not is_valid or normalized is None:
            return self._build_response(False, "apply_config", f"Konfigurasi GameService tidak valid: {err}")

        with self._lock:
            self.config = normalized
            self.active_config_version = config_version
            self.updated_at = self._utc_now_iso()
        return self._build_response(True, "apply_config", f"Konfigurasi GameService v{config_version} diterapkan.")
