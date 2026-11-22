"""
LoveYouAgent: Runtime koordinator agen Windows client (outbound-only HTTPS).
Mengelola:
- Enrollment otomatis tanpa admin login di LoveYou
- Heartbeat periodik + auto-reconnect saat offline dengan exponential backoff
- HTTPS command polling (dipercepat trigger Realtime opsional) + validasi/idempotency/ack
- Sinkronisasi konfigurasi runtime (config_version + rollback)
- Pelaporan perubahan status service & pengiriman batch log aktivitas
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any

try:
    from backend.agent.api_client import AgentApiClient, AgentApiError, AgentAuthRevokedError, AgentNetworkError
    from backend.agent.command_executor import CommandExecutor
    from backend.agent.config_sync import ConfigSyncManager
    from backend.agent.enrollment import DeviceEnrollmentManager
    from backend.agent.identity import DeviceIdentityStore
    from backend.agent.realtime_listener import RealtimeCommandListener
    from backend.agent.updater import ReleaseUpdater
    from backend.services.manager import ServiceManager
except ImportError:
    from agent.api_client import AgentApiClient, AgentApiError, AgentAuthRevokedError, AgentNetworkError
    from agent.command_executor import CommandExecutor
    from agent.config_sync import ConfigSyncManager
    from agent.enrollment import DeviceEnrollmentManager
    from agent.identity import DeviceIdentityStore
    from agent.realtime_listener import RealtimeCommandListener
    from agent.updater import ReleaseUpdater
    from services.manager import ServiceManager


class LoveYouAgent:
    """Agen komunikasi outbound terpusat antara LoveYou Windows Client dan Xoose Control Plane."""

    def __init__(
        self,
        service_manager: ServiceManager,
        api_base_url: str,
        app_slug: str = "loveyou",
        app_version: str = "1.0.0",
        enrollment_code: str = "",
        heartbeat_interval_seconds: float = 30.0,
        poll_interval_seconds: float = 10.0,
        config_sync_interval_seconds: float = 60.0,
        realtime_enabled: bool = False,
        supabase_url: str = "",
        supabase_key: str = "",
        storage_dir: str | None = None,
        api_client: AgentApiClient | None = None,
        identity_store: DeviceIdentityStore | None = None,
    ):
        self.service_manager = service_manager
        self.app_slug = app_slug
        self.app_version = app_version
        self.heartbeat_interval = max(1.0, float(heartbeat_interval_seconds))
        self.poll_interval = max(1.0, float(poll_interval_seconds))
        self.config_sync_interval = max(5.0, float(config_sync_interval_seconds))

        self.identity = identity_store or DeviceIdentityStore(app_slug=app_slug, storage_dir=storage_dir)
        self.api_client = api_client or AgentApiClient(
            base_url=api_base_url,
            device_token=self.identity.get_device_token(),
        )
        self.enrollment = DeviceEnrollmentManager(
            identity_store=self.identity,
            api_client=self.api_client,
            app_slug=app_slug,
            app_version=app_version,
            enrollment_code=enrollment_code,
        )

        self._log_lock = threading.Lock()
        self._log_buffer: list[dict[str, Any]] = []
        self._max_log_buffer = 200

        self.command_executor = CommandExecutor(
            service_manager=self.service_manager,
            identity_store=self.identity,
            api_client=self.api_client,
            on_log=self.buffer_log,
        )
        self.config_sync = ConfigSyncManager(
            service_manager=self.service_manager,
            identity_store=self.identity,
            api_client=self.api_client,
            on_log=self.buffer_log,
        )
        self.updater = ReleaseUpdater(api_client=self.api_client, current_version=app_version)

        self._stop_event = threading.Event()
        self._poll_trigger = threading.Event()
        self._worker: threading.Thread | None = None

        self.realtime_listener = RealtimeCommandListener(
            device_id=self.identity.device_id,
            poll_trigger_event=self._poll_trigger,
            supabase_url=supabase_url,
            supabase_key=supabase_key,
            enabled=realtime_enabled,
        )

        self._started_at: float | None = None
        self._last_heartbeat_at: float = 0.0
        self._last_poll_at: float = 0.0
        self._last_config_sync_at: float = 0.0
        self._consecutive_failures: int = 0
        self.is_online: bool = False

        # Dengarkan perubahan status granular dari ServiceManager
        self.service_manager.on_status_change(self._on_service_status_changed)

    def buffer_log(self, service_key: str, level: str, message: str, context: dict[str, Any] | None = None) -> None:
        entry = {
            "service_key": service_key,
            "level": level,
            "event_type": "agent_event",
            "message": message,
            "context": context or {},
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        with self._log_lock:
            self._log_buffer.append(entry)
            if len(self._log_buffer) > self._max_log_buffer:
                self._log_buffer = self._log_buffer[-self._max_log_buffer :]

    def _on_service_status_changed(self, service_key: str, status_payload: dict[str, Any]) -> None:
        if not self.identity.is_enrolled():
            return
        try:
            self.api_client.report_service_status(service_key, status_payload)
        except AgentAuthRevokedError:
            self.enrollment.handle_revocation()
        except Exception:
            pass

    def flush_logs(self) -> int:
        if not self.identity.is_enrolled():
            return 0
        with self._log_lock:
            if not self._log_buffer:
                return 0
            batch = list(self._log_buffer)
            self._log_buffer.clear()
        try:
            self.api_client.send_logs(batch)
            return len(batch)
        except AgentAuthRevokedError:
            self.enrollment.handle_revocation()
            return 0
        except Exception:
            # Kembalikan batch ke buffer agar terkirim ulang saat koneksi kembali online
            with self._log_lock:
                self._log_buffer = (batch + self._log_buffer)[-self._max_log_buffer :]
            return 0

    def trigger_poll(self) -> None:
        """Memicu polling command seketika (misal saat menerima sinyal Realtime)."""
        self._poll_trigger.set()

    def get_uptime_seconds(self) -> float:
        if self._started_at is None:
            return 0.0
        return round(max(0.0, time.time() - self._started_at), 2)

    def run_cycle(self, force_all: bool = False) -> dict[str, Any]:
        """
        Menjalankan satu siklus komunikasi Agent (enrollment -> heartbeat -> command poll -> config sync -> logs).
        Dapat dipanggil langsung dalam pengujian deterministik (pytest).
        """
        summary: dict[str, Any] = {
            "enrolled": False,
            "heartbeat_sent": False,
            "commands_processed": [],
            "configs_synced": [],
            "logs_flushed": 0,
            "online": False,
            "revoked": self.identity.is_revoked,
        }

        if self.identity.is_revoked:
            self.is_online = False
            return summary

        if not self.identity.is_enrolled():
            ok, msg = self.enrollment.ensure_enrolled(
                service_definitions=self.service_manager.get_service_definitions()
            )
            if not ok:
                self._consecutive_failures += 1
                self.is_online = False
                summary["error"] = msg
                return summary
            self.realtime_listener.device_id = self.identity.device_id
            self.realtime_listener.start()

        summary["enrolled"] = True
        now = time.time()

        try:
            # 1. Heartbeat periodik
            if force_all or (now - self._last_heartbeat_at >= self.heartbeat_interval):
                self.api_client.send_heartbeat(
                    app_version=self.app_version,
                    agent_uptime=self.get_uptime_seconds(),
                    services_status=self.service_manager.get_all_statuses(),
                )
                self._last_heartbeat_at = now
                summary["heartbeat_sent"] = True

            # 2. HTTPS Command Polling (+ wake-up trigger)
            if force_all or self._poll_trigger.is_set() or (now - self._last_poll_at >= self.poll_interval):
                self._poll_trigger.clear()
                commands = self.api_client.poll_pending_commands()
                self._last_poll_at = now
                for cmd in commands:
                    res = self.command_executor.execute_command(cmd)
                    summary["commands_processed"].append(res)

            # 3. Runtime Config Sync
            if force_all or (now - self._last_config_sync_at >= self.config_sync_interval):
                synced = self.config_sync.sync_once()
                self._last_config_sync_at = now
                summary["configs_synced"] = synced

            # 4. Flush buffered logs
            summary["logs_flushed"] = self.flush_logs()

            self._consecutive_failures = 0
            self.is_online = True
            summary["online"] = True
        except AgentAuthRevokedError as exc:
            self.enrollment.handle_revocation()
            self.is_online = False
            summary["revoked"] = True
            summary["error"] = str(exc)
        except (AgentNetworkError, AgentApiError) as exc:
            self._consecutive_failures += 1
            self.is_online = False
            summary["online"] = False
            summary["error"] = str(exc)

        return summary

    def _compute_backoff_wait(self) -> float:
        if self._consecutive_failures <= 0:
            return min(self.poll_interval, self.heartbeat_interval, 5.0)
        return min(60.0, float(2 ** min(self._consecutive_failures, 6)))

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            self.run_cycle(force_all=False)
            if self.identity.is_revoked:
                break
            wait_sec = self._compute_backoff_wait()
            # Tunggu sampai interval selesai atau sampai sinyal Realtime memicu _poll_trigger
            triggered = self._poll_trigger.wait(timeout=wait_sec)
            if triggered and self._stop_event.is_set():
                break

    def start(self) -> bool:
        if self._worker and self._worker.is_alive():
            return True
        self._stop_event.clear()
        self._started_at = time.time()
        self.realtime_listener.start()
        self._worker = threading.Thread(target=self._run_loop, daemon=True, name="LoveYouAgentLoop")
        self._worker.start()
        return True

    def stop(self) -> None:
        self._stop_event.set()
        self._poll_trigger.set()
        self.realtime_listener.stop()
        worker = self._worker
        self._worker = None
        if worker and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=3.0)
        self.api_client.close()
