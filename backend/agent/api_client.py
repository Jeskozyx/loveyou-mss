"""
AgentApiClient: Klien HTTPS outbound-only untuk komunikasi LoveYou Agent dengan
API Xoose (/api/v1/agent/*). Tidak pernah membuka port masuk di Windows.
"""

from __future__ import annotations

from typing import Any

import httpx


class AgentApiError(Exception):
    """Error umum komunikasi API Agent."""

    def __init__(self, message: str, status_code: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class AgentAuthRevokedError(AgentApiError):
    """Dilempar saat backend menolak token perangkat (401/403 — revoked atau invalid)."""


class AgentNetworkError(AgentApiError):
    """Dilempar saat jaringan/server tidak dapat dijangkau (offline, DNS, timeout, 5xx)."""


class AgentApiClient:
    """Klien HTTPS terautentikasi per perangkat untuk semua endpoint /api/v1/agent/*."""

    def __init__(
        self,
        base_url: str,
        device_token: str = "",
        timeout_seconds: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.device_token = device_token or ""
        self.timeout_seconds = timeout_seconds
        self._client = httpx.Client(
            base_url=self.base_url or "http://localhost:8000",
            timeout=timeout_seconds,
            transport=transport,
            headers={
                "Accept": "application/json",
                "User-Agent": "LoveYou-Agent/1.0",
            },
        )

    def set_token(self, token: str) -> None:
        self.device_token = token or ""

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    def _auth_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.device_token:
            headers["Authorization"] = f"Bearer {self.device_token}"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        require_auth: bool = True,
    ) -> dict[str, Any]:
        headers = self._auth_headers() if require_auth else {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        try:
            resp = self._client.request(
                method=method,
                url=path,
                json=json_body,
                params=params,
                headers=headers,
            )
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            raise AgentNetworkError(f"Koneksi ke Xoose gagal: {exc}") from exc
        except httpx.HTTPError as exc:
            raise AgentNetworkError(f"HTTP client error: {exc}") from exc

        parsed: Any = {}
        if resp.content:
            try:
                parsed = resp.json()
            except Exception:
                parsed = {"raw": resp.text}

        if resp.status_code in (401, 403):
            msg = parsed.get("message", "Device token tidak valid atau telah dicabut.") if isinstance(parsed, dict) else "Unauthorized"
            raise AgentAuthRevokedError(msg, status_code=resp.status_code, payload=parsed)

        if resp.status_code >= 500:
            raise AgentNetworkError(
                f"Server Xoose error HTTP {resp.status_code}",
                status_code=resp.status_code,
                payload=parsed,
            )

        if resp.status_code >= 400:
            msg = parsed.get("message", f"HTTP {resp.status_code}") if isinstance(parsed, dict) else f"HTTP {resp.status_code}"
            raise AgentApiError(msg, status_code=resp.status_code, payload=parsed)

        return parsed if isinstance(parsed, dict) else {"data": parsed}

    def enroll(
        self,
        enrollment_code: str,
        device_uuid: str,
        hostname: str,
        os_info: str,
        app_slug: str,
        app_version: str,
        service_definitions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """POST /api/v1/agent/enroll — Mendaftarkan perangkat baru dan menerima token unik."""
        payload = {
            "enrollment_code": enrollment_code,
            "device_uuid": device_uuid,
            "hostname": hostname,
            "os_info": os_info,
            "app_slug": app_slug,
            "app_version": app_version,
            "services": service_definitions or [],
        }
        return self._request("POST", "/api/v1/agent/enroll", json_body=payload, require_auth=False)

    def send_heartbeat(
        self,
        app_version: str,
        agent_uptime: float,
        services_status: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """POST /api/v1/agent/heartbeat — Mengirim heartbeat periodik & ringkasan status service."""
        payload = {
            "app_version": app_version,
            "agent_uptime": round(agent_uptime, 2),
            "services": services_status,
        }
        return self._request("POST", "/api/v1/agent/heartbeat", json_body=payload)

    def poll_pending_commands(self) -> list[dict[str, Any]]:
        """GET /api/v1/agent/commands/pending — Polling HTTPS command aktif untuk perangkat ini."""
        data = self._request("GET", "/api/v1/agent/commands/pending")
        cmds = data.get("commands", data.get("data", []))
        return cmds if isinstance(cmds, list) else []

    def acknowledge_command(
        self,
        command_id: str,
        status: str,
        result_message: str = "",
        result_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """POST /api/v1/agent/commands/{command_id}/ack — Mengirim acknowledgment & hasil eksekusi."""
        payload = {
            "status": status,
            "result_message": result_message,
            "result_payload": result_payload or {},
        }
        return self._request("POST", f"/api/v1/agent/commands/{command_id}/ack", json_body=payload)

    def fetch_configs(self, current_versions: dict[str, int] | None = None) -> list[dict[str, Any]]:
        """GET /api/v1/agent/configs — Mengambil konfigurasi runtime terbaru."""
        params = {}
        if current_versions:
            for k, v in current_versions.items():
                params[f"versions[{k}]"] = str(v)
        data = self._request("GET", "/api/v1/agent/configs", params=params or None)
        configs = data.get("configs", data.get("data", []))
        return configs if isinstance(configs, list) else []

    def report_service_status(self, service_key: str, status_payload: dict[str, Any]) -> dict[str, Any]:
        """POST /api/v1/agent/status — Pelaporan perubahan status granular tiap service."""
        payload = {
            "service_key": service_key,
            "current_status": status_payload.get("status", "Unknown"),
            "active_config_version": int(status_payload.get("active_config_version", 0)),
            "details": status_payload,
        }
        return self._request("POST", "/api/v1/agent/status", json_body=payload)

    def send_logs(self, entries: list[dict[str, Any]]) -> dict[str, Any]:
        """POST /api/v1/agent/logs — Mengirim batch log aktivitas & error lokal."""
        return self._request("POST", "/api/v1/agent/logs", json_body={"logs": entries})

    def check_latest_release(self, current_version: str) -> dict[str, Any]:
        """GET /api/v1/agent/releases/latest — Memeriksa pembaruan versi aplikasi."""
        return self._request("GET", "/api/v1/agent/releases/latest", params={"current_version": current_version})

    def unregister_self(self, reason: str = "user_requested") -> dict[str, Any]:
        """POST /api/v1/agent/unregister — Mencabut pendaftaran perangkat dari sisi lokal."""
        return self._request("POST", "/api/v1/agent/unregister", json_body={"reason": reason})

    def download_file(self, url_or_path: str, dest_path: str) -> str:
        """Mengunduh paket release melalui koneksi HTTPS keluar dengan Bearer token perangkat."""
        headers = {"Accept": "*/*"}
        if self.device_token:
            headers["Authorization"] = f"Bearer {self.device_token}"
        try:
            with self._client.stream("GET", url_or_path, headers=headers) as resp:
                if resp.status_code in (401, 403):
                    raise AgentAuthRevokedError("Akses unduhan release ditolak (401/403).", status_code=resp.status_code)
                if resp.status_code >= 500:
                    raise AgentNetworkError(f"Server error saat unduh release (HTTP {resp.status_code}).", status_code=resp.status_code)
                if resp.status_code >= 400:
                    raise AgentApiError(f"Gagal mengunduh release (HTTP {resp.status_code}).", status_code=resp.status_code)
                with open(dest_path, "wb") as out_f:
                    for chunk in resp.iter_bytes(chunk_size=65536):
                        if chunk:
                            out_f.write(chunk)
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            raise AgentNetworkError(f"Koneksi gagal saat mengunduh release: {exc}") from exc
        return dest_path
