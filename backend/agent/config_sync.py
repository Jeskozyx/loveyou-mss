"""
ConfigSyncManager: Sinkronisasi konfigurasi runtime berversi (config_version)
dari Xoose API ke ServiceManager dengan validasi skema dan rollback otomatis.
"""

from __future__ import annotations

from typing import Any, Callable

try:
    from backend.agent.api_client import AgentApiClient, AgentAuthRevokedError, AgentNetworkError
    from backend.agent.identity import DeviceIdentityStore
    from backend.services.manager import ServiceManager
except ImportError:
    from agent.api_client import AgentApiClient, AgentAuthRevokedError, AgentNetworkError
    from agent.identity import DeviceIdentityStore
    from services.manager import ServiceManager


class ConfigSyncManager:
    """Memeriksa versi konfigurasi terbaru dari backend dan menerapkannya secara aman."""

    def __init__(
        self,
        service_manager: ServiceManager,
        identity_store: DeviceIdentityStore,
        api_client: AgentApiClient,
        on_log: Callable[[str, str, str, dict[str, Any] | None], None] | None = None,
    ):
        self.service_manager = service_manager
        self.identity = identity_store
        self.api_client = api_client
        self._on_log = on_log

    def _emit_log(self, service_key: str, level: str, message: str, context: dict[str, Any] | None = None) -> None:
        if self._on_log:
            try:
                self._on_log(service_key, level, message, context)
            except Exception:
                pass

    def sync_once(self) -> list[dict[str, Any]]:
        """
        Mengambil daftar konfigurasi runtime dari /api/v1/agent/configs dan menerapkan
        hanya versi yang lebih baru (atau versi target eksplisit saat rollback dari admin).
        """
        local_versions = self.identity.get_config_versions()
        remote_configs = self.api_client.fetch_configs(current_versions=local_versions)
        results: list[dict[str, Any]] = []

        for entry in remote_configs:
            if not isinstance(entry, dict):
                continue
            service_key = str(entry.get("service_key", "")).strip()
            if not service_key:
                continue
            try:
                remote_version = int(entry.get("config_version", 0))
            except (TypeError, ValueError):
                continue

            config_json = entry.get("config_json", entry.get("config", {}))
            force_apply = bool(entry.get("force_apply", False))
            current_local_version = int(local_versions.get(service_key, 0))

            if not force_apply and remote_version <= current_local_version:
                continue

            res = self.service_manager.apply_service_config(
                service_key=service_key,
                config=config_json,
                config_version=remote_version,
            )

            if res.get("ok"):
                self.identity.set_config_version(service_key, remote_version)
                local_versions[service_key] = remote_version
                self._emit_log(
                    service_key,
                    "info",
                    f"Config runtime v{remote_version} berhasil diterapkan pada '{service_key}'.",
                    {"config_version": remote_version},
                )
                svc = self.service_manager.get(service_key)
                if svc is not None:
                    try:
                        self.api_client.report_service_status(service_key, svc.get_status())
                    except (AgentNetworkError, AgentAuthRevokedError):
                        pass
            else:
                self._emit_log(
                    service_key,
                    "error",
                    f"Gagal menerapkan config v{remote_version} pada '{service_key}': {res.get('message')}",
                    {"config_version": remote_version, "rolled_back": res.get("rolled_back", False)},
                )

            results.append(res)

        return results
