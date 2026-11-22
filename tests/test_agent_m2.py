"""
Pengujian otomatis Milestone 2: LoveYou Device Agent (Enrollment, DPAPI Identity,
Command Executor Idempotency/TTL/Allowlist, Config Sync + Rollback, Realtime Wake-up,
Release Integrity Verifier, dan Orchestration Loop + Offline Recovery + Revocation).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from backend.agent import (
    AgentApiClient,
    AgentApiError,
    AgentAuthRevokedError,
    AgentNetworkError,
    CommandExecutor,
    ConfigSyncManager,
    DeviceEnrollmentManager,
    DeviceIdentityStore,
    LoveYouAgent,
    RealtimeCommandListener,
    ReleaseUpdater,
    compute_sha256,
    verify_hmac_signature,
    verify_sha256,
)
from backend.services import (
    STATUS_PAUSED,
    STATUS_RUNNING,
    STATUS_STOPPED,
    CustomService,
    GameService,
    NotificationService,
    ScreenshotService,
    ServiceManager,
    SyncService,
    TrayService,
    UiService,
)


def _make_service_manager() -> ServiceManager:
    mgr = ServiceManager()
    mgr.register(ScreenshotService(capture_fn=None))
    mgr.register(TrayService(tray_factory=None))
    mgr.register(SyncService(logger_factory=None))
    mgr.register(NotificationService())
    mgr.register(UiService())
    mgr.register(GameService())
    mgr.register(CustomService())
    return mgr


class MockXooseControlPlane:
    """Backend simulasi in-memory untuk memverifikasi seluruh 9 endpoint /api/v1/agent/*."""

    def __init__(self) -> None:
        self.expected_token = "tok_unique_device_secret_12345"
        self.device_id = "dev-uuid-001"
        self.pending_commands: list[dict[str, Any]] = []
        self.remote_configs: list[dict[str, Any]] = []
        self.latest_release: dict[str, Any] = {"version": "1.0.0"}
        self.revoked = False
        self.offline = False

        self.enroll_calls: list[dict[str, Any]] = []
        self.heartbeats: list[dict[str, Any]] = []
        self.acks: list[tuple[str, dict[str, Any]]] = []
        self.status_reports: list[dict[str, Any]] = []
        self.logs_received: list[dict[str, Any]] = []
        self.unregister_calls: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.offline:
            raise httpx.ConnectError("Simulated offline network")

        path = request.url.path
        method = request.method
        body = json.loads(request.content.decode("utf-8")) if request.content else {}

        if method == "POST" and path == "/api/v1/agent/enroll":
            self.enroll_calls.append(body)
            if body.get("enrollment_code") != "ENROLL-OK-2026":
                return httpx.Response(422, json={"message": "Enrollment code tidak valid"})
            return httpx.Response(
                200,
                json={
                    "device_id": self.device_id,
                    "device_code": "DEV-001",
                    "device_token": self.expected_token,
                },
            )

        # Semua endpoint selain /enroll wajib menyertakan Bearer token valid
        auth = request.headers.get("Authorization", "")
        if self.revoked or auth != f"Bearer {self.expected_token}":
            return httpx.Response(401, json={"message": "Device token revoked or unauthorized"})

        if method == "POST" and path == "/api/v1/agent/heartbeat":
            self.heartbeats.append(body)
            return httpx.Response(200, json={"ok": True})

        if method == "GET" and path == "/api/v1/agent/commands/pending":
            return httpx.Response(200, json={"commands": list(self.pending_commands)})

        if method == "POST" and path.startswith("/api/v1/agent/commands/") and path.endswith("/ack"):
            cmd_id = path.split("/")[5]
            self.acks.append((cmd_id, body))
            return httpx.Response(200, json={"ok": True})

        if method == "GET" and path == "/api/v1/agent/configs":
            return httpx.Response(200, json={"configs": list(self.remote_configs)})

        if method == "POST" and path == "/api/v1/agent/status":
            self.status_reports.append(body)
            return httpx.Response(200, json={"ok": True})

        if method == "POST" and path == "/api/v1/agent/logs":
            self.logs_received.extend(body.get("logs", []))
            return httpx.Response(200, json={"ok": True})

        if method == "GET" and path == "/api/v1/agent/releases/latest":
            return httpx.Response(200, json={"release": self.latest_release})

        if method == "POST" and path == "/api/v1/agent/unregister":
            self.unregister_calls.append(body)
            self.revoked = True
            return httpx.Response(200, json={"ok": True})

        return httpx.Response(404, json={"message": f"Unknown route {method} {path}"})


def test_identity_store_dpapi_encryption_and_persistent_ledger(tmp_path: Path) -> None:
    store = DeviceIdentityStore(app_slug="loveyou-test", storage_dir=tmp_path / "agent_state")
    first_uuid = store.device_uuid
    assert first_uuid and len(first_uuid) >= 32
    assert store.is_enrolled() is False

    secret_token = "super_secret_device_token_abc999"
    store.save_enrollment(device_id="dev-123", device_token=secret_token, device_code="DEV-123")
    assert store.is_enrolled() is True
    assert store.get_device_token() == secret_token

    # Pastikan token plaintext TIDAK tersimpan apa adanya pada file identity.json
    raw_json = store.identity_file.read_text(encoding="utf-8")
    assert secret_token not in raw_json
    if os.name == "nt":
        assert '"dpapi:' in raw_json

    # Simpan ledger command dan config_version lalu reload store baru
    store.record_processed_command("cmd-001", {"status": "succeeded", "message": "done"})
    store.set_config_version("screenshot_service", 5)

    reloaded = DeviceIdentityStore(app_slug="loveyou-test", storage_dir=tmp_path / "agent_state")
    assert reloaded.device_uuid == first_uuid
    assert reloaded.device_id == "dev-123"
    assert reloaded.get_device_token() == secret_token
    assert reloaded.has_processed_command("cmd-001") is True
    assert reloaded.get_config_versions()["screenshot_service"] == 5

    # Revoke menghapus token dan menandai revoked=True
    reloaded.mark_revoked()
    assert reloaded.is_revoked is True
    assert reloaded.is_enrolled() is False
    assert reloaded.get_device_token() == ""

    # clear_all menghapus direktori penyimpanan
    reloaded.clear_all()
    assert not (tmp_path / "agent_state").exists()


def test_enrollment_flow_offline_and_unregister(tmp_path: Path) -> None:
    mock_cp = MockXooseControlPlane()
    client = AgentApiClient("https://xoose.local", transport=httpx.MockTransport(mock_cp.handler))
    identity = DeviceIdentityStore(storage_dir=tmp_path / "id_enroll")
    enrollment = DeviceEnrollmentManager(
        identity_store=identity,
        api_client=client,
        app_slug="loveyou",
        app_version="1.0.0",
        enrollment_code="ENROLL-OK-2026",
    )

    # Saat offline, enrollment ditunda tanpa crash
    mock_cp.offline = True
    ok_off, msg_off = enrollment.ensure_enrolled()
    assert ok_off is False
    assert "offline" in msg_off.lower()
    assert identity.is_enrolled() is False

    # Saat online, enrollment berhasil tanpa login admin di LoveYou
    mock_cp.offline = False
    ok_on, _ = enrollment.ensure_enrolled()
    assert ok_on is True
    assert identity.is_enrolled() is True
    assert identity.device_id == mock_cp.device_id
    assert len(mock_cp.enroll_calls) == 1

    # Pemanggilan kedua tidak melakukan enroll ulang
    ok_again, _ = enrollment.ensure_enrolled()
    assert ok_again is True
    assert len(mock_cp.enroll_calls) == 1

    # Unregister lokal memberi tahu server dan mencabut token lokal
    unreg_ok, _ = enrollment.unregister_local(reason="user_uninstall")
    assert unreg_ok is True
    assert len(mock_cp.unregister_calls) == 1
    assert identity.is_revoked is True
    assert identity.get_device_token() == ""
    client.close()


def test_command_executor_stop_idempotency_ttl_target_and_allowlist(tmp_path: Path) -> None:
    mock_cp = MockXooseControlPlane()
    client = AgentApiClient(
        "https://xoose.local",
        device_token=mock_cp.expected_token,
        transport=httpx.MockTransport(mock_cp.handler),
    )
    identity = DeviceIdentityStore(storage_dir=tmp_path / "id_cmd")
    identity.save_enrollment(mock_cp.device_id, mock_cp.expected_token, "DEV-001")
    mgr = _make_service_manager()
    mgr.execute_action("screenshot_service", "start")
    assert mgr.get("screenshot_service").current_status == STATUS_RUNNING

    executor = CommandExecutor(service_manager=mgr, identity_store=identity, api_client=client)
    future_iso = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    past_iso = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()

    # 1. Command dengan target device_id salah -> ditolak, service tetap Running
    res_wrong_dev = executor.execute_command(
        {
            "command_id": "cmd-wrong-dev",
            "device_id": "other-device-999",
            "service": "screenshot_service",
            "action": "stop",
            "expires_at": future_iso,
        }
    )
    assert res_wrong_dev["ok"] is False
    assert res_wrong_dev["status"] == "failed"
    assert mgr.get("screenshot_service").current_status == STATUS_RUNNING

    # 2. Command expired -> ditolak dengan status 'expired', service tetap Running
    res_expired = executor.execute_command(
        {
            "command_id": "cmd-expired-1",
            "device_id": mock_cp.device_id,
            "service": "screenshot_service",
            "action": "stop",
            "expires_at": past_iso,
        }
    )
    assert res_expired["ok"] is False
    assert res_expired["status"] == "expired"
    assert mgr.get("screenshot_service").current_status == STATUS_RUNNING
    assert ("cmd-expired-1", "expired") in [(cid, b["status"]) for cid, b in mock_cp.acks]

    # 3. Command arbitrary shell / exec -> ditolak keras
    for bad_action in ("shell", "exec", "powershell", "cmd"):
        res_bad = executor.execute_command(
            {
                "command_id": f"cmd-bad-{bad_action}",
                "device_id": mock_cp.device_id,
                "service": "screenshot_service",
                "action": bad_action,
                "expires_at": future_iso,
            }
        )
        assert res_bad["ok"] is False
        assert res_bad["status"] == "failed"
    assert mgr.get("screenshot_service").current_status == STATUS_RUNNING

    # 4. Command STOP valid -> ack 'acknowledged' -> stop graceful -> ack 'succeeded' + report status
    res_stop = executor.execute_command(
        {
            "command_id": "cmd-stop-001",
            "device_id": mock_cp.device_id,
            "service": "screenshot_service",
            "action": "stop",
            "expires_at": future_iso,
        }
    )
    assert res_stop["ok"] is True
    assert res_stop["status"] == "succeeded"
    assert mgr.get("screenshot_service").current_status == STATUS_STOPPED
    stop_ack_statuses = [b["status"] for cid, b in mock_cp.acks if cid == "cmd-stop-001"]
    assert stop_ack_statuses == ["acknowledged", "succeeded"]
    assert any(r["service_key"] == "screenshot_service" and r["current_status"] == STATUS_STOPPED for r in mock_cp.status_reports)

    # 5. Idempotency: ulangi command_id sama setelah service dijalankan lagi -> diabaikan, service tetap Running
    mgr.execute_action("screenshot_service", "start")
    assert mgr.get("screenshot_service").current_status == STATUS_RUNNING
    res_dup = executor.execute_command(
        {
            "command_id": "cmd-stop-001",
            "device_id": mock_cp.device_id,
            "service": "screenshot_service",
            "action": "stop",
            "expires_at": future_iso,
        }
    )
    assert res_dup["duplicate"] is True
    assert mgr.get("screenshot_service").current_status == STATUS_RUNNING

    # 6. Command apply_config via CommandExecutor
    res_cfg = executor.execute_command(
        {
            "command_id": "cmd-cfg-001",
            "device_id": mock_cp.device_id,
            "service": "screenshot_service",
            "action": "apply_config",
            "payload": {"config_version": 3, "config_json": {"interval_seconds": 15}},
            "expires_at": future_iso,
        }
    )
    assert res_cfg["ok"] is True
    assert mgr.get("screenshot_service").config["interval_seconds"] == 15
    assert identity.get_config_versions()["screenshot_service"] == 3

    mgr.stop_all()
    client.close()


def test_config_sync_versioning_and_rollback(tmp_path: Path) -> None:
    mock_cp = MockXooseControlPlane()
    client = AgentApiClient(
        "https://xoose.local",
        device_token=mock_cp.expected_token,
        transport=httpx.MockTransport(mock_cp.handler),
    )
    identity = DeviceIdentityStore(storage_dir=tmp_path / "id_cfg")
    identity.save_enrollment(mock_cp.device_id, mock_cp.expected_token, "DEV-001")
    mgr = _make_service_manager()
    sync_mgr = ConfigSyncManager(service_manager=mgr, identity_store=identity, api_client=client)

    # Versi 2 valid diterapkan
    mock_cp.remote_configs = [
        {"service_key": "screenshot_service", "config_version": 2, "config_json": {"interval_seconds": 8}},
    ]
    res1 = sync_mgr.sync_once()
    assert len(res1) == 1 and res1[0]["ok"] is True
    assert mgr.get("screenshot_service").config["interval_seconds"] == 8
    assert identity.get_config_versions()["screenshot_service"] == 2

    # Versi sama (2) diabaikan
    res2 = sync_mgr.sync_once()
    assert res2 == []

    # Versi 3 tidak valid -> ditolak, config tetap di versi 2 (interval 8)
    mock_cp.remote_configs = [
        {"service_key": "screenshot_service", "config_version": 3, "config_json": {"interval_seconds": -5}},
    ]
    res3 = sync_mgr.sync_once()
    assert len(res3) == 1 and res3[0]["ok"] is False
    assert mgr.get("screenshot_service").config["interval_seconds"] == 8
    assert identity.get_config_versions()["screenshot_service"] == 2

    # Rollback eksplisit dari admin (force_apply=True ke versi 1)
    mock_cp.remote_configs = [
        {
            "service_key": "screenshot_service",
            "config_version": 1,
            "config_json": {"interval_seconds": 3},
            "force_apply": True,
        }
    ]
    res4 = sync_mgr.sync_once()
    assert len(res4) == 1 and res4[0]["ok"] is True
    assert mgr.get("screenshot_service").config["interval_seconds"] == 3
    assert identity.get_config_versions()["screenshot_service"] == 1
    client.close()


def test_realtime_listener_wake_signal_filtering() -> None:
    trigger = threading.Event()
    received: list[dict[str, Any]] = []
    listener = RealtimeCommandListener(
        device_id="dev-uuid-001",
        poll_trigger_event=trigger,
        supabase_url="https://example.supabase.co",
        supabase_key="anon-key",
        enabled=True,
        on_event_received=lambda p: received.append(p),
    )
    assert listener.start() is True

    # Sinyal untuk device lain harus diabaikan
    listener.handle_wake_signal({"device_id": "dev-uuid-999", "event": "new_command"})
    assert trigger.is_set() is False

    # Sinyal untuk device ini memicu poll_trigger_event
    listener.handle_wake_signal({"device_id": "dev-uuid-001", "event": "new_command"})
    assert trigger.is_set() is True
    assert len(received) == 1
    listener.stop()
    assert listener.is_running is False


def test_release_updater_sha256_and_hmac_verification(tmp_path: Path) -> None:
    mock_cp = MockXooseControlPlane()
    verify_key = "release-signing-key-2026"
    pkg_file = tmp_path / "LoveYou-1.2.0.zip"
    pkg_file.write_bytes(b"LOVEYOU_RELEASE_PACKAGE_BINARY_DATA")
    digest = compute_sha256(pkg_file)
    sig = hmac.new(verify_key.encode("utf-8"), digest.encode("utf-8"), hashlib.sha256).hexdigest()

    mock_cp.latest_release = {
        "version": "1.2.0",
        "artifact_url": "https://xoose.local/releases/LoveYou-1.2.0.zip",
        "sha256_checksum": digest,
        "signature": sig,
        "release_notes": "Modular service update",
    }

    client = AgentApiClient(
        "https://xoose.local",
        device_token=mock_cp.expected_token,
        transport=httpx.MockTransport(mock_cp.handler),
    )
    updater = ReleaseUpdater(api_client=client, current_version="1.0.0", verify_key=verify_key)

    info = updater.check_update()
    assert info["update_available"] is True
    assert info["latest_version"] == "1.2.0"

    assert verify_sha256(pkg_file, digest) is True
    assert verify_hmac_signature(digest, sig, verify_key) is True

    ok, _ = updater.verify_package(pkg_file, digest, sig)
    assert ok is True

    # Signature salah ditolak
    ok_bad_sig, _ = updater.verify_package(pkg_file, digest, "0" * 64)
    assert ok_bad_sig is False

    # File dimodifikasi (tampered) -> checksum gagal dan ditolak
    pkg_file.write_bytes(b"TAMPERED_DATA")
    ok_tampered, _ = updater.verify_package(pkg_file, digest, sig)
    assert ok_tampered is False
    client.close()


def test_loveyou_agent_full_cycle_offline_recovery_and_revocation(tmp_path: Path) -> None:
    mock_cp = MockXooseControlPlane()
    client = AgentApiClient("https://xoose.local", transport=httpx.MockTransport(mock_cp.handler))
    mgr = _make_service_manager()
    mgr.execute_action("screenshot_service", "start")
    assert mgr.get("screenshot_service").current_status == STATUS_RUNNING

    future_iso = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    mock_cp.pending_commands = [
        {
            "command_id": "cmd-pause-100",
            "device_id": mock_cp.device_id,
            "service": "screenshot_service",
            "action": "pause",
            "expires_at": future_iso,
        }
    ]
    mock_cp.remote_configs = [
        {"service_key": "screenshot_service", "config_version": 2, "config_json": {"interval_seconds": 10}}
    ]

    agent = LoveYouAgent(
        service_manager=mgr,
        api_base_url="https://xoose.local",
        app_slug="loveyou",
        app_version="1.0.0",
        enrollment_code="ENROLL-OK-2026",
        storage_dir=str(tmp_path / "agent_runtime"),
        api_client=client,
    )

    # Siklus 1: Enroll -> Heartbeat -> Command PAUSE -> Config Sync v2 -> Flush Logs
    summary1 = agent.run_cycle(force_all=True)
    assert summary1["enrolled"] is True
    assert summary1["online"] is True
    assert summary1["heartbeat_sent"] is True
    assert mgr.get("screenshot_service").current_status == STATUS_PAUSED
    assert mgr.get("screenshot_service").config["interval_seconds"] == 10
    assert summary1["logs_flushed"] >= 2
    assert len(mock_cp.heartbeats) == 1
    assert len(mock_cp.logs_received) >= 2

    # Siklus 2: Jaringan offline -> backoff naik, log ditahan di buffer
    mock_cp.offline = True
    agent.buffer_log("screenshot_service", "warning", "Buffer offline log")
    summary_off = agent.run_cycle(force_all=True)
    assert summary_off["online"] is False
    assert agent._consecutive_failures == 1
    assert agent._compute_backoff_wait() >= 2.0

    # Siklus 3: Kembali online -> reconnect berhasil & log tertunda terkirim
    mock_cp.offline = False
    mock_cp.pending_commands = []
    summary_back = agent.run_cycle(force_all=True)
    assert summary_back["online"] is True
    assert agent._consecutive_failures == 0
    assert any(l["message"] == "Buffer offline log" for l in mock_cp.logs_received)

    # Siklus 4: Admin mencabut token perangkat (HTTP 401) -> Agent mark revoked & berhenti kirim traffic
    mock_cp.revoked = True
    summary_rev = agent.run_cycle(force_all=True)
    assert summary_rev["revoked"] is True
    assert agent.identity.is_revoked is True
    assert agent.identity.get_device_token() == ""

    # Siklus 5 setelah revoked tidak memanggil server lagi
    hb_before = len(mock_cp.heartbeats)
    summary_after = agent.run_cycle(force_all=True)
    assert summary_after["revoked"] is True
    assert len(mock_cp.heartbeats) == hb_before

    agent.stop()
    mgr.stop_all()
