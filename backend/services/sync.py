"""
SyncService: Layanan modular pengunggah antrean screenshot & metadata ke Supabase
dengan auto-retry saat offline/boot, graceful stop, dan pembersihan file temporer.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from datetime import datetime
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


class SyncService(BaseService):
    """Layanan modular sinkronisasi & upload antrean ke Supabase Storage/Database."""

    service_key = "sync_service"
    display_name = "Sync Service"
    supported_actions = ["start", "stop", "pause", "resume", "restart"]
    configuration_schema = get_service_json_schema("sync_service")
    permission_requirements = ["network_outbound"]

    def __init__(
        self,
        initial_config: dict[str, Any] | None = None,
        logger_factory: Callable[[], Any] | None = None,
        get_user_fn: Callable[[], str] | None = None,
        keep_local_file_fn: Callable[[], bool] | None = None,
        on_uploaded: Callable[[dict[str, Any]], None] | None = None,
    ):
        defaults = {
            "supabase_url": "",
            "table_name": "hasil_ss",
            "bucket_name": "screenshots",
            "max_queue_size": 200,
            "max_upload_retries": 6,
            "retry_backoff_seconds": 4.0,
        }
        if initial_config:
            defaults.update({k: v for k, v in initial_config.items() if k in defaults})
        super().__init__(initial_config=defaults)

        self._logger_factory = logger_factory
        self._logger: Any = None
        self._get_user_fn = get_user_fn or (lambda: "anonymous")
        self._keep_local_file_fn = keep_local_file_fn or (lambda: False)
        self._on_uploaded = on_uploaded

        self.upload_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=int(defaults["max_queue_size"]))
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._worker: threading.Thread | None = None

        self.total_uploaded: int = 0
        self.total_failed: int = 0
        self.last_upload_at: str | None = None

    @staticmethod
    def is_transient_network_error(err: Exception | str) -> bool:
        msg = str(err).lower()
        markers = (
            "connect",
            "timeout",
            "timed out",
            "getaddrinfo",
            "name resolution",
            "network",
            "dns",
            "ssl",
            "socket",
            "winerror",
            "temporarily unavailable",
            "eof",
            "reset",
        )
        return any(m in msg for m in markers)

    def ensure_logger(self) -> Any:
        if self._logger is not None:
            return self._logger
        if self._logger_factory is None:
            return None
        try:
            self._logger = self._logger_factory()
            return self._logger
        except Exception as exc:
            self.last_error = str(exc)
            return None

    def enqueue(self, item: dict[str, Any]) -> bool:
        """Menambahkan item ke antrean upload; mengganti item tertua jika antrean penuh."""
        try:
            self.upload_queue.put_nowait(item)
            return True
        except queue.Full:
            try:
                dropped = self.upload_queue.get_nowait()
                self.upload_queue.task_done()
                if not self._keep_local_file_fn():
                    fp = dropped.get("filepath", "")
                    if fp and os.path.exists(fp):
                        os.remove(fp)
                self.upload_queue.put_nowait(item)
                return True
            except Exception:
                return False

    def process_item(self, item: dict[str, Any]) -> bool:
        """Memproses satu item upload Storage + insert Database dengan retry jaringan."""
        filepath = item.get("filepath", "")
        filename = item.get("filename", "")
        resolution = item.get("resolution", "Unknown")
        active_window = item.get("active_window", "")
        file_size = int(item.get("file_size", 0))
        user_name = item.get("user_name", "anonymous")

        if user_name == "anonymous":
            latest_user = (self._get_user_fn() or "anonymous").strip()
            if latest_user and latest_user != "anonymous":
                user_name = latest_user

        bucket = str(self.config.get("bucket_name", "screenshots"))
        supa_url = str(self.config.get("supabase_url", "")).rstrip("/")
        storage_filename = f"{user_name}/{filename}" if user_name != "anonymous" else filename
        storage_path = f"{bucket}/{storage_filename}"

        max_retries = max(1, int(self.config.get("max_upload_retries", 6)))
        backoff = max(0.2, float(self.config.get("retry_backoff_seconds", 4.0)))

        for _ in range(max_retries):
            if self._stop_event.is_set():
                return False
            if self.ensure_logger() is not None:
                break
            self._stop_event.wait(timeout=min(backoff, 3.0))

        logger = self._logger
        ok = False
        if logger is not None and filepath and os.path.exists(filepath):
            image_url = ""
            storage_error: str | None = None

            for attempt in range(1, max_retries + 1):
                if self._stop_event.is_set():
                    break
                try:
                    storage_error = None
                    image_url = logger.upload_file(filepath, storage_filename)
                    break
                except Exception as exc:
                    storage_error = str(exc)
                    if attempt < max_retries and self.is_transient_network_error(exc):
                        self._stop_event.wait(timeout=min(backoff * attempt, 10.0))
                    else:
                        break

            if not image_url and not storage_error and supa_url:
                image_url = f"{supa_url}/storage/v1/object/public/{bucket}/{storage_filename}"

            for attempt in range(1, max(2, max_retries) + 1):
                if self._stop_event.is_set():
                    break
                try:
                    status = "success" if not storage_error else "storage_failed"
                    source = str(item.get("source", "desktop_mss")).strip() or "desktop_mss"
                    capture_meta = {
                        "source": source,
                        "browser": item.get("browser", ""),
                        "rule_id": item.get("rule_id", ""),
                        "device_id": item.get("device_id", ""),
                        "origin_domain": item.get("origin_domain", ""),
                        "captured_at": item.get("captured_at", ""),
                        "mime_type": item.get("mime_type", ""),
                        "image_checksum": item.get("image_checksum", ""),
                        "process": item.get("process", ""),
                    }
                    if source == "browser_tab":
                        sha_short = str(capture_meta["image_checksum"])[:16]
                        notes = (
                            f"[source=browser_tab | browser={capture_meta['browser']} | "
                            f"rule={capture_meta['rule_id']} | domain={capture_meta['origin_domain']} | "
                            f"sha256={sha_short}]"
                        )
                    elif source == "application_window":
                        notes = (
                            f"[source=application_window | process={capture_meta['process']} | "
                            f"rule={capture_meta['rule_id']}] Window: {active_window}"
                        )
                    else:
                        notes = f"Window: {active_window}" if active_window else ""

                    if storage_error:
                        notes += f" | Storage err: {storage_error}"

                    log_kwargs = {
                        "filename": filename,
                        "image_url": image_url or "",
                        "resolution": resolution,
                        "active_window": active_window,
                        "file_size_bytes": file_size,
                        "status": status,
                        "notes": notes,
                        "user_name": user_name,
                        "storage_path": storage_path,
                    }
                    try:
                        logger.log_record(**log_kwargs, capture_metadata=capture_meta)
                    except TypeError:
                        logger.log_record(**log_kwargs)

                    with self._lock:
                        self.total_uploaded += 1
                        self.last_upload_at = self._utc_now_iso()
                    ok = True
                    if self._on_uploaded:
                        self._on_uploaded(
                            {
                                "filename": filename,
                                "user_name": user_name,
                                "status": status,
                                "source": source,
                            }
                        )
                    break
                except Exception as exc:
                    self.last_error = str(exc)
                    if attempt < max_retries and self.is_transient_network_error(exc):
                        self._stop_event.wait(timeout=min(backoff * attempt, 8.0))
                    else:
                        with self._lock:
                            self.total_failed += 1
                        break

        if not self._keep_local_file_fn() and filepath and os.path.exists(filepath):
            try:
                os.remove(filepath)
            except Exception:
                pass

        return ok

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            if self._pause_event.is_set():
                self._stop_event.wait(timeout=0.3)
                continue
            try:
                item = self.upload_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.process_item(item)
            except Exception as exc:
                self.last_error = str(exc)
                print(f"[{datetime.now().strftime('%H:%M:%S')}] [SYNC WARN] {exc}")
            finally:
                self.upload_queue.task_done()

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self.current_status == STATUS_RUNNING and self._worker and self._worker.is_alive():
                return self._build_response(True, "start", "SyncService sudah berjalan.")
            self._stop_event.clear()
            self._pause_event.clear()
            self._worker = threading.Thread(target=self._worker_loop, daemon=True, name="SyncServiceWorker")
            self._worker.start()
            self._set_status(STATUS_RUNNING)
            return self._build_response(True, "start", "SyncService berhasil dijalankan.")

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._stop_event.set()
            self._pause_event.clear()
            worker = self._worker
            self._worker = None
        if worker and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=2.0)
        self._set_status(STATUS_STOPPED)
        return self._build_response(True, "stop", "SyncService dihentikan secara normal.")

    def pause(self) -> dict[str, Any]:
        with self._lock:
            if self.current_status == STATUS_STOPPED:
                return self._build_response(False, "pause", "SyncService sedang berhenti.")
            self._pause_event.set()
            self._set_status(STATUS_PAUSED)
            return self._build_response(True, "pause", "SyncService dijeda (antrean tetap ditahan).")

    def resume(self) -> dict[str, Any]:
        with self._lock:
            if self.current_status == STATUS_STOPPED:
                return self.start()
            self._pause_event.clear()
            self._set_status(STATUS_RUNNING)
            return self._build_response(True, "resume", "SyncService dilanjutkan.")

    def restart(self) -> dict[str, Any]:
        self.stop()
        self._logger = None
        res = self.start()
        res["action"] = "restart"
        res["message"] = "SyncService berhasil di-restart."
        return res

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "service_key": self.service_key,
                "display_name": self.display_name,
                "status": self.current_status,
                "active_config_version": self.active_config_version,
                "uptime_seconds": self.get_uptime_seconds(),
                "queue_size": self.upload_queue.qsize(),
                "total_uploaded": self.total_uploaded,
                "total_failed": self.total_failed,
                "last_upload_at": self.last_upload_at,
                "last_error": self.last_error,
                "updated_at": self.updated_at,
            }

    def apply_config(self, config: dict[str, Any], config_version: int) -> dict[str, Any]:
        is_valid, normalized, err = validate_service_config(self.service_key, config, base_config=self.config)
        if not is_valid or normalized is None:
            return self._build_response(False, "apply_config", f"Konfigurasi SyncService tidak valid: {err}")

        with self._lock:
            url_changed = (
                normalized.get("supabase_url") != self.config.get("supabase_url")
                or normalized.get("table_name") != self.config.get("table_name")
                or normalized.get("bucket_name") != self.config.get("bucket_name")
            )
            self.config = normalized
            self.active_config_version = config_version
            if url_changed:
                self._logger = None
            self.updated_at = self._utc_now_iso()
        return self._build_response(True, "apply_config", f"Konfigurasi SyncService v{config_version} diterapkan.")
