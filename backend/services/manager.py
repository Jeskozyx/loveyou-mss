"""
ServiceManager: Registry & pengelola lifecycle independen seluruh layanan LoveYou.
Menegakkan allowlist supported_actions, graceful stop, serta rollback konfigurasi otomatis.
"""

from __future__ import annotations

import copy
import threading
from datetime import datetime, timezone
from typing import Any, Callable

try:
    from backend.schemas.service_schemas import validate_service_config
    from backend.services.base import ALLOWED_LIFECYCLE_ACTIONS, BaseService
except ImportError:
    from schemas.service_schemas import validate_service_config
    from services.base import ALLOWED_LIFECYCLE_ACTIONS, BaseService


class ServiceManager:
    """Pengelola terpusat seluruh modular service di sisi Windows Client."""

    def __init__(self):
        self._lock = threading.RLock()
        self._services: dict[str, BaseService] = {}
        self._status_callbacks: list[Callable[[str, dict[str, Any]], None]] = []

    def on_status_change(self, callback: Callable[[str, dict[str, Any]], None]) -> None:
        with self._lock:
            if callback not in self._status_callbacks:
                self._status_callbacks.append(callback)
                for svc in self._services.values():
                    svc.add_status_listener(callback)

    def register(self, service: BaseService) -> None:
        if not service.service_key:
            raise ValueError("Service harus mendeklarasikan service_key yang tidak kosong.")
        with self._lock:
            self._services[service.service_key] = service
            for cb in self._status_callbacks:
                service.add_status_listener(cb)

    def unregister(self, service_key: str) -> None:
        with self._lock:
            svc = self._services.pop(service_key, None)
        if svc is not None:
            try:
                svc.stop()
            except Exception:
                pass

    def get(self, service_key: str) -> BaseService | None:
        with self._lock:
            return self._services.get(service_key)

    def list_services(self) -> list[str]:
        with self._lock:
            return list(self._services.keys())

    def get_service_definitions(self) -> list[dict[str, Any]]:
        """Mengembalikan katalog metadata semua layanan terdaftar untuk enrollment & dashboard."""
        with self._lock:
            services = list(self._services.values())
        return [svc.get_metadata() for svc in services]

    def get_all_statuses(self) -> dict[str, dict[str, Any]]:
        """Mengembalikan status terkini setiap layanan secara terpisah."""
        with self._lock:
            items = list(self._services.items())
        return {key: svc.get_status() for key, svc in items}

    def execute_action(self, service_key: str, action: str) -> dict[str, Any]:
        """
        Mengeksekusi aksi lifecycle pada layanan tertentu dengan validasi allowlist ketat.
        Tidak pernah mengeksekusi aksi di luar ALLOWED_LIFECYCLE_ACTIONS dan service.supported_actions.
        """
        action_clean = (action or "").strip().lower()
        now_iso = datetime.now(timezone.utc).isoformat()

        if action_clean not in ALLOWED_LIFECYCLE_ACTIONS:
            return {
                "ok": False,
                "service_key": service_key,
                "action": action_clean,
                "status": "Unknown",
                "message": f"Aksi '{action}' tidak diizinkan (di luar allowlist lifecycle).",
                "timestamp": now_iso,
            }

        service = self.get(service_key)
        if service is None:
            return {
                "ok": False,
                "service_key": service_key,
                "action": action_clean,
                "status": "Unknown",
                "message": f"Service '{service_key}' tidak terdaftar pada ServiceManager.",
                "timestamp": now_iso,
            }

        if action_clean not in service.supported_actions:
            return {
                "ok": False,
                "service_key": service_key,
                "action": action_clean,
                "status": service.current_status,
                "message": f"Service '{service_key}' tidak mendukung aksi '{action_clean}'. Didukung: {service.supported_actions}.",
                "timestamp": now_iso,
            }

        handler = getattr(service, action_clean)
        try:
            return handler()
        except Exception as exc:
            return {
                "ok": False,
                "service_key": service_key,
                "action": action_clean,
                "status": service.current_status,
                "message": f"Error saat mengeksekusi '{action_clean}' pada '{service_key}': {exc}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

    def apply_service_config(
        self,
        service_key: str,
        config: dict[str, Any],
        config_version: int,
    ) -> dict[str, Any]:
        """
        Memvalidasi skema konfigurasi runtime dan menerapkannya pada service terkait.
        Jika penerapan gagal, otomatis melakukan rollback ke konfigurasi & versi sebelumnya.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        service = self.get(service_key)
        if service is None:
            return {
                "ok": False,
                "service_key": service_key,
                "action": "apply_config",
                "status": "Unknown",
                "message": f"Service '{service_key}' tidak ditemukan.",
                "timestamp": now_iso,
            }

        if not isinstance(config_version, int) or config_version < 0:
            return {
                "ok": False,
                "service_key": service_key,
                "action": "apply_config",
                "status": service.current_status,
                "message": "config_version harus berupa bilangan bulat non-negatif.",
                "timestamp": now_iso,
            }

        previous_config = copy.deepcopy(service.config)
        previous_version = service.active_config_version

        is_valid, normalized, err_msg = validate_service_config(
            service_key=service_key,
            config=config,
            base_config=previous_config,
        )
        if not is_valid or normalized is None:
            return {
                "ok": False,
                "service_key": service_key,
                "action": "apply_config",
                "status": service.current_status,
                "active_config_version": previous_version,
                "rolled_back": False,
                "message": f"Validasi skema konfigurasi gagal: {err_msg}",
                "timestamp": now_iso,
            }

        try:
            result = service.apply_config(normalized, config_version)
            if not result.get("ok", False):
                # Rollback otomatis ke snapshot konfigurasi sebelumnya
                try:
                    service.apply_config(previous_config, previous_version)
                except Exception:
                    service.config = previous_config
                    service.active_config_version = previous_version
                result["rolled_back"] = True
                result["active_config_version"] = service.active_config_version
            return result
        except Exception as exc:
            # Rollback otomatis jika exception muncul saat apply_config
            try:
                service.apply_config(previous_config, previous_version)
            except Exception:
                service.config = previous_config
                service.active_config_version = previous_version
            return {
                "ok": False,
                "service_key": service_key,
                "action": "apply_config",
                "status": service.current_status,
                "active_config_version": service.active_config_version,
                "rolled_back": True,
                "message": f"Penerapan konfigurasi gagal dan di-rollback otomatis: {exc}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

    def start_all(self, service_keys: list[str] | None = None) -> dict[str, dict[str, Any]]:
        keys = service_keys if service_keys is not None else self.list_services()
        results: dict[str, dict[str, Any]] = {}
        for key in keys:
            svc = self.get(key)
            if svc and "start" in svc.supported_actions:
                results[key] = self.execute_action(key, "start")
        return results

    def stop_all(self, service_keys: list[str] | None = None) -> dict[str, dict[str, Any]]:
        """Menghentikan seluruh layanan secara normal (graceful stop) dalam urutan terbalik."""
        keys = service_keys if service_keys is not None else list(reversed(self.list_services()))
        results: dict[str, dict[str, Any]] = {}
        for key in keys:
            svc = self.get(key)
            if svc and "stop" in svc.supported_actions:
                results[key] = self.execute_action(key, "stop")
        return results

    def coordinate_capture_mode(self, policy_config: dict[str, Any]) -> dict[str, Any]:
        """
        Koordinator live switching sumber capture antara BrowserCaptureService dan ScreenshotService (MSS)
        tanpa restart aplikasi atau bentrok duplikasi tangkapan layar.
        """
        policy = policy_config.get("capture_policy") or {}
        bc = policy_config.get("browser_capture") or {}
        wc = policy_config.get("window_capture") or {}
        active_source = str(policy.get("active_source", "desktop_mss")).strip().lower()

        ss_svc = self.get("screenshot_service")
        bc_svc = self.get("browser_capture_service")

        if active_source == "browser_domain":
            if ss_svc is not None:
                if hasattr(ss_svc, "set_window_capture_policy"):
                    ss_svc.set_window_capture_policy(enabled=False, rules=[])
                if ss_svc.current_status == "Running":
                    self.execute_action("screenshot_service", "pause")
            if bc_svc is not None:
                if bool(bc.get("enabled", False)):
                    if bc_svc.current_status == "Paused":
                        self.execute_action("browser_capture_service", "resume")
                    elif bc_svc.current_status != "Running":
                        self.execute_action("browser_capture_service", "start")
                elif bc_svc.current_status == "Running":
                    self.execute_action("browser_capture_service", "pause")

        elif active_source == "application_window":
            if bc_svc is not None and bc_svc.current_status == "Running":
                self.execute_action("browser_capture_service", "pause")
            if ss_svc is not None:
                if hasattr(ss_svc, "set_window_capture_policy"):
                    ss_svc.set_window_capture_policy(
                        enabled=True,
                        rules=list(wc.get("rules", [])),
                        interval_seconds=wc.get("capture_interval_seconds"),
                    )
                if bool(wc.get("enabled", False)):
                    if ss_svc.current_status == "Paused":
                        self.execute_action("screenshot_service", "resume")
                    elif ss_svc.current_status != "Running":
                        self.execute_action("screenshot_service", "start")
                elif ss_svc.current_status == "Running":
                    self.execute_action("screenshot_service", "pause")

        elif active_source == "desktop_mss":
            if bc_svc is not None and bc_svc.current_status == "Running":
                self.execute_action("browser_capture_service", "pause")
            if ss_svc is not None:
                if hasattr(ss_svc, "set_window_capture_policy"):
                    ss_svc.set_window_capture_policy(enabled=False, rules=[])
                if ss_svc.current_status == "Paused":
                    self.execute_action("screenshot_service", "resume")
                elif ss_svc.current_status != "Running":
                    self.execute_action("screenshot_service", "start")

        elif active_source == "disabled":
            if bc_svc is not None and bc_svc.current_status == "Running":
                self.execute_action("browser_capture_service", "pause")
            if ss_svc is not None:
                if hasattr(ss_svc, "set_window_capture_policy"):
                    ss_svc.set_window_capture_policy(enabled=False, rules=[])
                if ss_svc.current_status == "Running":
                    self.execute_action("screenshot_service", "pause")

        return {
            "active_source": active_source,
            "screenshot_status": ss_svc.current_status if ss_svc else "Unknown",
            "browser_capture_status": bc_svc.current_status if bc_svc else "Unknown",
        }
