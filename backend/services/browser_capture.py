"""
BrowserCaptureService: Layanan modular (implementasi BaseService) untuk
Specific Screen & Browser Domain Capture via Native Messaging Host.

Tanggung jawab:
- Menerima konfigurasi runtime berversi (capture_policy + browser_capture + window_capture)
- Mendorong aturan domain terbaru secara live ke NativeHostBridge (runtime_config.json)
- Memantau spool/incoming/ secara atomik dan meneruskan gambar tab browser ke SyncService
  dengan metadata terpisah (source="browser_tab")
- Melaporkan status live extension, browser terhubung, rule aktif, mode izin, sesi lokal,
  jumlah capture, error terakhir, dan config_version ke Xoose (/api/v1/agent/status)
- Memicu koordinator mode (on_policy_changed) untuk menyelaraskan ScreenshotService (MSS)
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

try:
    from backend.agent.native_bridge import NativeHostBridge
    from backend.schemas.service_schemas import (
        BrowserCaptureServiceConfig,
        get_service_json_schema,
        validate_service_config,
    )
    from backend.services.base import (
        STATUS_PAUSED,
        STATUS_RUNNING,
        STATUS_STOPPED,
        BaseService,
    )
except ImportError:
    from agent.native_bridge import NativeHostBridge
    from schemas.service_schemas import (
        BrowserCaptureServiceConfig,
        get_service_json_schema,
        validate_service_config,
    )
    from services.base import (
        STATUS_PAUSED,
        STATUS_RUNNING,
        STATUS_STOPPED,
        BaseService,
    )

DAYS_ID = {
    0: "Senin",
    1: "Selasa",
    2: "Rabu",
    3: "Kamis",
    4: "Jumat",
    5: "Sabtu",
    6: "Minggu",
}


class BrowserCaptureService(BaseService):
    """Layanan modular pengendali Browser Domain Capture & koordinasi live source selection."""

    service_key = "browser_capture_service"
    display_name = "Browser Capture Service"
    supported_actions = ["start", "stop", "pause", "resume", "restart"]
    configuration_schema = get_service_json_schema("browser_capture_service")
    permission_requirements = ["browser_extension", "native_messaging", "local_pause_stop_control"]

    def __init__(
        self,
        initial_config: dict[str, Any] | None = None,
        native_bridge: NativeHostBridge | None = None,
        get_user_fn: Callable[[], str] | None = None,
        get_device_id_fn: Callable[[], str] | None = None,
        on_captured: Callable[[dict[str, Any]], None] | None = None,
        on_policy_changed: Callable[[dict[str, Any], int], None] | None = None,
        poll_interval_seconds: float = 0.5,
    ):
        defaults = BrowserCaptureServiceConfig().model_dump()
        if initial_config:
            ok, normalized, _ = validate_service_config(self.service_key, initial_config, base_config=defaults)
            if ok and normalized is not None:
                defaults = normalized

        super().__init__(initial_config=defaults)

        self.bridge = native_bridge or NativeHostBridge()
        self._get_user_fn = get_user_fn or (lambda: "anonymous")
        self._get_device_id_fn = get_device_id_fn or (lambda: "")
        self._on_captured = on_captured
        self._on_policy_changed = on_policy_changed
        self._poll_interval = max(0.1, float(poll_interval_seconds))

        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._worker: threading.Thread | None = None

        self.total_forwarded: int = 0
        self.last_capture_at: str | None = None

    def _stage_image_for_sync(self, spool_image_path: str, browser: str) -> tuple[str, str]:
        """Memindahkan file spool ke direktori temp capture dengan nama file teridentifikasi browser_tab."""
        src = Path(spool_image_path)
        ext = src.suffix.lstrip(".").lower() or "webp"
        now = datetime.now()
        hari = DAYS_ID.get(now.weekday(), "Hari")
        base_name = f"browser_{browser}_{now.strftime('%H-%M-%S_%d-%m-%Y')}_{hari}"

        stage_dir = Path(tempfile.gettempdir()) / "_xoose_browser_capture"
        stage_dir.mkdir(parents=True, exist_ok=True)

        filename = f"{base_name}.{ext}"
        dest = stage_dir / filename
        counter = 1
        while dest.exists():
            filename = f"{base_name}_{counter}.{ext}"
            dest = stage_dir / filename
            counter += 1

        try:
            os.replace(src, dest)
        except Exception:
            shutil.copy2(src, dest)
            src.unlink(missing_ok=True)

        return str(dest), filename

    def consume_spool_once(self) -> list[dict[str, Any]]:
        """Memproses seluruh item valid di spool/incoming/ dan meneruskannya ke SyncService."""
        if self._pause_event.is_set() or self.current_status == STATUS_STOPPED:
            return []

        raw_items = self.bridge.consume_incoming_spool(max_items=25)
        forwarded: list[dict[str, Any]] = []

        for entry in raw_items:
            meta = entry.get("metadata") or {}
            spool_img = entry.get("image_path", "")
            if not spool_img or not os.path.exists(spool_img):
                continue

            browser = str(meta.get("browser", "chrome")).strip().lower() or "chrome"
            origin_domain = str(meta.get("origin_domain", "")).strip().lower()
            rule_id = str(meta.get("rule_id", "")).strip()
            device_id = str(meta.get("device_id") or self._get_device_id_fn() or "").strip()
            captured_at = str(meta.get("captured_at") or self._utc_now_iso())
            mime_type = str(meta.get("mime_type", "image/webp"))
            image_checksum = str(meta.get("image_checksum", ""))
            resolution = str(meta.get("resolution", "Unknown"))

            staged_path, filename = self._stage_image_for_sync(spool_img, browser)
            file_size = os.path.getsize(staged_path) if os.path.exists(staged_path) else 0
            active_user = (self._get_user_fn() or "anonymous").strip() or "anonymous"

            sync_item = {
                "filepath": staged_path,
                "filename": filename,
                "resolution": resolution,
                "active_window": f"[{browser}] {origin_domain}" if origin_domain else f"[{browser}] browser_tab",
                "file_size": file_size,
                "user_name": active_user,
                # Metadata terpisah browser_tab sesuai kontrak Section 5
                "source": "browser_tab",
                "browser": browser,
                "rule_id": rule_id,
                "device_id": device_id,
                "origin_domain": origin_domain,
                "captured_at": captured_at,
                "mime_type": mime_type,
                "image_checksum": image_checksum,
            }

            with self._lock:
                self.total_forwarded += 1
                self.last_capture_at = captured_at

            if self._on_captured:
                self._on_captured(sync_item)
            forwarded.append(sync_item)

        return forwarded

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            if not self._pause_event.is_set():
                try:
                    self.consume_spool_once()
                except Exception as exc:
                    self.last_error = str(exc)
            self._stop_event.wait(timeout=self._poll_interval)

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self.current_status == STATUS_RUNNING and self._worker and self._worker.is_alive():
                return self._build_response(True, "start", "BrowserCaptureService sudah berjalan.")
            self._stop_event.clear()
            self._pause_event.clear()
            self.bridge.publish_runtime_config(
                config=self.config,
                config_version=self.active_config_version,
                device_id=self._get_device_id_fn(),
            )
            self._worker = threading.Thread(
                target=self._worker_loop,
                daemon=True,
                name="BrowserCaptureSpoolWorker",
            )
            self._worker.start()
            self._set_status(STATUS_RUNNING)
            return self._build_response(True, "start", "BrowserCaptureService berhasil dijalankan.")

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._stop_event.set()
            self._pause_event.clear()
            worker = self._worker
            self._worker = None
        if worker and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=2.0)
        try:
            self.bridge.send_control_signal("stop", source="browser_capture_service")
        except Exception:
            pass
        self._set_status(STATUS_STOPPED)
        return self._build_response(True, "stop", "BrowserCaptureService dihentikan secara normal.")

    def pause(self) -> dict[str, Any]:
        with self._lock:
            if self.current_status == STATUS_STOPPED:
                return self._build_response(False, "pause", "BrowserCaptureService sedang berhenti.")
            self._pause_event.set()
            self._set_status(STATUS_PAUSED)
        try:
            self.bridge.send_control_signal("pause", source="browser_capture_service")
        except Exception:
            pass
        return self._build_response(True, "pause", "BrowserCaptureService dijeda.")

    def resume(self) -> dict[str, Any]:
        with self._lock:
            if self.current_status == STATUS_STOPPED:
                return self.start()
            self._pause_event.clear()
            self._set_status(STATUS_RUNNING)
        try:
            self.bridge.send_control_signal("resume", source="browser_capture_service")
        except Exception:
            pass
        return self._build_response(True, "resume", "BrowserCaptureService dilanjutkan.")

    def restart(self) -> dict[str, Any]:
        self.stop()
        res = self.start()
        res["action"] = "restart"
        res["message"] = "BrowserCaptureService berhasil di-restart."
        return res

    def get_status(self) -> dict[str, Any]:
        host_summary = self.bridge.read_host_statuses()
        primary = host_summary.get("primary") or {}
        policy = self.config.get("capture_policy") or {}
        bc = self.config.get("browser_capture") or {}
        wc = self.config.get("window_capture") or {}

        with self._lock:
            return {
                "service_key": self.service_key,
                "display_name": self.display_name,
                "status": self.current_status,
                "active_config_version": self.active_config_version,
                "uptime_seconds": self.get_uptime_seconds(),
                "active_source": policy.get("active_source", "desktop_mss"),
                "browser_capture_enabled": bool(bc.get("enabled", False)),
                "window_capture_enabled": bool(wc.get("enabled", False)),
                "permission_mode": primary.get("permission_mode") or bc.get("permission_mode", "local_start"),
                "has_host_permission": bool(primary.get("has_host_permission", False)),
                "extension_connected": bool(host_summary.get("extension_connected", False)),
                "connected_browsers": list(host_summary.get("connected_browsers", [])),
                "local_session_state": primary.get("local_session_state", "stopped"),
                "capture_state": primary.get("capture_state", "idle"),
                "active_rule_id": primary.get("active_rule_id"),
                "matched_domain": primary.get("matched_domain"),
                "extension_config_version": int(primary.get("config_version", 0) or 0),
                "browser_rules_count": len([r for r in bc.get("rules", []) if r.get("enabled")]),
                "window_rules_count": len([r for r in wc.get("rules", []) if r.get("enabled")]),
                "total_captured": self.total_forwarded,
                "last_capture_at": self.last_capture_at or primary.get("last_capture_at"),
                "last_error": self.last_error or primary.get("last_error"),
                "updated_at": self.updated_at,
            }

    def apply_config(self, config: dict[str, Any], config_version: int) -> dict[str, Any]:
        is_valid, normalized, err = validate_service_config(self.service_key, config, base_config=self.config)
        if not is_valid or normalized is None:
            return self._build_response(False, "apply_config", f"Konfigurasi BrowserCaptureService tidak valid: {err}")

        with self._lock:
            self.config = normalized
            self.active_config_version = config_version
            self.updated_at = self._utc_now_iso()

        # Dorong konfigurasi terbaru secara live ke Native Host -> Browser Extension
        self.bridge.publish_runtime_config(
            config=normalized,
            config_version=config_version,
            device_id=self._get_device_id_fn(),
        )

        # Beri tahu koordinator mode (ServiceManager / ServiceController) untuk menyelaraskan MSS
        if self._on_policy_changed:
            self._on_policy_changed(normalized, config_version)

        return self._build_response(
            True,
            "apply_config",
            f"Konfigurasi BrowserCaptureService v{config_version} diterapkan & disinkronkan ke Native Bridge.",
        )
