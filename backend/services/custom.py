"""
CustomService: Template plugin ekstensi modular yang dapat diperluas
tanpa mengubah inti ServiceManager atau Agent.
"""

from __future__ import annotations

from typing import Any, Callable

try:
    from backend.schemas.service_schemas import get_service_json_schema, validate_service_config
    from backend.services.base import (
        STATUS_FAILED,
        STATUS_PAUSED,
        STATUS_RUNNING,
        STATUS_STOPPED,
        BaseService,
    )
except ImportError:
    from schemas.service_schemas import get_service_json_schema, validate_service_config
    from services.base import (
        STATUS_FAILED,
        STATUS_PAUSED,
        STATUS_RUNNING,
        STATUS_STOPPED,
        BaseService,
    )


class CustomService(BaseService):
    """Template plugin layanan kustom yang mengikuti kontrak BaseService."""

    service_key = "custom_service"
    display_name = "Custom Service"
    supported_actions = ["start", "stop", "pause", "resume", "restart"]
    configuration_schema = get_service_json_schema("custom_service")
    permission_requirements: list[str] = []

    def __init__(
        self,
        initial_config: dict[str, Any] | None = None,
        on_start: Callable[[dict[str, Any]], None] | None = None,
        on_stop: Callable[[], None] | None = None,
        on_config: Callable[[dict[str, Any]], None] | None = None,
    ):
        defaults = {
            "enabled": False,
            "plugin_name": "default_plugin",
            "parameters": {},
        }
        if initial_config:
            defaults.update({k: v for k, v in initial_config.items() if k in defaults})
        super().__init__(initial_config=defaults)

        self._on_start = on_start
        self._on_stop = on_stop
        self._on_config = on_config

    def start(self) -> dict[str, Any]:
        try:
            if self._on_start:
                self._on_start(dict(self.config))
            self._set_status(STATUS_RUNNING)
            return self._build_response(True, "start", "CustomService dijalankan.")
        except Exception as exc:
            self._set_status(STATUS_FAILED, error=str(exc))
            return self._build_response(False, "start", f"CustomService gagal start: {exc}")

    def stop(self) -> dict[str, Any]:
        if self._on_stop:
            try:
                self._on_stop()
            except Exception:
                pass
        self._set_status(STATUS_STOPPED)
        return self._build_response(True, "stop", "CustomService dihentikan secara normal.")

    def pause(self) -> dict[str, Any]:
        if self.current_status == STATUS_STOPPED:
            return self._build_response(False, "pause", "CustomService sedang berhenti.")
        self._set_status(STATUS_PAUSED)
        return self._build_response(True, "pause", "CustomService dijeda.")

    def resume(self) -> dict[str, Any]:
        if self.current_status == STATUS_STOPPED:
            return self.start()
        self._set_status(STATUS_RUNNING)
        return self._build_response(True, "resume", "CustomService dilanjutkan.")

    def restart(self) -> dict[str, Any]:
        self.stop()
        res = self.start()
        res["action"] = "restart"
        res["message"] = "CustomService di-restart."
        return res

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "service_key": self.service_key,
                "display_name": self.display_name,
                "status": self.current_status,
                "active_config_version": self.active_config_version,
                "uptime_seconds": self.get_uptime_seconds(),
                "plugin_name": self.config.get("plugin_name", "default_plugin"),
                "parameters": dict(self.config.get("parameters", {})),
                "last_error": self.last_error,
                "updated_at": self.updated_at,
            }

    def apply_config(self, config: dict[str, Any], config_version: int) -> dict[str, Any]:
        is_valid, normalized, err = validate_service_config(self.service_key, config, base_config=self.config)
        if not is_valid or normalized is None:
            return self._build_response(False, "apply_config", f"Konfigurasi CustomService tidak valid: {err}")

        if self._on_config:
            self._on_config(dict(normalized))

        with self._lock:
            self.config = normalized
            self.active_config_version = config_version
            self.updated_at = self._utc_now_iso()
        return self._build_response(True, "apply_config", f"Konfigurasi CustomService v{config_version} diterapkan.")
