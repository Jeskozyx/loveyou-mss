"""
CommandExecutor: Validator & eksekutor remote command untuk LoveYou Agent.
Menegakkan:
1. Validasi target device_id
2. Validasi masa berlaku (expires_at)
3. Idempotency persisten berbasis command_id (mencegah eksekusi ganda)
4. Allowlist aksi ketat (dilarang keras arbitrary shell / eksekusi kode bebas)
5. Siklus status: acknowledged -> succeeded / failed / expired
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

try:
    from backend.agent.api_client import AgentApiClient, AgentNetworkError
    from backend.agent.identity import DeviceIdentityStore
    from backend.services.base import ALLOWED_LIFECYCLE_ACTIONS
    from backend.services.manager import ServiceManager
except ImportError:
    from agent.api_client import AgentApiClient, AgentNetworkError
    from agent.identity import DeviceIdentityStore
    from services.base import ALLOWED_LIFECYCLE_ACTIONS
    from services.manager import ServiceManager


ALLOWED_COMMAND_ACTIONS = frozenset(ALLOWED_LIFECYCLE_ACTIONS | {"apply_config"})


def _parse_utc_iso(ts: str) -> datetime | None:
    if not ts or not isinstance(ts, str):
        return None
    s = ts.strip()
    try:
        if s.endswith("Z") or s.endswith("z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


class CommandExecutor:
    """Memvalidasi dan mengeksekusi command remote secara aman, idempotent, dan teraudit."""

    def __init__(
        self,
        service_manager: ServiceManager,
        identity_store: DeviceIdentityStore,
        api_client: AgentApiClient | None = None,
        on_log: Callable[[str, str, str, dict[str, Any] | None], None] | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ):
        self.service_manager = service_manager
        self.identity = identity_store
        self.api_client = api_client
        self._on_log = on_log
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))

    def _emit_log(self, service_key: str, level: str, message: str, context: dict[str, Any] | None = None) -> None:
        if self._on_log:
            try:
                self._on_log(service_key, level, message, context)
            except Exception:
                pass

    def _safe_ack(self, command_id: str, status: str, message: str, payload: dict[str, Any] | None = None) -> None:
        if self.api_client is None:
            return
        try:
            self.api_client.acknowledge_command(
                command_id=command_id,
                status=status,
                result_message=message,
                result_payload=payload,
            )
        except AgentNetworkError:
            pass
        except Exception:
            pass

    def execute_command(self, command: dict[str, Any]) -> dict[str, Any]:
        """
        Memvalidasi dan menjalankan satu payload command dari Control Plane.
        Format payload standar:
        {
            "command_id": "uuid-v4",
            "device_id": "uuid-device",
            "service": "screenshot_service",  # atau "service_key"
            "action": "stop",
            "payload": {},
            "expires_at": "2026-11-23T03:51:00Z"
        }
        """
        command_id = str(command.get("command_id", "")).strip()
        target_device_id = str(command.get("device_id", "")).strip()
        service_key = str(command.get("service_key") or command.get("service") or "").strip()
        action = str(command.get("action", "")).strip().lower()
        payload = command.get("payload") if isinstance(command.get("payload"), dict) else {}
        expires_at_raw = str(command.get("expires_at", "")).strip()

        if not command_id:
            return {"ok": False, "status": "failed", "message": "command_id tidak boleh kosong."}

        # 1. Cek Idempotency: jangan pernah eksekusi ulang command_id yang sudah diproses
        if self.identity.has_processed_command(command_id):
            prev = self.identity.get_processed_command(command_id) or {}
            prev_status = str(prev.get("status", "succeeded"))
            msg = f"Command duplikat '{command_id}' diabaikan (idempotent; status sebelumnya: {prev_status})."
            # Kirim ulang ack idempotent agar server menutup command jika ack sebelumnya sempat hilang saat offline
            if prev_status in ("succeeded", "failed", "expired"):
                self._safe_ack(command_id, prev_status, msg)
            self._emit_log(service_key or "agent", "info", msg, {"command_id": command_id})
            return {
                "ok": True,
                "command_id": command_id,
                "status": prev_status,
                "duplicate": True,
                "message": msg,
            }

        # 2. Validasi target device_id
        expected_device_id = self.identity.device_id
        if expected_device_id and target_device_id != expected_device_id:
            msg = f"Target device_id tidak cocok (diterima='{target_device_id}', lokal='{expected_device_id}')."
            self._safe_ack(command_id, "failed", msg)
            self._emit_log(service_key or "agent", "warning", msg, {"command_id": command_id})
            return {"ok": False, "command_id": command_id, "status": "failed", "message": msg}

        # 3. Validasi masa berlaku (expires_at)
        if expires_at_raw:
            exp_dt = _parse_utc_iso(expires_at_raw)
            if exp_dt is None:
                msg = f"Format expires_at tidak valid: '{expires_at_raw}'."
                self._safe_ack(command_id, "failed", msg)
                return {"ok": False, "command_id": command_id, "status": "failed", "message": msg}
            if self._now_fn() > exp_dt:
                msg = f"Command '{command_id}' sudah kedaluwarsa pada {expires_at_raw}."
                result = {"ok": False, "command_id": command_id, "status": "expired", "message": msg}
                self.identity.record_processed_command(command_id, result)
                self._safe_ack(command_id, "expired", msg)
                self._emit_log(service_key or "agent", "warning", msg, {"command_id": command_id})
                return result

        # 4. Validasi allowlist aksi (tolak keras shell / exec / arbitrary code)
        if action not in ALLOWED_COMMAND_ACTIONS:
            msg = f"Aksi '{action}' ditolak karena tidak termasuk allowlist command."
            result = {"ok": False, "command_id": command_id, "status": "failed", "message": msg}
            self.identity.record_processed_command(command_id, result)
            self._safe_ack(command_id, "failed", msg)
            self._emit_log(service_key or "agent", "error", msg, {"command_id": command_id, "action": action})
            return result

        # 5. Kirim acknowledged sebelum mengeksekusi
        self._safe_ack(command_id, "acknowledged", f"Mengeksekusi '{action}' pada '{service_key}'.")

        # 6. Eksekusi melalui ServiceManager
        if action == "apply_config":
            cfg_data = payload.get("config_json", payload.get("config", {}))
            cfg_ver = int(payload.get("config_version", 0))
            exec_res = self.service_manager.apply_service_config(service_key, cfg_data, cfg_ver)
            if exec_res.get("ok"):
                self.identity.set_config_version(service_key, cfg_ver)
        else:
            exec_res = self.service_manager.execute_action(service_key, action)

        final_status = "succeeded" if exec_res.get("ok") else "failed"
        final_message = exec_res.get("message", "")
        result = {
            "ok": bool(exec_res.get("ok")),
            "command_id": command_id,
            "service_key": service_key,
            "action": action,
            "status": final_status,
            "service_status": exec_res.get("status", "Unknown"),
            "message": final_message,
        }

        # 7. Simpan idempotency ledger, kirim ack akhir, dan laporkan status service terkini
        self.identity.record_processed_command(command_id, result)
        self._safe_ack(command_id, final_status, final_message, payload=exec_res)

        if self.api_client is not None:
            svc = self.service_manager.get(service_key)
            if svc is not None:
                try:
                    self.api_client.report_service_status(service_key, svc.get_status())
                except Exception:
                    pass

        self._emit_log(
            service_key or "agent",
            "info" if result["ok"] else "error",
            f"Command {command_id} ({action}): {final_status} — {final_message}",
            {"command_id": command_id, "action": action, "service_status": result["service_status"]},
        )
        return result
