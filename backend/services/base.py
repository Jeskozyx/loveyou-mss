"""
Kontrak antarmuka lifecycle standar (BaseService) untuk seluruh layanan modular LoveYou.
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Callable

# Status standar UI sesuai blueprint Xoose-LoveYou
STATUS_RUNNING = "Running"
STATUS_STOPPED = "Stopped"
STATUS_PAUSED = "Paused"
STATUS_FAILED = "Failed"
STATUS_UNKNOWN = "Unknown"

ALLOWED_LIFECYCLE_ACTIONS = frozenset({"start", "stop", "pause", "resume", "restart"})


class BaseService(ABC):
    """
    Interface abstrak yang wajib diimplementasikan oleh setiap layanan modular
    di dalam Service Manager (Screenshot, Tray, Sync, Notification, UI, Game, Custom).
    """

    service_key: str = ""
    display_name: str = ""
    supported_actions: list[str] = []
    configuration_schema: dict[str, Any] = {}
    permission_requirements: list[str] = []

    def __init__(self, initial_config: dict[str, Any] | None = None):
        self._lock = threading.RLock()
        self.current_status: str = STATUS_STOPPED
        self.active_config_version: int = 0
        self.config: dict[str, Any] = dict(initial_config or {})
        self.last_error: str | None = None
        self.started_at: float | None = None
        self.updated_at: str = self._utc_now_iso()
        self._status_listeners: list[Callable[[str, dict[str, Any]], None]] = []

    @staticmethod
    def _utc_now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def add_status_listener(self, listener: Callable[[str, dict[str, Any]], None]) -> None:
        with self._lock:
            if listener not in self._status_listeners:
                self._status_listeners.append(listener)

    def _set_status(self, new_status: str, error: str | None = None) -> None:
        with self._lock:
            changed = (self.current_status != new_status) or (self.last_error != error)
            self.current_status = new_status
            self.last_error = error
            self.updated_at = self._utc_now_iso()
            if new_status == STATUS_RUNNING and self.started_at is None:
                self.started_at = time.time()
            elif new_status in (STATUS_STOPPED, STATUS_FAILED):
                self.started_at = None
            listeners = list(self._status_listeners)
            snapshot = self.get_status() if changed else None

        if changed and snapshot is not None:
            for cb in listeners:
                try:
                    cb(self.service_key, snapshot)
                except Exception:
                    pass

    def _build_response(self, ok: bool, action: str, message: str = "", extra: dict[str, Any] | None = None) -> dict[str, Any]:
        res: dict[str, Any] = {
            "ok": ok,
            "service_key": self.service_key,
            "action": action,
            "status": self.current_status,
            "active_config_version": self.active_config_version,
            "message": message,
            "timestamp": self._utc_now_iso(),
        }
        if extra:
            res.update(extra)
        return res

    def get_metadata(self) -> dict[str, Any]:
        """Metadata layanan untuk registrasi ke ServiceDefinition / Dashboard Xoose."""
        return {
            "service_key": self.service_key,
            "display_name": self.display_name,
            "supported_actions": list(self.supported_actions),
            "current_status": self.current_status,
            "active_config_version": self.active_config_version,
            "configuration_schema": self.configuration_schema,
            "permission_requirements": list(self.permission_requirements),
        }

    def get_uptime_seconds(self) -> float:
        with self._lock:
            if self.started_at is None or self.current_status == STATUS_STOPPED:
                return 0.0
            return round(max(0.0, time.time() - self.started_at), 2)

    @abstractmethod
    def start(self) -> dict[str, Any]:
        ...

    @abstractmethod
    def stop(self) -> dict[str, Any]:
        ...

    @abstractmethod
    def pause(self) -> dict[str, Any]:
        ...

    @abstractmethod
    def resume(self) -> dict[str, Any]:
        ...

    @abstractmethod
    def restart(self) -> dict[str, Any]:
        ...

    @abstractmethod
    def get_status(self) -> dict[str, Any]:
        ...

    @abstractmethod
    def apply_config(self, config: dict[str, Any], config_version: int) -> dict[str, Any]:
        ...
