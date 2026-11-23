"""
Pengujian otomatis Milestone BC-4:
- Lifecycle lengkap BrowserCaptureService (BaseService)
- Validasi skema Pydantic & default browser_capture.enabled=False
- Bridge live config sync ke NativeHostBridge (runtime_config.json & control_signal.json)
- Atomic spool consumption -> SyncService dengan metadata terpisah source="browser_tab"
- Koordinasi live mode switch (browser_domain / application_window / desktop_mss / disabled)
  tanpa merusak ScreenshotService (MSS) lama
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.agent.native_bridge import NativeHostBridge
from backend.schemas.service_schemas import validate_service_config
from backend.services import (
    STATUS_PAUSED,
    STATUS_RUNNING,
    STATUS_STOPPED,
    BrowserCaptureService,
    ScreenshotService,
    ServiceManager,
    SyncService,
)


class DummyLogger:
    def __init__(self) -> None:
        self.uploaded: list[tuple[str, str]] = []
        self.records: list[dict[str, Any]] = []

    def upload_file(self, filepath: str, remote_filename: str) -> str:
        self.uploaded.append((filepath, remote_filename))
        return f"https://example.supabase.co/storage/v1/object/public/screenshots/{remote_filename}"

    def log_record(self, **kwargs: Any) -> dict[str, Any]:
        self.records.append(kwargs)
        return kwargs


def _sample_browser_policy(
    active_source: str = "browser_domain",
    browser_enabled: bool = True,
    window_enabled: bool = False,
    domain: str = "konstantaedu.com",
) -> dict[str, Any]:
    return {
        "capture_policy": {
            "active_source": active_source,
            "allow_live_switch": True,
        },
        "browser_capture": {
            "enabled": browser_enabled,
            "capture_mode": "active_tab",
            "permission_mode": "local_start",
            "local_session_required": True,
            "pause_when_browser_unfocused": True,
            "pause_when_tab_inactive": True,
            "image_format": "webp",
            "fallback_format": "png",
            "capture_interval_seconds": 10,
            "rules": [
                {
                    "id": "konstanta-domain",
                    "enabled": True,
                    "domain": domain,
                    "include_subdomains": True,
                    "schemes": ["https"],
                    "browsers": ["chrome", "edge", "firefox"],
                }
            ],
        },
        "window_capture": {
            "enabled": window_enabled,
            "capture_mode": "active_window",
            "capture_interval_seconds": 10,
            "rules": [
                {
                    "id": "chrome-window",
                    "enabled": window_enabled,
                    "process": "chrome.exe",
                    "window_title_contains": "Tryout",
                }
            ],
        },
    }


def test_browser_capture_schema_defaults_and_strict_validation() -> None:
    ok, norm, err = validate_service_config("browser_capture_service", {})
    assert ok is True and err is None and norm is not None
    assert norm["browser_capture"]["enabled"] is False
    assert norm["browser_capture"]["permission_mode"] == "local_start"

    # Wildcard domain wajib ditolak
    bad_cfg = _sample_browser_policy(domain="*.konstantaedu.com")
    ok_bad, _, err_bad = validate_service_config("browser_capture_service", bad_cfg)
    assert ok_bad is False and err_bad is not None

    # Process dengan path traversal wajib ditolak
    bad_win = _sample_browser_policy()
    bad_win["window_capture"]["rules"][0]["process"] = "C:\\Windows\\cmd.exe"
    ok_win, _, _ = validate_service_config("browser_capture_service", bad_win)
    assert ok_win is False


def test_browser_capture_service_lifecycle_and_bridge_sync(tmp_path: Path) -> None:
    bridge = NativeHostBridge(bridge_dir=tmp_path / "bridge")
    mgr = ServiceManager()
    svc = BrowserCaptureService(
        native_bridge=bridge,
        get_device_id_fn=lambda: "dev-uuid-77",
        poll_interval_seconds=0.1,
    )
    mgr.register(svc)

    # Lifecycle start -> pause -> resume -> restart -> stop
    assert mgr.execute_action("browser_capture_service", "start")["ok"] is True
    assert svc.current_status == STATUS_RUNNING
    assert bridge.runtime_config_file.exists()

    assert mgr.execute_action("browser_capture_service", "pause")["ok"] is True
    assert svc.current_status == STATUS_PAUSED
    sig = json.loads(bridge.control_signal_file.read_text(encoding="utf-8"))
    assert sig["action"] == "pause"

    assert mgr.execute_action("browser_capture_service", "resume")["ok"] is True
    assert svc.current_status == STATUS_RUNNING

    # Terapkan config baru secara live -> langsung ditulis ke runtime_config.json
    res = mgr.apply_service_config("browser_capture_service", _sample_browser_policy(domain="ujian.kampus.ac.id"), config_version=3)
    assert res["ok"] is True
    rt = json.loads(bridge.runtime_config_file.read_text(encoding="utf-8"))
    assert rt["config_version"] == 3
    assert rt["device_id"] == "dev-uuid-77"
    assert rt["browser_capture"]["rules"][0]["domain"] == "ujian.kampus.ac.id"

    # Simulasikan laporan status dari native-host/host.py -> terbaca di get_status()
    (bridge.bridge_dir / "host_status_chrome.json").write_text(
        json.dumps(
            {
                "browser": "chrome",
                "extension_connected": True,
                "permission_mode": "automatic_domain",
                "has_host_permission": True,
                "local_session_state": "running",
                "capture_state": "capturing",
                "active_rule_id": "konstanta-domain",
                "matched_domain": "ujian.kampus.ac.id",
                "config_version": 3,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    st = svc.get_status()
    assert st["extension_connected"] is True
    assert st["connected_browsers"] == ["chrome"]
    assert st["matched_domain"] == "ujian.kampus.ac.id"
    assert st["active_config_version"] == 3

    # Rollback otomatis jika config tidak valid dikirim
    bad_res = mgr.apply_service_config("browser_capture_service", _sample_browser_policy(domain="http://evil.com"), config_version=4)
    assert bad_res["ok"] is False
    assert svc.active_config_version == 3

    assert mgr.execute_action("browser_capture_service", "stop")["ok"] is True
    assert svc.current_status == STATUS_STOPPED


def test_atomic_spool_to_sync_service_with_browser_tab_metadata(tmp_path: Path) -> None:
    bridge = NativeHostBridge(bridge_dir=tmp_path / "bridge")
    logger = DummyLogger()
    sync_svc = SyncService(
        initial_config={
            "supabase_url": "https://example.supabase.co",
            "table_name": "hasil_ss",
            "bucket_name": "screenshots",
            "retry_backoff_seconds": 0.5,
        },
        logger_factory=lambda: logger,
        get_user_fn=lambda: "mahasiswa1",
        keep_local_file_fn=lambda: False,
    )
    bc_svc = BrowserCaptureService(
        native_bridge=bridge,
        get_user_fn=lambda: "mahasiswa1",
        get_device_id_fn=lambda: "dev-uuid-88",
        on_captured=sync_svc.enqueue,
    )
    bc_svc.start()

    # Tulis pasangan spool atomik seperti keluaran image_assembler.py
    cid = str(uuid.uuid4())
    raw_img = b"RIFF\x10\x00\x00\x00WEBPVP8 FAKEBYTES"
    sha = hashlib.sha256(raw_img).hexdigest()
    (bridge.incoming_dir / f"{cid}.webp").write_bytes(raw_img)
    (bridge.incoming_dir / f"{cid}.meta.json").write_text(
        json.dumps(
            {
                "capture_id": cid,
                "source": "browser_tab",
                "browser": "chrome",
                "rule_id": "konstanta-domain",
                "device_id": "dev-uuid-88",
                "origin_domain": "www.konstantaedu.com",
                "captured_at": "2026-11-23T13:00:00Z",
                "mime_type": "image/webp",
                "image_checksum": sha,
                "resolution": "1280x720",
                "image_filename": f"{cid}.webp",
            }
        ),
        encoding="utf-8",
    )

    items = bc_svc.consume_spool_once()
    assert len(items) == 1
    assert items[0]["source"] == "browser_tab"
    assert items[0]["origin_domain"] == "www.konstantaedu.com"
    assert items[0]["filename"].startswith("browser_chrome_")

    # Proses item pada SyncService
    queued = sync_svc.upload_queue.get_nowait()
    ok = sync_svc.process_item(queued)
    assert ok is True
    assert len(logger.uploaded) == 1
    assert len(logger.records) == 1

    rec = logger.records[0]
    assert rec["capture_metadata"]["source"] == "browser_tab"
    assert rec["capture_metadata"]["browser"] == "chrome"
    assert rec["capture_metadata"]["origin_domain"] == "www.konstantaedu.com"
    assert rec["capture_metadata"]["image_checksum"] == sha
    assert "source=browser_tab" in rec["notes"]
    assert "[chrome] www.konstantaedu.com" == rec["active_window"]
    # File temporer dibersihkan setelah diunggah
    assert not os.path.exists(queued["filepath"])

    bc_svc.stop()


def test_live_capture_mode_switching_and_application_window_filter(tmp_path: Path) -> None:
    bridge = NativeHostBridge(bridge_dir=tmp_path / "bridge")
    mgr = ServiceManager()

    captured_items: list[dict[str, Any]] = []
    fg_state = {"process": "notepad.exe", "title": "Catatan Biasa"}

    def fake_mss_capture(output_dir: str, image_format: str, jpg_quality: int) -> tuple[str, str, str]:
        p = Path(output_dir) / f"ss_{len(captured_items) + 1}.{image_format}"
        p.write_bytes(b"MSSBYTES")
        return str(p), p.name, "1920x1080"

    ss_svc = ScreenshotService(
        initial_config={"interval_seconds": 10, "local_folder": str(tmp_path / "ss"), "keep_local_file": True},
        capture_fn=fake_mss_capture,
        active_window_fn=lambda: fg_state["title"],
        active_process_fn=lambda: fg_state["process"],
        on_captured=captured_items.append,
    )
    bc_svc = BrowserCaptureService(
        native_bridge=bridge,
        on_policy_changed=lambda cfg, _ver: mgr.coordinate_capture_mode(cfg),
    )

    mgr.register(ss_svc)
    mgr.register(bc_svc)

    # Awalnya MSS berjalan normal (desktop_mss)
    mgr.execute_action("screenshot_service", "start")
    assert ss_svc.current_status == STATUS_RUNNING
    assert ss_svc.capture_once()["source"] == "desktop_mss"

    # 1. Admin live switch -> browser_domain (enabled=True) => MSS otomatis PAUSED, BrowserCaptureService RUNNING
    mgr.apply_service_config("browser_capture_service", _sample_browser_policy(active_source="browser_domain", browser_enabled=True), config_version=1)
    assert ss_svc.current_status == STATUS_PAUSED
    assert bc_svc.current_status == STATUS_RUNNING

    # 2. Admin live switch -> application_window (chrome.exe + "Tryout") => BrowserCapture PAUSED, MSS RUNNING berfilter
    mgr.apply_service_config("browser_capture_service", _sample_browser_policy(active_source="application_window", browser_enabled=False, window_enabled=True), config_version=2)
    assert bc_svc.current_status == STATUS_PAUSED
    assert ss_svc.current_status == STATUS_RUNNING
    assert ss_svc.window_filter_enabled is True

    # Foreground adalah notepad.exe -> skip!
    assert ss_svc.capture_once() is None

    # Foreground berubah ke chrome.exe judul "Tryout Konstanta" -> capture!
    fg_state["process"] = "chrome.exe"
    fg_state["title"] = "Tryout Konstanta — Google Chrome"
    win_item = ss_svc.capture_once()
    assert win_item is not None
    assert win_item["source"] == "application_window"
    assert win_item["rule_id"] == "chrome-window"

    # 3. Admin live switch -> disabled => Keduanya PAUSED
    mgr.apply_service_config("browser_capture_service", _sample_browser_policy(active_source="disabled", browser_enabled=False, window_enabled=False), config_version=3)
    assert bc_svc.current_status == STATUS_PAUSED
    assert ss_svc.current_status == STATUS_PAUSED

    # 4. Admin live switch kembali ke desktop_mss => MSS kembali penuh tanpa filter
    mgr.apply_service_config("browser_capture_service", _sample_browser_policy(active_source="desktop_mss", browser_enabled=False, window_enabled=False), config_version=4)
    assert ss_svc.current_status == STATUS_RUNNING
    assert ss_svc.window_filter_enabled is False
    assert ss_svc.capture_once()["source"] == "desktop_mss"

    mgr.stop_all()
