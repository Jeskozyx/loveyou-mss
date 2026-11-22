"""
DeviceEnrollmentManager: Menangani pendaftaran perangkat baru (enrollment) ke Xoose API,
penerbitan token unik per perangkat, penanganan revocation, dan self-unregister lokal.
"""

from __future__ import annotations

from typing import Any

try:
    from backend.agent.api_client import AgentApiClient, AgentApiError, AgentAuthRevokedError, AgentNetworkError
    from backend.agent.identity import DeviceIdentityStore
except ImportError:
    from agent.api_client import AgentApiClient, AgentApiError, AgentAuthRevokedError, AgentNetworkError
    from agent.identity import DeviceIdentityStore


class DeviceEnrollmentManager:
    """Mengelola siklus enrollment & revocation kredensial perangkat."""

    def __init__(
        self,
        identity_store: DeviceIdentityStore,
        api_client: AgentApiClient,
        app_slug: str = "loveyou",
        app_version: str = "1.0.0",
        enrollment_code: str = "",
    ):
        self.identity = identity_store
        self.api_client = api_client
        self.app_slug = app_slug
        self.app_version = app_version
        self.enrollment_code = (enrollment_code or "").strip()

        # Jika token tersimpan sebelumnya, langsung pasangkan ke api_client
        saved_token = self.identity.get_device_token()
        if saved_token and not self.identity.is_revoked:
            self.api_client.set_token(saved_token)

    def ensure_enrolled(self, service_definitions: list[dict[str, Any]] | None = None) -> tuple[bool, str]:
        """
        Memastikan perangkat sudah terdaftar dan memiliki token aktif.
        Returns: (is_ready, message)
        """
        if self.identity.is_revoked:
            return False, "Akses perangkat telah dicabut (revoked)."

        if self.identity.is_enrolled():
            self.api_client.set_token(self.identity.get_device_token())
            return True, f"Perangkat sudah terdaftar (device_id={self.identity.device_id})."

        if not self.enrollment_code:
            return False, "Enrollment code belum dikonfigurasi."

        try:
            resp = self.api_client.enroll(
                enrollment_code=self.enrollment_code,
                device_uuid=self.identity.device_uuid,
                hostname=self.identity.hostname,
                os_info=self.identity.os_info,
                app_slug=self.app_slug,
                app_version=self.app_version,
                service_definitions=service_definitions,
            )
        except AgentNetworkError as exc:
            return False, f"Enrollment tertunda (jaringan offline): {exc}"
        except AgentApiError as exc:
            return False, f"Enrollment ditolak backend: {exc}"

        data = resp.get("data", resp) if isinstance(resp, dict) else {}
        device_id = str(data.get("device_id", "")).strip()
        device_token = str(data.get("device_token", "")).strip()
        device_code = str(data.get("device_code", device_id)).strip()

        if not device_id or not device_token:
            return False, "Respons enrollment backend tidak menyertakan device_id atau device_token."

        self.identity.save_enrollment(device_id=device_id, device_token=device_token, device_code=device_code)
        self.api_client.set_token(device_token)
        return True, f"Enrollment berhasil (device_id={device_id})."

    def handle_revocation(self) -> None:
        """Dipanggil saat backend mengembalikan 401/403 (token dicabut oleh admin)."""
        self.identity.mark_revoked()
        self.api_client.set_token("")

    def unregister_local(self, reason: str = "local_user_unregister") -> tuple[bool, str]:
        """Mencabut perangkat dari sisi lokal (memberitahu backend bila online, lalu menghapus token lokal)."""
        backend_notified = False
        if self.identity.is_enrolled():
            try:
                self.api_client.unregister_self(reason=reason)
                backend_notified = True
            except (AgentAuthRevokedError, AgentNetworkError, AgentApiError):
                backend_notified = False

        self.identity.mark_revoked()
        self.api_client.set_token("")
        msg = "Perangkat dicabut di backend & lokal." if backend_notified else "Token lokal dicabut (backend akan sinkron saat online)."
        return True, msg
