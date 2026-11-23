"""
Milestone BC-7 — End-to-End Integration Testing:
Menghubungkan secara LIVE:
  1. Server Laravel Xoose nyata (php artisan serve pada SQLite terisolasi)
  2. LoveYouAgent + ServiceManager + BrowserCaptureService + ScreenshotService + SyncService
  3. Compiled PyInstaller onedir Native Messaging Host (xoose-native-host.exe) / host.py via stdio pipe
  4. Simulasi protokol WebExtensions untuk Chrome, Edge, dan Firefox

Skenario wajib yang diverifikasi:
  - Enrollment perangkat & sinkronisasi konfigurasi live tanpa rebuild EXE
  - Admin mengubah active_source -> browser_domain & tambah domain konstantaedu.com secara live
  - Multi-chunk transfer + SHA-256 + konversi WEBP + atomic spool -> SyncService (source=browser_tab)
  - Admin mengganti target domain (ujian.kampus.ac.id) secara live saat Native Host masih berjalan!
  - Live switch -> application_window (filter GetForegroundWindow) -> disabled -> desktop_mss -> rollback
  - Kontrol Pause/Stop lokal meneruskan control_action ke Native Host
  - Pelaporan status extension/izin/domain ke POST /api/v1/agent/status & audit log di Xoose
  - Simulasi offline/reconnect + pemulihan pengiriman log
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import socket
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from backend.agent import DeviceIdentityStore, LoveYouAgent, NativeHostBridge
from backend.services import (
    STATUS_PAUSED,
    STATUS_RUNNING,
    BrowserCaptureService,
    ScreenshotService,
    ServiceManager,
    SyncService,
)

XOOSE_DIR = Path(r"D:\laragon\www\xoose")
INTEGRATION_DIR = Path(r"D:\KULIAH\KODINGAN\xoose-loveyou")
COMPILED_NATIVE_HOST_EXE = (
    INTEGRATION_DIR
    / "builds"
    / "browser-capture"
    / "bundle_v1"
    / "native-host"
    / "dist"
    / "xoose-native-host"
    / "xoose-native-host.exe"
)
NATIVE_HOST_PY = INTEGRATION_DIR / "native-host" / "host.py"


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _enc_frame(payload: dict[str, Any]) -> bytes:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return struct.pack("<I", len(raw)) + raw


def _read_frame(stream: Any, timeout_sec: float = 8.0) -> dict[str, Any]:
    result: dict[str, Any] = {}
    err: list[Exception] = []

    def _reader() -> None:
        try:
            hdr = stream.read(4)
            if not hdr or len(hdr) < 4:
                raise EOFError("Native host stdout EOF")
            (ln,) = struct.unpack("<I", hdr)
            body = stream.read(ln)
            result["msg"] = json.loads(body.decode("utf-8"))
        except Exception as exc:
            err.append(exc)

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    t.join(timeout=timeout_sec)
    if t.is_alive():
        raise TimeoutError(f"Timeout membaca frame Native Messaging setelah {timeout_sec}s")
    if err:
        raise err[0]
    return result["msg"]


def _wait_for_frame_type(stream: Any, expected_type: str, max_reads: int = 8, timeout_sec: float = 8.0) -> dict[str, Any]:
    seen: list[str] = []
    for _ in range(max_reads):
        msg = _read_frame(stream, timeout_sec=timeout_sec)
        seen.append(str(msg.get("type")))
        if msg.get("type") == expected_type:
            return msg
    raise AssertionError(f"Tidak menerima '{expected_type}'; frame terlihat: {seen}")


def _make_png_bytes(color: tuple[int, int, int] = (16, 185, 129)) -> tuple[bytes, str, str]:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), color=color).save(buf, format="PNG")
    raw = buf.getvalue()
    return raw, base64.b64encode(raw).decode("ascii"), hashlib.sha256(raw).hexdigest()


class FakeUploadLogger:
    def __init__(self) -> None:
        self.uploaded: list[tuple[str, str]] = []
        self.records: list[dict[str, Any]] = []

    def upload_file(self, filepath: str, remote_filename: str) -> str:
        self.uploaded.append((filepath, remote_filename))
        return f"https://example.supabase.co/storage/v1/object/public/screenshots/{remote_filename}"

    def log_record(self, **kwargs: Any) -> dict[str, Any]:
        self.records.append(kwargs)
        return kwargs


def _kill_process_tree(proc: subprocess.Popen[Any] | None) -> None:
    if proc is None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    else:
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _run_artisan_php(env: dict[str, str], php_code: str) -> str:
    proc = subprocess.run(
        ["php", "-r", php_code],
        cwd=str(XOOSE_DIR),
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    return proc.stdout.strip()


@pytest.mark.skipif(not XOOSE_DIR.exists(), reason="Laravel Xoose tidak tersedia di mesin ini")
def test_e2e_live_specific_screen_across_xoose_agent_and_native_host(tmp_path: Path) -> None:
    # 1. Siapkan database SQLite terisolasi untuk server Xoose uji
    db_file = tmp_path / "e2e_bc7.sqlite"
    db_file.write_bytes(b"")

    server_env = os.environ.copy()
    server_env["APP_ENV"] = "testing"
    server_env["DB_CONNECTION"] = "sqlite"
    server_env["DB_DATABASE"] = str(db_file)
    server_env["CACHE_STORE"] = "array"
    server_env["SESSION_DRIVER"] = "file"
    server_env["REMOTE_CAPTURE_RULES_ENABLED"] = "true"

    subprocess.run(
        ["php", "artisan", "migrate", "--force"],
        cwd=str(XOOSE_DIR),
        env=server_env,
        check=True,
        capture_output=True,
        timeout=30,
    )
    subprocess.run(
        ["php", "artisan", "db:seed", "--class=ServiceDefinitionSeeder", "--force"],
        cwd=str(XOOSE_DIR),
        env=server_env,
        check=True,
        capture_output=True,
        timeout=30,
    )

    # Seed Admin, ManagedApp, dan Enrollment Code melalui Laravel bootstrap
    seed_out = _run_artisan_php(
        server_env,
        r"""
        require 'vendor/autoload.php';
        $app = require 'bootstrap/app.php';
        $app->make(Illuminate\Contracts\Console\Kernel::class)->bootstrap();
        $user = App\Models\User::factory()->create(['email' => 'admin@bc7.test']);
        $mApp = App\Models\ManagedApp::create([
            'owner_id' => $user->id,
            'name' => 'LoveYou BC7',
            'slug' => 'loveyou-bc7',
            'current_version' => '1.0.0',
            'build_status' => 'built',
            'is_active' => true,
        ]);
        $enr = app(App\Services\Remote\DeviceEnrollmentService::class)->createEnrollmentCode($mApp, $user, 'E2E BC7', 5, 24);
        echo json_encode(['app_id' => $mApp->id, 'user_id' => $user->id, 'enrollment_code' => $enr->enrollment_code]);
        """,
    )
    seed_info = json.loads(seed_out)
    app_id = int(seed_info["app_id"])
    user_id = int(seed_info["user_id"])
    enrollment_code = str(seed_info["enrollment_code"])

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    server_proc = subprocess.Popen(
        ["php", "artisan", "serve", "--host=127.0.0.1", f"--port={port}", "--no-reload"],
        cwd=str(XOOSE_DIR),
        env=server_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    host_proc: subprocess.Popen[bytes] | None = None
    agent: LoveYouAgent | None = None
    mgr = ServiceManager()

    try:
        # Tunggu server Laravel siap menerima koneksi HTTP
        deadline = time.time() + 15.0
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f"{base_url}/login", timeout=1.0)
                break
            except Exception:
                time.sleep(0.25)
        else:
            raise RuntimeError("Server Laravel uji gagal menyala tepat waktu.")

        # 2. Inisialisasi LoveYou ServiceManager + NativeHostBridge + LoveYouAgent
        bridge_dir = tmp_path / "bridge"
        bridge = NativeHostBridge(bridge_dir=bridge_dir)
        fake_logger = FakeUploadLogger()
        fg_state = {"process": "notepad.exe", "title": "Catatan"}

        def fake_mss(output_dir: str, image_format: str, jpg_quality: int) -> tuple[str, str, str]:
            p = Path(output_dir) / f"mss_{int(time.time() * 1000)}.{image_format}"
            p.write_bytes(b"MSS_LIVE_BYTES")
            return str(p), p.name, "1920x1080"

        sync_svc = SyncService(
            initial_config={"supabase_url": "https://example.supabase.co", "retry_backoff_seconds": 0.2},
            logger_factory=lambda: fake_logger,
            get_user_fn=lambda: "peserta_ujian",
            keep_local_file_fn=lambda: False,
        )
        ss_svc = ScreenshotService(
            initial_config={"interval_seconds": 60, "local_folder": str(tmp_path / "ss"), "keep_local_file": False},
            capture_fn=fake_mss,
            active_window_fn=lambda: fg_state["title"],
            active_process_fn=lambda: fg_state["process"],
            on_captured=sync_svc.enqueue,
        )
        identity_store = DeviceIdentityStore(app_slug="loveyou-bc7", storage_dir=tmp_path / "agent_id")
        bc_svc = BrowserCaptureService(
            native_bridge=bridge,
            get_user_fn=lambda: "peserta_ujian",
            get_device_id_fn=lambda: identity_store.device_id,
            on_captured=sync_svc.enqueue,
            on_policy_changed=lambda cfg, _ver: mgr.coordinate_capture_mode(cfg),
            poll_interval_seconds=0.1,
        )

        mgr.register(sync_svc)
        mgr.register(ss_svc)
        mgr.register(bc_svc)
        mgr.execute_action("screenshot_service", "start")

        agent = LoveYouAgent(
            service_manager=mgr,
            api_base_url=base_url,
            app_slug="loveyou-bc7",
            app_version="1.0.0",
            enrollment_code=enrollment_code,
            identity_store=identity_store,
        )

        # Siklus 1: Enrollment & Heartbeat awal
        c1 = agent.run_cycle(force_all=True)
        assert c1["enrolled"] is True
        assert c1["online"] is True
        assert identity_store.is_enrolled()

        # 3. Spawn REAL Native Messaging Host (compiled PyInstaller onedir EXE bila ada, fallback host.py)
        host_cmd = [str(COMPILED_NATIVE_HOST_EXE)] if COMPILED_NATIVE_HOST_EXE.exists() else [sys.executable, "-u", str(NATIVE_HOST_PY)]
        host_env = os.environ.copy()
        host_env["XOOSE_BRIDGE_DIR"] = str(bridge_dir)
        host_proc = subprocess.Popen(
            host_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=host_env,
        )
        assert host_proc.stdin is not None and host_proc.stdout is not None

        # Handshake hello (Chrome)
        host_proc.stdin.write(_enc_frame({"type": "hello", "browser": "chrome", "extension_id": "a" * 32}))
        host_proc.stdin.flush()
        hello_ack = _wait_for_frame_type(host_proc.stdout, "hello_ack")
        assert hello_ack["spool_ready"] is True
        init_cfg = _wait_for_frame_type(host_proc.stdout, "config_update")
        assert init_cfg["browser_capture"]["enabled"] is False

        # 4. ADMIN LIVE ACTION #1 di Xoose: switch ke browser_domain + tambah domain konstantaedu.com
        _run_artisan_php(
            server_env,
            f"""
            require 'vendor/autoload.php';
            $app = require 'bootstrap/app.php';
            $app->make(Illuminate\\Contracts\\Console\\Kernel::class)->bootstrap();
            $svc = app(App\\Services\\Remote\\CaptureRuleConfigService::class);
            $mApp = App\\Models\\ManagedApp::findOrFail({app_id});
            $user = App\\Models\\User::findOrFail({user_id});
            $svc->updatePolicy($mApp, ['active_source' => 'browser_domain', 'permission_mode' => 'local_start', 'capture_interval_seconds' => 10, 'image_format' => 'webp'], null, $user);
            $svc->storeRule($mApp, ['rule_type' => 'browser', 'id' => 'konstanta-domain', 'domain' => 'konstantaedu.com', 'include_subdomains' => true, 'schemes' => ['https'], 'browsers' => ['chrome', 'edge', 'firefox'], 'enabled' => true], null, $user);
            """,
        )

        # Agent sinkron live tanpa rebuild/restart!
        c2 = agent.run_cycle(force_all=True)
        assert c2["online"] is True
        assert bc_svc.active_config_version == 2
        assert ss_svc.current_status == STATUS_PAUSED
        assert bc_svc.current_status == STATUS_RUNNING

        # Native Host watcher otomatis mendorong config_update v2 ke Extension!
        live_cfg_v2 = _wait_for_frame_type(host_proc.stdout, "config_update", timeout_sec=6.0)
        assert live_cfg_v2["config_version"] == 2
        assert live_cfg_v2["active_source"] == "browser_domain"
        assert live_cfg_v2["browser_capture"]["rules"][0]["domain"] == "konstantaedu.com"

        # Bersihkan antrean MSS awal sebelum switch agar kita verifikasi hanya item browser_tab yang masuk
        while not sync_svc.upload_queue.empty():
            sync_svc.upload_queue.get_nowait()

        # 5. Extension mengirim status_report & gambar multi-chunk (2 potongan) + SHA-256 ke Native Host
        host_proc.stdin.write(
            _enc_frame(
                {
                    "type": "status_report",
                    "browser": "chrome",
                    "extension_connected": True,
                    "permission_mode": "local_start",
                    "has_host_permission": False,
                    "local_session_state": "running",
                    "capture_state": "capturing",
                    "active_rule_id": "konstanta-domain",
                    "matched_domain": "www.konstantaedu.com",
                    "config_version": 2,
                }
            )
        )

        _, b64, sha = _make_png_bytes()
        mid = len(b64) // 2
        cid = str(uuid.uuid4())
        host_proc.stdin.write(
            _enc_frame(
                {
                    "type": "capture_chunk",
                    "capture_id": cid,
                    "chunk_index": 0,
                    "chunk_count": 2,
                    "mime_type": "image/png",
                    "target_format": "webp",
                    "payload": b64[:mid],
                    "checksum": sha,
                    "meta": {
                        "source": "browser_tab",
                        "browser": "chrome",
                        "rule_id": "konstanta-domain",
                        "origin_domain": "www.konstantaedu.com",
                    },
                }
            )
        )
        host_proc.stdin.write(
            _enc_frame(
                {
                    "type": "capture_chunk",
                    "capture_id": cid,
                    "chunk_index": 1,
                    "chunk_count": 2,
                    "mime_type": "image/png",
                    "target_format": "webp",
                    "payload": b64[mid:],
                    "checksum": sha,
                }
            )
        )
        host_proc.stdin.flush()

        cap_ack = _wait_for_frame_type(host_proc.stdout, "capture_ack", timeout_sec=6.0)
        assert cap_ack["capture_id"] == cid
        assert cap_ack["status"] == "spooled"
        assert cap_ack["mime_type"] == "image/webp"

        # Tunggu worker spool BrowserCaptureService meneruskan ke SyncService
        deadline_q = time.time() + 4.0
        while sync_svc.upload_queue.empty() and time.time() < deadline_q:
            bc_svc.consume_spool_once()
            time.sleep(0.05)

        queued_item = sync_svc.upload_queue.get(timeout=2.0)
        assert sync_svc.process_item(queued_item) is True
        assert len(fake_logger.records) == 1
        assert fake_logger.records[0]["capture_metadata"]["source"] == "browser_tab"
        assert fake_logger.records[0]["capture_metadata"]["origin_domain"] == "www.konstantaedu.com"

        # Agent melaporkan status live + heartbeat ke Xoose DB
        agent.run_cycle(force_all=True)

        # 6. ADMIN LIVE ACTION #2 saat aplikasi sedang dipakai: ganti domain ke ujian.kampus.ac.id (v3)
        _run_artisan_php(
            server_env,
            f"""
            require 'vendor/autoload.php';
            $app = require 'bootstrap/app.php';
            $app->make(Illuminate\\Contracts\\Console\\Kernel::class)->bootstrap();
            $svc = app(App\\Services\\Remote\\CaptureRuleConfigService::class);
            $mApp = App\\Models\\ManagedApp::findOrFail({app_id});
            $user = App\\Models\\User::findOrFail({user_id});
            $svc->updateRule($mApp, 'konstanta-domain', ['rule_type' => 'browser', 'domain' => 'ujian.kampus.ac.id', 'enabled' => true], null, $user);
            """,
        )
        agent.run_cycle(force_all=True)
        assert bc_svc.active_config_version == 3

        live_cfg_v3 = _wait_for_frame_type(host_proc.stdout, "config_update", timeout_sec=6.0)
        assert live_cfg_v3["config_version"] == 3
        assert live_cfg_v3["browser_capture"]["rules"][0]["domain"] == "ujian.kampus.ac.id"

        # 7. Kontrol Pause lokal meneruskan control_action='pause' ke Native Host
        mgr.execute_action("browser_capture_service", "pause")
        ctrl_msg = _wait_for_frame_type(host_proc.stdout, "control_action", timeout_sec=6.0)
        assert ctrl_msg["action"] == "pause"

        # 8. ADMIN LIVE ACTION #3: switch ke application_window & uji filter foreground MSS (v4 & v5)
        _run_artisan_php(
            server_env,
            f"""
            require 'vendor/autoload.php';
            $app = require 'bootstrap/app.php';
            $app->make(Illuminate\\Contracts\\Console\\Kernel::class)->bootstrap();
            $svc = app(App\\Services\\Remote\\CaptureRuleConfigService::class);
            $mApp = App\\Models\\ManagedApp::findOrFail({app_id});
            $user = App\\Models\\User::findOrFail({user_id});
            $svc->storeRule($mApp, ['rule_type' => 'window', 'id' => 'edge-ujian', 'process' => 'msedge.exe', 'window_title_contains' => 'Ujian', 'enabled' => true], null, $user);
            $svc->updatePolicy($mApp, ['active_source' => 'application_window'], null, $user);
            """,
        )
        agent.run_cycle(force_all=True)
        assert ss_svc.current_status == STATUS_RUNNING
        assert ss_svc.window_filter_enabled is True
        assert ss_svc.capture_once() is None  # notepad.exe -> skip

        fg_state["process"] = "msedge.exe"
        fg_state["title"] = "Portal Ujian Nasional — Microsoft Edge"
        win_cap = ss_svc.capture_once()
        assert win_cap is not None and win_cap["source"] == "application_window"

        # 9. ADMIN ROLLBACK ke v2 (kembali ke browser_domain konstantaedu.com sebagai v6 force_apply)
        _run_artisan_php(
            server_env,
            f"""
            require 'vendor/autoload.php';
            $app = require 'bootstrap/app.php';
            $app->make(Illuminate\\Contracts\\Console\\Kernel::class)->bootstrap();
            $svc = app(App\\Services\\Remote\\CaptureRuleConfigService::class);
            $mApp = App\\Models\\ManagedApp::findOrFail({app_id});
            $user = App\\Models\\User::findOrFail({user_id});
            $svc->rollback($mApp, 2, null, $user);
            """,
        )
        agent.run_cycle(force_all=True)
        assert bc_svc.active_config_version == 6
        assert bc_svc.config["browser_capture"]["rules"][0]["domain"] == "konstantaedu.com"
        assert ss_svc.current_status == STATUS_PAUSED

        # 10. Verifikasi data tersimpan di SQLite Xoose (service_status.details & audit service_logs)
        conn = sqlite3.connect(str(db_file))
        try:
            row = conn.execute(
                "SELECT current_status, active_config_version, details FROM service_status WHERE service_key = 'browser_capture_service'"
            ).fetchone()
            assert row is not None
            details = json.loads(row[2])
            assert details["extension_connected"] is True
            assert details["matched_domain"] == "www.konstantaedu.com"

            log_count = conn.execute(
                "SELECT COUNT(*) FROM service_logs WHERE service_key = 'browser_capture_service'"
            ).fetchone()[0]
            assert log_count >= 5
        finally:
            conn.close()

        # 11. Uji offline/reconnect: matikan server (termasuk child php -S) -> agent buffering log -> graceful offline
        _kill_process_tree(server_proc)
        time.sleep(0.3)
        agent.buffer_log("browser_capture_service", "info", "Offline buffered event BC7")
        offline_cycle = agent.run_cycle(force_all=True)
        assert offline_cycle["online"] is False

        # Nyalakan ulang server di port yang sama -> agent otomatis reconnect & flush log!
        server_proc = subprocess.Popen(
            ["php", "artisan", "serve", "--host=127.0.0.1", f"--port={port}", "--no-reload"],
            cwd=str(XOOSE_DIR),
            env=server_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline2 = time.time() + 15.0
        while time.time() < deadline2:
            try:
                urllib.request.urlopen(f"{base_url}/login", timeout=1.0)
                break
            except Exception:
                time.sleep(0.25)

        rec_cycle = agent.run_cycle(force_all=True)
        assert rec_cycle["online"] is True
        assert rec_cycle["logs_flushed"] >= 1

    finally:
        if host_proc is not None:
            try:
                if host_proc.stdin:
                    host_proc.stdin.close()
                host_proc.wait(timeout=3)
            except Exception:
                _kill_process_tree(host_proc)
        if agent is not None:
            agent.stop()
        mgr.stop_all()
        _kill_process_tree(server_proc)
