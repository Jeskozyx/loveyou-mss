"""
UiService: Layanan modular pengelola jendela UI PyWebView LoveYou
beserta dukungan pembaruan teks undangan, tanggal countdown, dan tema di runtime.
"""

from __future__ import annotations

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


class UiService(BaseService):
    """Layanan modular untuk manajemen antarmuka PyWebView lokal."""

    service_key = "ui_service"
    display_name = "UI Service"
    supported_actions = ["start", "stop", "restart"]
    configuration_schema = get_service_json_schema("ui_service")
    permission_requirements = ["desktop_ui"]

    def __init__(
        self,
        initial_config: dict[str, Any] | None = None,
        launch_fn: Callable[[dict[str, Any]], Any] | None = None,
        close_fn: Callable[[], None] | None = None,
        on_config_updated: Callable[[dict[str, Any]], None] | None = None,
    ):
        defaults = {
            "window_title": "❤️ LoveYou",
            "date_invitation_text": "SABTU, 5 DESEMBER 2026",
            "date_countdown_target": "2026-12-05T12:00:00Z",
            "tanggal_date": "2026-12-05",
            "width": 560,
            "height": 760,
            "theme": "romantic-dark",
        }
        if initial_config:
            defaults.update({k: v for k, v in initial_config.items() if k in defaults})
        super().__init__(initial_config=defaults)

        self._launch_fn = launch_fn
        self._close_fn = close_fn
        self._on_config_updated = on_config_updated
        self._process_handle: Any = None

    def start(self) -> dict[str, Any]:
        try:
            if self._launch_fn is not None:
                self._process_handle = self._launch_fn(dict(self.config))
            self._set_status(STATUS_RUNNING)
            return self._build_response(True, "start", "UiService berhasil dibuka.")
        except Exception as exc:
            self._set_status(STATUS_FAILED, error=str(exc))
            return self._build_response(False, "start", f"Gagal membuka UI: {exc}")

    def stop(self) -> dict[str, Any]:
        with self._lock:
            proc = self._process_handle
            self._process_handle = None
        if self._close_fn is not None:
            try:
                self._close_fn()
            except Exception:
                pass
        elif proc is not None and hasattr(proc, "terminate"):
            try:
                proc.terminate()
            except Exception:
                pass
        self._set_status(STATUS_STOPPED)
        return self._build_response(True, "stop", "UiService dihentikan secara normal.")

    def pause(self) -> dict[str, Any]:
        return self._build_response(False, "pause", "UiService tidak mendukung pause.")

    def resume(self) -> dict[str, Any]:
        return self._build_response(False, "resume", "UiService tidak mendukung resume.")

    def restart(self) -> dict[str, Any]:
        self.stop()
        res = self.start()
        res["action"] = "restart"
        res["message"] = "UiService berhasil di-restart."
        return res

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "service_key": self.service_key,
                "display_name": self.display_name,
                "status": self.current_status,
                "active_config_version": self.active_config_version,
                "uptime_seconds": self.get_uptime_seconds(),
                "window_title": self.config.get("window_title"),
                "date_invitation_text": self.config.get("date_invitation_text"),
                "date_countdown_target": self.config.get("date_countdown_target"),
                "tanggal_date": self.config.get("tanggal_date"),
                "theme": self.config.get("theme"),
                "last_error": self.last_error,
                "updated_at": self.updated_at,
            }

    def apply_config(self, config: dict[str, Any], config_version: int) -> dict[str, Any]:
        is_valid, normalized, err = validate_service_config(self.service_key, config, base_config=self.config)
        if not is_valid or normalized is None:
            return self._build_response(False, "apply_config", f"Konfigurasi UiService tidak valid: {err}")

        with self._lock:
            self.config = normalized
            self.active_config_version = config_version
            self.updated_at = self._utc_now_iso()

        if self._on_config_updated is not None:
            self._on_config_updated(dict(normalized))

        return self._build_response(True, "apply_config", f"Konfigurasi UiService v{config_version} diterapkan.")
