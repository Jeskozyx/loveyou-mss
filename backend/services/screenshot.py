"""
ScreenshotService: Layanan modular penangkap layar menggunakan MSS + Pillow
dengan kontrol lifecycle penuh (start/stop/pause/resume/restart/apply_config).
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from datetime import datetime
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


class ScreenshotService(BaseService):
    """Layanan modular pengambilan tangkapan layar periodik berbasis MSS."""

    service_key = "screenshot_service"
    display_name = "Screenshot Service"
    supported_actions = ["start", "stop", "pause", "resume", "restart"]
    configuration_schema = get_service_json_schema("screenshot_service")
    permission_requirements = ["screen_capture", "local_pause_stop_control"]

    def __init__(
        self,
        initial_config: dict[str, Any] | None = None,
        capture_fn: Callable[[str, str, int], tuple[str, str, str]] | None = None,
        active_window_fn: Callable[[], str] | None = None,
        active_process_fn: Callable[[], str] | None = None,
        get_user_fn: Callable[[], str] | None = None,
        on_captured: Callable[[dict[str, Any]], None] | None = None,
        on_notify: Callable[[str, str], None] | None = None,
    ):
        defaults = {
            "interval_seconds": 3,
            "image_format": "webp",
            "jpg_quality": 75,
            "keep_local_file": False,
            "local_folder": "screenshots",
        }
        if initial_config:
            defaults.update({k: v for k, v in initial_config.items() if k in defaults})
        super().__init__(initial_config=defaults)

        self._capture_fn = capture_fn
        self._active_window_fn = active_window_fn or (lambda: "")
        self._active_process_fn = active_process_fn or (lambda: "")
        self._get_user_fn = get_user_fn or (lambda: "anonymous")
        self._on_captured = on_captured
        self._on_notify = on_notify

        # Mode filter jendela aplikasi (diatur secara live via CaptureModeCoordinator)
        self.window_filter_enabled: bool = False
        self.window_rules: list[dict[str, Any]] = []
        self._window_interval_override: int | None = None

        self._stop_event = threading.Event()
        self._pause_event = threading.Event()  # set = paused
        self._worker: threading.Thread | None = None
        self.total_captured: int = 0
        self.last_capture_at: str | None = None

    def set_window_capture_policy(
        self,
        enabled: bool,
        rules: list[dict[str, Any]] | None = None,
        interval_seconds: int | None = None,
    ) -> None:
        """Mengaktifkan/menonaktifkan filter jendela aktif untuk mode application_window tanpa merusak MSS."""
        with self._lock:
            self.window_filter_enabled = bool(enabled)
            self.window_rules = list(rules or [])
            self._window_interval_override = int(interval_seconds) if (enabled and interval_seconds) else None

    def _match_active_window_rule(self, active_window: str, active_process: str) -> dict[str, Any] | None:
        proc_lower = (active_process or "").strip().lower()
        title_lower = (active_window or "").strip().lower()
        for rule in self.window_rules:
            if not isinstance(rule, dict) or not rule.get("enabled"):
                continue
            target_proc = str(rule.get("process", "")).strip().lower()
            title_substr = str(rule.get("window_title_contains", "")).strip().lower()
            if target_proc and proc_lower != target_proc:
                continue
            if title_substr and title_substr not in title_lower:
                continue
            return rule
        return None

    def _resolve_output_dir(self, active_user: str) -> str:
        keep_local = bool(self.config.get("keep_local_file", False))
        if not keep_local:
            base = os.path.join(tempfile.gettempdir(), "_xoose_temp_capture")
        else:
            base = str(self.config.get("local_folder", "screenshots"))
        return os.path.join(base, active_user) if active_user and active_user != "anonymous" else base

    def capture_once(self) -> dict[str, Any] | None:
        """Menangkap satu screenshot secara langsung dan mengirimkannya ke callback antrean."""
        if self._capture_fn is None:
            raise RuntimeError("Fungsi capture_fn belum diinisialisasi pada ScreenshotService.")

        active_user = (self._get_user_fn() or "anonymous").strip() or "anonymous"
        active_window = self._active_window_fn() or ""
        active_process = self._active_process_fn() or ""

        matched_window_rule = None
        with self._lock:
            filter_on = self.window_filter_enabled
        if filter_on:
            matched_window_rule = self._match_active_window_rule(active_window, active_process)
            if matched_window_rule is None:
                return None

        output_dir = self._resolve_output_dir(active_user)
        os.makedirs(output_dir, exist_ok=True)

        filepath, filename, resolution = self._capture_fn(
            output_dir,
            str(self.config.get("image_format", "webp")),
            int(self.config.get("jpg_quality", 75)),
        )
        file_size = os.path.getsize(filepath) if os.path.exists(filepath) else 0

        with self._lock:
            self.total_captured += 1
            self.last_capture_at = self._utc_now_iso()

        item = {
            "filepath": filepath,
            "filename": filename,
            "resolution": resolution,
            "active_window": active_window,
            "file_size": file_size,
            "user_name": active_user,
            "source": "application_window" if matched_window_rule else "desktop_mss",
            "rule_id": str(matched_window_rule.get("id", "")) if matched_window_rule else "",
            "process": active_process if matched_window_rule else "",
        }
        if self._on_captured:
            self._on_captured(item)
        if self._on_notify:
            try:
                self._on_notify("Screenshot", f"Berhasil menangkap {filename}")
            except Exception:
                pass
        return item

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            cycle_start = time.time()
            if not self._pause_event.is_set():
                try:
                    self.capture_once()
                except Exception as exc:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] [SCREENSHOT WARN] {exc}")

            with self._lock:
                interval = self._window_interval_override or int(self.config.get("interval_seconds", 3))
            interval = max(1, interval)
            elapsed = time.time() - cycle_start
            wait_time = max(0.05, interval - elapsed)
            self._stop_event.wait(timeout=wait_time)

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self.current_status == STATUS_RUNNING and self._worker and self._worker.is_alive():
                return self._build_response(True, "start", "ScreenshotService sudah berjalan.")
            self._stop_event.clear()
            self._pause_event.clear()
            self._worker = threading.Thread(target=self._worker_loop, daemon=True, name="ScreenshotServiceWorker")
            self._worker.start()
            self._set_status(STATUS_RUNNING)
            return self._build_response(True, "start", "ScreenshotService berhasil dijalankan.")

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._stop_event.set()
            self._pause_event.clear()
            worker = self._worker
            self._worker = None
        if worker and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=2.0)
        self._set_status(STATUS_STOPPED)
        return self._build_response(True, "stop", "ScreenshotService dihentikan secara normal (graceful stop).")

    def pause(self) -> dict[str, Any]:
        with self._lock:
            if self.current_status == STATUS_STOPPED:
                return self._build_response(False, "pause", "ScreenshotService sedang berhenti, tidak dapat dijeda.")
            self._pause_event.set()
            self._set_status(STATUS_PAUSED)
            return self._build_response(True, "pause", "ScreenshotService dijeda.")

    def resume(self) -> dict[str, Any]:
        with self._lock:
            if self.current_status == STATUS_STOPPED:
                return self.start()
            self._pause_event.clear()
            self._set_status(STATUS_RUNNING)
            return self._build_response(True, "resume", "ScreenshotService dilanjutkan.")

    def restart(self) -> dict[str, Any]:
        self.stop()
        res = self.start()
        res["action"] = "restart"
        res["message"] = "ScreenshotService berhasil di-restart."
        return res

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "service_key": self.service_key,
                "display_name": self.display_name,
                "status": self.current_status,
                "active_config_version": self.active_config_version,
                "uptime_seconds": self.get_uptime_seconds(),
                "total_captured": self.total_captured,
                "last_capture_at": self.last_capture_at,
                "interval_seconds": self.config.get("interval_seconds", 3),
                "image_format": self.config.get("image_format", "webp"),
                "keep_local_file": self.config.get("keep_local_file", False),
                "last_error": self.last_error,
                "updated_at": self.updated_at,
            }

    def apply_config(self, config: dict[str, Any], config_version: int) -> dict[str, Any]:
        is_valid, normalized, err = validate_service_config(self.service_key, config, base_config=self.config)
        if not is_valid or normalized is None:
            return self._build_response(False, "apply_config", f"Konfigurasi ScreenshotService tidak valid: {err}")

        with self._lock:
            self.config = normalized
            self.active_config_version = config_version
            self.updated_at = self._utc_now_iso()
        return self._build_response(True, "apply_config", f"Konfigurasi ScreenshotService v{config_version} diterapkan.")
