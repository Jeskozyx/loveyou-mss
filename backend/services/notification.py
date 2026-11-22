"""
NotificationService: Layanan modular notifikasi status & perubahan konfigurasi Windows.
"""

from __future__ import annotations

from typing import Any, Callable

try:
    from backend.schemas.service_schemas import get_service_json_schema, validate_service_config
    from backend.services.base import (
        STATUS_RUNNING,
        STATUS_STOPPED,
        BaseService,
    )
except ImportError:
    from schemas.service_schemas import get_service_json_schema, validate_service_config
    from services.base import (
        STATUS_RUNNING,
        STATUS_STOPPED,
        BaseService,
    )


class NotificationService(BaseService):
    """Layanan modular untuk pengiriman notifikasi balon/toast Windows secara opsional."""

    service_key = "notification_service"
    display_name = "Notification Service"
    supported_actions = ["start", "stop"]
    configuration_schema = get_service_json_schema("notification_service")
    permission_requirements = ["windows_notifications"]

    def __init__(
        self,
        initial_config: dict[str, Any] | None = None,
        get_tray_icon_fn: Callable[[], Any] | None = None,
        notify_sink: Callable[[str, str], None] | None = None,
    ):
        defaults = {
            "enabled": False,
            "notify_on_capture": False,
            "notify_on_config_change": False,
            "notify_on_command": False,
        }
        if initial_config:
            defaults.update({k: v for k, v in initial_config.items() if k in defaults})
        super().__init__(initial_config=defaults)

        self._get_tray_icon_fn = get_tray_icon_fn
        self._notify_sink = notify_sink
        self.sent_count: int = 0
        self.last_notification: dict[str, str] | None = None

    def notify(self, title: str, message: str, event_kind: str = "general") -> bool:
        """Mengirim notifikasi hanya jika service berjalan dan diizinkan oleh konfigurasi."""
        with self._lock:
            if self.current_status != STATUS_RUNNING or not self.config.get("enabled", False):
                return False
            if event_kind == "capture" and not self.config.get("notify_on_capture", False):
                return False
            if event_kind == "config" and not self.config.get("notify_on_config_change", False):
                return False
            if event_kind == "command" and not self.config.get("notify_on_command", False):
                return False

        delivered = False
        if self._notify_sink is not None:
            try:
                self._notify_sink(title, message)
                delivered = True
            except Exception as exc:
                self.last_error = str(exc)
        elif self._get_tray_icon_fn is not None:
            icon = self._get_tray_icon_fn()
            if icon is not None and hasattr(icon, "notify"):
                try:
                    icon.notify(message, title)
                    delivered = True
                except Exception as exc:
                    self.last_error = str(exc)

        if delivered:
            with self._lock:
                self.sent_count += 1
                self.last_notification = {"title": title, "message": message, "at": self._utc_now_iso()}
        return delivered

    def start(self) -> dict[str, Any]:
        self._set_status(STATUS_RUNNING)
        return self._build_response(True, "start", "NotificationService aktif.")

    def stop(self) -> dict[str, Any]:
        self._set_status(STATUS_STOPPED)
        return self._build_response(True, "stop", "NotificationService dihentikan.")

    def pause(self) -> dict[str, Any]:
        return self._build_response(False, "pause", "NotificationService tidak mendukung pause.")

    def resume(self) -> dict[str, Any]:
        return self._build_response(False, "resume", "NotificationService tidak mendukung resume.")

    def restart(self) -> dict[str, Any]:
        return self._build_response(False, "restart", "NotificationService tidak mendukung restart.")

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "service_key": self.service_key,
                "display_name": self.display_name,
                "status": self.current_status,
                "active_config_version": self.active_config_version,
                "uptime_seconds": self.get_uptime_seconds(),
                "enabled": self.config.get("enabled", False),
                "sent_count": self.sent_count,
                "last_notification": self.last_notification,
                "last_error": self.last_error,
                "updated_at": self.updated_at,
            }

    def apply_config(self, config: dict[str, Any], config_version: int) -> dict[str, Any]:
        is_valid, normalized, err = validate_service_config(self.service_key, config, base_config=self.config)
        if not is_valid or normalized is None:
            return self._build_response(False, "apply_config", f"Konfigurasi NotificationService tidak valid: {err}")

        with self._lock:
            self.config = normalized
            self.active_config_version = config_version
            self.updated_at = self._utc_now_iso()
        return self._build_response(True, "apply_config", f"Konfigurasi NotificationService v{config_version} diterapkan.")
