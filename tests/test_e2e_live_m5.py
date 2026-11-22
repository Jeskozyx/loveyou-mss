"""
Milestone 5 — Pengujian End-to-End Nyata (Real HTTP):
LoveYou Python Agent + Modular ServiceManager (D:/KULIAH/KODINGAN/screenshotusingpy)
berkomunikasi langsung melalui TCP/HTTP nyata dengan server Laravel Xoose
(D:/laragon/www/xoose) dan SQLite terisolasi (tanpa mock backend).

Skenario yang diuji secara nyata:
1. Admin login Xoose -> buat ManagedApp & kode enrollment -> LoveYou Agent enroll (DPAPI token)
   -> heartbeat -> status perangkat Online di Xoose.
2. Admin kirim perintah STOP ke screenshot_service -> Agent poll, validasi, graceful stop,
   kirim ack (acknowledged -> succeeded) & report status -> Xoose menampilkan Stopped.
3. Admin kirim START + ubah konfigurasi runtime v1 -> v2 -> Rollback v1 (v3 force_apply)
   + verifikasi ReleaseUpdater terhadap /api/v1/agent/releases/latest.
4. Command kedaluwarsa (expired di sisi server & sisi Agent) -> ditolak tanpa mengubah service.
5. Command duplikat (idempotency lintas restart DeviceIdentityStore) -> tidak dieksekusi ganda.
6. Simulasi Offline (server HTTP dimatikan total) -> Agent bertahan + buffer log + backoff
   -> Reconnect otomatis saat server hidup kembali + log terkirim.
7. Revocation remote dari Admin Xoose -> Agent menerima HTTP 401 nyata, mencabut token DPAPI,
   dan menghentikan lalu lintas remote secara bersih.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import socket
import sqlite3
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from backend.agent import DeviceIdentityStore, LoveYouAgent
from backend.services import (
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

XOOSE_DIR = Path(os.environ.get("XOOSE_LARAVEL_DIR", r"D:\laragon\www\xoose"))
SERVER_ROUTER = XOOSE_DIR / "vendor" / "laravel" / "framework" / "src" / "Illuminate" / "Foundation" / "resources" / "server.php"
PHP_BIN = shutil.which("php")

pytestmark = pytest.mark.skipif(
    not (PHP_BIN and XOOSE_DIR.exists() and SERVER_ROUTER.exists()),
    reason="Server Laravel Xoose atau binary PHP tidak tersedia di mesin ini.",
)


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class LiveXooseServer:
    """Mengelola instance server HTTP Laravel Xoose nyata dengan database SQLite terisolasi."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.port = _find_free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.proc: subprocess.Popen[str] | None = None

        self.env = os.environ.copy()
        self.env.update(
            {
                "APP_ENV": "local",
                "APP_DEBUG": "true",
                "APP_URL": self.base_url,
                "DB_CONNECTION": "sqlite",
                "DB_DATABASE": str(self.db_path),
                "QUEUE_CONNECTION": "sync",
                "SESSION_DRIVER": "database",
                "CACHE_STORE": "array",
                "XOOSE_ADMIN_EMAIL": "admin@xoose.local",
                "XOOSE_ADMIN_PASSWORD": "password123",
            }
        )

    def prepare_database(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path.touch(exist_ok=True)
        for cmd in (
            [PHP_BIN, "artisan", "migrate", "--force"],
            [PHP_BIN, "artisan", "db:seed", "--class=ServiceDefinitionSeeder", "--force"],
            [PHP_BIN, "artisan", "db:seed", "--class=AdminUserSeeder", "--force"],
        ):
            res = subprocess.run(
                cmd,
                cwd=str(XOOSE_DIR),
                env=self.env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert res.returncode == 0, f"Gagal menjalankan {' '.join(cmd)}:\n{res.stdout}\n{res.stderr}"

    def start(self) -> None:
        if self.proc and self.proc.poll() is None:
            return
        self.proc = subprocess.Popen(
            [PHP_BIN, "-S", f"127.0.0.1:{self.port}", str(SERVER_ROUTER)],
            cwd=str(XOOSE_DIR / "public"),
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        deadline = time.time() + 15.0
        while time.time() < deadline:
            try:
                r = httpx.get(f"{self.base_url}/up", timeout=1.0)
                if r.status_code == 200:
                    return
            except Exception:
                time.sleep(0.15)
        raise RuntimeError(f"Server Laravel Xoose gagal siap di {self.base_url}")

    def stop(self) -> None:
        if self.proc is not None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5.0)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None


class AdminHttpSession:
    """Klien HTTP nyata yang masuk sebagai Admin Xoose via /login (cookie + CSRF)."""

    def __init__(self, base_url: str) -> None:
        self.client = httpx.Client(base_url=base_url, timeout=15.0, follow_redirects=False)
        self.csrf_token = ""

    def login(self, email: str = "admin@xoose.local", password: str = "password123") -> None:
        page = self.client.get("/login")
        assert page.status_code == 200
        m = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', page.text)
        assert m is not None, "CSRF meta token tidak ditemukan pada halaman /login"
        self.csrf_token = m.group(1)

        resp = self.client.post(
            "/login",
            data={"_token": self.csrf_token, "email": email, "password": password},
        )
        assert resp.status_code in (302, 303), f"Login admin gagal: HTTP {resp.status_code} {resp.text}"

        # Perbarui CSRF token setelah regenerasi sesi pasca login
        dash = self.client.get("/remote/apps")
        assert dash.status_code == 200
        m2 = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', dash.text)
        assert m2 is not None
        self.csrf_token = m2.group(1)

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "X-CSRF-TOKEN": self.csrf_token,
            "X-Requested-With": "XMLHttpRequest",
        }

    def get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        return self.client.get(path, params=params, headers=self._headers())

    def post(self, path: str, json_body: dict[str, Any] | None = None) -> httpx.Response:
        return self.client.post(path, json=json_body or {}, headers=self._headers())

    def put(self, path: str, json_body: dict[str, Any] | None = None) -> httpx.Response:
        return self.client.put(path, json=json_body or {}, headers=self._headers())

    def close(self) -> None:
        self.client.close()


def _build_live_service_manager(tmp_path: Path) -> ServiceManager:
    mgr = ServiceManager()
    counter = {"n": 0}

    def safe_capture(output_dir: str, image_format: str, jpg_quality: int) -> tuple[str, str, str]:
        counter["n"] += 1
        fname = f"e2e_ss_{counter['n']}.{image_format}"
        fpath = os.path.join(output_dir, fname)
        Path(fpath).write_bytes(b"E2E_FRAME")
        return fpath, fname, "1920x1080"

    mgr.register(
        ScreenshotService(
            initial_config={
                "interval_seconds": 30,
                "image_format": "webp",
                "jpg_quality": 75,
                "keep_local_file": False,
                "local_folder": str(tmp_path / "screenshots"),
            },
            capture_fn=safe_capture,
            active_window_fn=lambda: "LoveYou E2E Window",
            get_user_fn=lambda: "e2e_user",
        )
    )
    mgr.register(TrayService(tray_factory=None))
    mgr.register(SyncService(logger_factory=None))
    mgr.register(NotificationService())
    mgr.register(UiService())
    mgr.register(GameService())
    mgr.register(CustomService())
    return mgr


@pytest.fixture()
def live_xoose(tmp_path: Path):
    db_path = tmp_path / "e2e_xoose.sqlite"
    server = LiveXooseServer(db_path=db_path)
    server.prepare_database()
    server.start()
    try:
        yield server
    finally:
        server.stop()


def test_e2e_enroll_heartbeat_stop_ack_config_rollback_and_release(live_xoose: LiveXooseServer, tmp_path: Path) -> None:
    admin = AdminHttpSession(live_xoose.base_url)
    mgr = _build_live_service_manager(tmp_path)
    agent: LoveYouAgent | None = None

    try:
        # 1. Admin login & buat ManagedApp + kode enrollment nyata di server Laravel
        admin.login()
        app_resp = admin.post("/api/v1/admin/apps", {"name": "LoveYou E2E App", "slug": "loveyou-e2e"})
        assert app_resp.status_code == 201, app_resp.text
        app_id = app_resp.json()["app"]["id"]

        enr_resp = admin.post(f"/api/v1/admin/apps/{app_id}/enrollments", {"label": "E2E Windows Device", "max_uses": 2})
        assert enr_resp.status_code == 201, enr_resp.text
        enrollment_code = enr_resp.json()["enrollment"]["enrollment_code"]

        # 2. Jalankan ScreenshotService & hubungkan LoveYouAgent (tanpa login admin di klien)
        mgr.execute_action("screenshot_service", "start")
        assert mgr.get("screenshot_service").current_status == STATUS_RUNNING

        agent_dir = tmp_path / "agent_e2e_1"
        agent = LoveYouAgent(
            service_manager=mgr,
            api_base_url=live_xoose.base_url,
            app_slug="loveyou-e2e",
            app_version="1.0.0",
            enrollment_code=enrollment_code,
            storage_dir=str(agent_dir),
        )

        # Siklus 1: Enroll + Heartbeat nyata
        c1 = agent.run_cycle(force_all=True)
        assert c1["enrolled"] is True, c1
        assert c1["online"] is True, c1
        assert c1["heartbeat_sent"] is True

        # Verifikasi token tersimpan terenkripsi DPAPI pada disk (bukan plaintext)
        raw_token = agent.identity.get_device_token()
        assert raw_token.startswith("xly_dev_")
        raw_identity_file = agent.identity.identity_file.read_text(encoding="utf-8")
        assert raw_token not in raw_identity_file
        if os.name == "nt":
            assert '"dpapi:' in raw_identity_file

        # Verifikasi perangkat tampil Online pada Xoose Admin API & Blade Dashboard
        devices_resp = admin.get("/api/v1/admin/devices")
        assert devices_resp.status_code == 200
        devices = devices_resp.json()["devices"]
        assert len(devices) == 1
        device = devices[0]
        device_db_id = device["id"]
        assert device["status"] == "Online"
        assert device["hostname"] == (platform.node() or "Unknown")

        web_devices_page = admin.client.get("/remote/devices")
        assert web_devices_page.status_code == 200
        assert device["device_code"] in web_devices_page.text
        assert "Online" in web_devices_page.text

        # 3. Admin mengirim perintah STOP ke screenshot_service via real HTTP
        stop_resp = admin.post(
            f"/api/v1/admin/devices/{device_db_id}/commands",
            {"service_key": "screenshot_service", "action": "stop", "ttl_seconds": 120},
        )
        assert stop_resp.status_code == 201, stop_resp.text
        stop_cmd_id = stop_resp.json()["command"]["command_id"]

        # Agent melakukan polling & eksekusi STOP secara normal (graceful stop) + mengirim ack
        c2 = agent.run_cycle(force_all=True)
        assert c2["online"] is True
        assert len(c2["commands_processed"]) == 1
        assert c2["commands_processed"][0]["command_id"] == stop_cmd_id
        assert c2["commands_processed"][0]["status"] == "succeeded"

        # Verifikasi status Stopped di sisi lokal DAN di sisi server Xoose
        assert mgr.get("screenshot_service").current_status == STATUS_STOPPED

        detail_resp = admin.get(f"/api/v1/admin/devices/{device_db_id}")
        assert detail_resp.status_code == 200
        dev_detail = detail_resp.json()["device"]
        ss_statuses = [s for s in dev_detail["service_statuses"] if s["service_key"] == "screenshot_service"]
        assert ss_statuses and ss_statuses[0]["current_status"] == "Stopped"

        cmd_records = [c for c in dev_detail["commands"] if c["command_id"] == stop_cmd_id]
        assert cmd_records and cmd_records[0]["status"] == "succeeded"
        assert cmd_records[0]["acknowledged_at"] is not None

        # 4. Admin mengirim START -> kembali Running, lalu ubah konfigurasi runtime v1 -> v2 -> rollback v1 (v3)
        start_resp = admin.post(
            f"/api/v1/admin/devices/{device_db_id}/commands",
            {"service_key": "screenshot_service", "action": "start"},
        )
        assert start_resp.status_code == 201
        agent.run_cycle(force_all=True)
        assert mgr.get("screenshot_service").current_status == STATUS_RUNNING

        cfg1 = admin.put(
            f"/api/v1/admin/configs/{app_id}/screenshot_service",
            {"config_json": {"interval_seconds": 11, "jpg_quality": 85}, "change_notes": "E2E v1"},
        )
        assert cfg1.status_code == 201
        agent.run_cycle(force_all=True)
        assert mgr.get("screenshot_service").config["interval_seconds"] == 11
        assert mgr.get("screenshot_service").active_config_version == 1

        cfg2 = admin.put(
            f"/api/v1/admin/configs/{app_id}/screenshot_service",
            {"config_json": {"interval_seconds": 25}, "change_notes": "E2E v2"},
        )
        assert cfg2.status_code == 201
        rb = admin.post(f"/api/v1/admin/configs/{app_id}/screenshot_service/rollback/1")
        assert rb.status_code == 200
        assert rb.json()["config"]["config_version"] == 3

        agent.run_cycle(force_all=True)
        assert mgr.get("screenshot_service").config["interval_seconds"] == 11
        assert mgr.get("screenshot_service").active_config_version == 3

        # 5. Verifikasi ReleaseUpdater nyata saat belum ada release & sesudah release v1.2.0 diterbitkan
        no_rel = agent.updater.check_update()
        assert no_rel["update_available"] is False

        checksum = hashlib.sha256(b"LOVEYOU_E2E_BINARY_V120").hexdigest()
        rel_resp = admin.post(
            f"/api/v1/admin/apps/{app_id}/releases",
            {
                "version": "1.2.0",
                "artifact_path": "builds/loveyou-e2e/1.2.0/LoveYou.exe",
                "sha256_checksum": checksum,
                "release_notes": "Live E2E release",
            },
        )
        assert rel_resp.status_code == 201
        has_rel = agent.updater.check_update()
        assert has_rel["update_available"] is True
        assert has_rel["latest_version"] == "1.2.0"
        assert has_rel["sha256_checksum"] == checksum
        assert has_rel["signature"]
    finally:
        if agent is not None:
            agent.stop()
        mgr.stop_all()
        admin.close()


def test_e2e_expired_command_and_duplicate_idempotency(live_xoose: LiveXooseServer, tmp_path: Path) -> None:
    admin = AdminHttpSession(live_xoose.base_url)
    mgr = _build_live_service_manager(tmp_path)
    agent: LoveYouAgent | None = None

    try:
        admin.login()
        app_id = admin.post("/api/v1/admin/apps", {"name": "LoveYou Edge App", "slug": "loveyou-edge"}).json()["app"]["id"]
        code = admin.post(f"/api/v1/admin/apps/{app_id}/enrollments", {"max_uses": 1}).json()["enrollment"]["enrollment_code"]

        mgr.execute_action("screenshot_service", "start")
        agent_dir = tmp_path / "agent_e2e_edge"
        agent = LoveYouAgent(
            service_manager=mgr,
            api_base_url=live_xoose.base_url,
            app_slug="loveyou-edge",
            app_version="1.0.0",
            enrollment_code=code,
            storage_dir=str(agent_dir),
        )
        assert agent.run_cycle(force_all=True)["online"] is True

        device_db_id = admin.get("/api/v1/admin/devices").json()["devices"][0]["id"]

        # A. Command kedaluwarsa di sisi server (expires_at di masa lalu) -> server menandai expired & tidak mengirimkannya
        expired_cmd_uuid = str(uuid.uuid4())
        past_ts = (datetime.now(timezone.utc) - timedelta(minutes=3)).strftime("%Y-%m-%d %H:%M:%S")
        now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(str(live_xoose.db_path)) as conn:
            conn.execute(
                """
                INSERT INTO service_commands
                    (command_id, device_id, service_key, action, payload, status, expires_at, created_at, updated_at)
                VALUES (?, ?, 'screenshot_service', 'stop', '{}', 'pending', ?, ?, ?)
                """,
                (expired_cmd_uuid, device_db_id, past_ts, now_ts, now_ts),
            )
            conn.commit()

        cyc = agent.run_cycle(force_all=True)
        assert cyc["commands_processed"] == []
        assert mgr.get("screenshot_service").current_status == STATUS_RUNNING

        cmds = admin.get("/api/v1/admin/commands").json()["commands"]
        exp_row = [c for c in cmds if c["command_id"] == expired_cmd_uuid][0]
        assert exp_row["status"] == "expired"

        # B. Command yang diterima Agent dalam keadaan sudah lewat expires_at -> ditolak lokal & di-ack 'expired' ke server
        live_cmd = admin.post(
            f"/api/v1/admin/devices/{device_db_id}/commands",
            {"service_key": "screenshot_service", "action": "stop", "ttl_seconds": 60},
        ).json()["command"]
        tampered_expired = dict(live_cmd)
        tampered_expired["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()

        res_local_exp = agent.command_executor.execute_command(tampered_expired)
        assert res_local_exp["ok"] is False
        assert res_local_exp["status"] == "expired"
        assert mgr.get("screenshot_service").current_status == STATUS_RUNNING

        cmds_after_exp = admin.get("/api/v1/admin/commands").json()["commands"]
        row_after_exp = [c for c in cmds_after_exp if c["command_id"] == live_cmd["command_id"]][0]
        assert row_after_exp["status"] == "expired"

        # C. Duplicate command idempotency (termasuk setelah reload DeviceIdentityStore seolah restart aplikasi)
        stop_cmd = admin.post(
            f"/api/v1/admin/devices/{device_db_id}/commands",
            {"service_key": "screenshot_service", "action": "stop", "ttl_seconds": 120},
        ).json()["command"]
        agent.run_cycle(force_all=True)
        assert mgr.get("screenshot_service").current_status == STATUS_STOPPED

        # Hidupkan kembali service, lalu reload identity dari disk & kirim ulang payload command yang SAMA persis
        mgr.execute_action("screenshot_service", "start")
        assert mgr.get("screenshot_service").current_status == STATUS_RUNNING

        reloaded_identity = DeviceIdentityStore(app_slug="loveyou-edge", storage_dir=agent_dir)
        agent.command_executor.identity = reloaded_identity
        dup_res = agent.command_executor.execute_command(stop_cmd)
        assert dup_res["duplicate"] is True
        assert mgr.get("screenshot_service").current_status == STATUS_RUNNING
    finally:
        if agent is not None:
            agent.stop()
        mgr.stop_all()
        admin.close()


def test_e2e_offline_reconnect_recovery_and_remote_revocation(live_xoose: LiveXooseServer, tmp_path: Path) -> None:
    admin = AdminHttpSession(live_xoose.base_url)
    mgr = _build_live_service_manager(tmp_path)
    agent: LoveYouAgent | None = None

    try:
        admin.login()
        app_id = admin.post("/api/v1/admin/apps", {"name": "LoveYou Resilient", "slug": "loveyou-resilient"}).json()["app"]["id"]
        code = admin.post(f"/api/v1/admin/apps/{app_id}/enrollments", {"max_uses": 1}).json()["enrollment"]["enrollment_code"]

        mgr.execute_action("screenshot_service", "start")
        agent = LoveYouAgent(
            service_manager=mgr,
            api_base_url=live_xoose.base_url,
            app_slug="loveyou-resilient",
            app_version="1.0.0",
            enrollment_code=code,
            storage_dir=str(tmp_path / "agent_e2e_offline"),
        )
        assert agent.run_cycle(force_all=True)["online"] is True
        device_db_id = admin.get("/api/v1/admin/devices").json()["devices"][0]["id"]

        # 1. Matikan server Laravel nyata -> simulasi offline total
        live_xoose.stop()
        agent.buffer_log("screenshot_service", "warning", "Offline log buffered during outage")
        c_off = agent.run_cycle(force_all=True)
        assert c_off["online"] is False
        assert agent._consecutive_failures == 1
        assert agent._compute_backoff_wait() >= 2.0
        # Layanan lokal tetap berjalan stabil tanpa terpengaruh offline
        assert mgr.get("screenshot_service").current_status == STATUS_RUNNING

        # 2. Hidupkan kembali server Laravel pada port & SQLite yang sama -> auto-reconnect & flush log tertunda
        live_xoose.start()
        c_back = agent.run_cycle(force_all=True)
        assert c_back["online"] is True
        assert agent._consecutive_failures == 0
        assert c_back["heartbeat_sent"] is True
        assert c_back["logs_flushed"] >= 1

        logs_resp = admin.get("/api/v1/admin/logs", params={"event_type": "agent_event"})
        assert logs_resp.status_code == 200
        messages = [entry["message"] for entry in logs_resp.json()["logs"]]
        assert "Offline log buffered during outage" in messages

        # 3. Admin mencabut akses perangkat (Revoke) -> Agent menerima HTTP 401 nyata & membersihkan token lokal
        rev_resp = admin.post(f"/api/v1/admin/devices/{device_db_id}/revoke", {"reason": "e2e_security_test"})
        assert rev_resp.status_code == 200

        c_rev = agent.run_cycle(force_all=True)
        assert c_rev["revoked"] is True
        assert agent.identity.is_revoked is True
        assert agent.identity.get_device_token() == ""
    finally:
        if agent is not None:
            agent.stop()
        mgr.stop_all()
        admin.close()
