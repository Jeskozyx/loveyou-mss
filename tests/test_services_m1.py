"""
Pengujian otomatis Milestone 1: Modular Service Refactoring & ServiceManager.
"""

from __future__ import annotations

import configparser
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from backend import screenshot_service as legacy_module
from backend.schemas import get_service_json_schema, validate_service_config
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

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class DummyTrayIcon:
    def __init__(self) -> None:
        self.title = " "
        self.started = False
        self.stopped = False
        self.notifications: list[tuple[str, str]] = []

    def run_detached(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def notify(self, message: str, title: str) -> None:
        self.notifications.append((title, message))


class FakeSupabaseLogger:
    def __init__(self) -> None:
        self.uploaded_files: list[tuple[str, str]] = []
        self.records: list[dict[str, Any]] = []

    def upload_file(self, filepath: str, remote_filename: str) -> str:
        self.uploaded_files.append((filepath, remote_filename))
        return f"https://example.supabase.co/storage/v1/object/public/screenshots/{remote_filename}"

    def log_record(self, **kwargs: Any) -> dict[str, Any]:
        self.records.append(kwargs)
        return kwargs


def _build_full_service_manager(tmp_path: Path) -> tuple[ServiceManager, dict[str, Any]]:
    mgr = ServiceManager()
    captured_counter = {"n": 0}
    fake_logger = FakeSupabaseLogger()
    notified: list[tuple[str, str]] = []

    def fake_capture(output_dir: str, image_format: str, jpg_quality: int) -> tuple[str, str, str]:
        captured_counter["n"] += 1
        fname = f"ss_test_{captured_counter['n']}.{image_format}"
        fpath = os.path.join(output_dir, fname)
        Path(fpath).write_bytes(b"FAKEIMAGEBYTES")
        return fpath, fname, "1920x1080"

    sync_svc = SyncService(
        initial_config={
            "supabase_url": "https://example.supabase.co",
            "table_name": "hasil_ss",
            "bucket_name": "screenshots",
            "retry_backoff_seconds": 0.5,
        },
        logger_factory=lambda: fake_logger,
        get_user_fn=lambda: "tester",
        keep_local_file_fn=lambda: bool(screenshot_svc.config.get("keep_local_file", False)),
    )
    notif_svc = NotificationService(
        initial_config={"enabled": False, "notify_on_capture": False},
        notify_sink=lambda title, msg: notified.append((title, msg)),
    )
    screenshot_svc = ScreenshotService(
        initial_config={
            "interval_seconds": 1,
            "image_format": "webp",
            "jpg_quality": 75,
            "keep_local_file": False,
            "local_folder": str(tmp_path / "screenshots"),
        },
        capture_fn=fake_capture,
        active_window_fn=lambda: "Test Window",
        get_user_fn=lambda: "tester",
        on_captured=sync_svc.enqueue,
        on_notify=lambda t, m: notif_svc.notify(t, m, event_kind="capture"),
    )
    tray_icon = DummyTrayIcon()
    tray_svc = TrayService(
        initial_config={"icon": "heart.ico", "app_name": "LoveYou", "minimalist_tooltip": True},
        tray_factory=lambda _cfg: tray_icon,
    )
    ui_updates: list[dict[str, Any]] = []
    ui_svc = UiService(
        launch_fn=lambda _cfg: None,
        on_config_updated=lambda cfg: ui_updates.append(cfg),
    )
    game_ticks = {"count": 0}
    game_svc = GameService(
        initial_config={"enabled": True, "game_mode": "idle", "tick_rate_hz": 20},
        tick_handler=lambda _cfg: game_ticks.__setitem__("count", game_ticks["count"] + 1),
    )
    custom_svc = CustomService(initial_config={"enabled": True, "plugin_name": "demo_plugin"})

    for svc in (screenshot_svc, tray_svc, sync_svc, notif_svc, ui_svc, game_svc, custom_svc):
        mgr.register(svc)

    return mgr, {
        "fake_logger": fake_logger,
        "tray_icon": tray_icon,
        "notified": notified,
        "ui_updates": ui_updates,
        "game_ticks": game_ticks,
    }


def test_config_ini_aligned_with_template_manifest_and_no_service_role_secret() -> None:
    config_path = PROJECT_ROOT / "backend" / "config.ini"
    manifest_path = PROJECT_ROOT / "template-manifest.json"

    raw_ini = config_path.read_text(encoding="utf-8")
    raw_manifest = manifest_path.read_text(encoding="utf-8")

    # Tidak boleh ada service_role key yang bocor ke client config atau manifest
    assert "service_role" not in raw_ini.lower()
    assert "eyj" not in raw_ini.lower()

    cfg = configparser.ConfigParser()
    cfg.read(config_path, encoding="utf-8")
    manifest = json.loads(raw_manifest)

    features_by_key = {f["key"]: f["default"] for f in manifest["features_schema"]}
    config_by_key = {c["key"]: c["default"] for c in manifest["configuration_schema"]}

    assert cfg["STARTUP"].getboolean("auto_start") is features_by_key["auto_start"] is False
    assert cfg["CAPTURE"].getboolean("keep_local_file") is features_by_key["keep_local_file"] is False
    assert cfg["CAPTURE"].get("image_format").strip().lower() == config_by_key["image_format"] == "webp"
    assert cfg["CAPTURE"].getint("jpg_quality") == config_by_key["jpg_quality"] == 75
    assert "AGENT" in cfg
    assert cfg["AGENT"].getboolean("enabled") is False


def test_all_seven_services_implement_base_contract(tmp_path: Path) -> None:
    mgr, _ = _build_full_service_manager(tmp_path)
    expected_keys = {
        "screenshot_service",
        "tray_service",
        "sync_service",
        "notification_service",
        "ui_service",
        "game_service",
        "custom_service",
    }
    assert set(mgr.list_services()) == expected_keys

    definitions = mgr.get_service_definitions()
    assert len(definitions) == 7
    for meta in definitions:
        assert meta["service_key"] in expected_keys
        assert meta["display_name"]
        assert isinstance(meta["supported_actions"], list) and len(meta["supported_actions"]) >= 2
        assert meta["current_status"] == STATUS_STOPPED
        assert isinstance(meta["configuration_schema"], dict)
        assert "properties" in meta["configuration_schema"]
        assert isinstance(meta["permission_requirements"], list)


def test_service_manager_lifecycle_and_graceful_stop(tmp_path: Path) -> None:
    mgr, ctx = _build_full_service_manager(tmp_path)

    # Start -> Pause -> Resume -> Restart -> Stop pada screenshot_service
    assert mgr.execute_action("screenshot_service", "start")["ok"] is True
    assert mgr.get("screenshot_service").current_status == STATUS_RUNNING

    assert mgr.execute_action("screenshot_service", "pause")["ok"] is True
    assert mgr.get("screenshot_service").current_status == STATUS_PAUSED

    assert mgr.execute_action("screenshot_service", "resume")["ok"] is True
    assert mgr.get("screenshot_service").current_status == STATUS_RUNNING

    assert mgr.execute_action("screenshot_service", "restart")["ok"] is True
    assert mgr.get("screenshot_service").current_status == STATUS_RUNNING

    # Tray & Sync start
    assert mgr.execute_action("tray_service", "start")["ok"] is True
    assert ctx["tray_icon"].started is True
    assert mgr.execute_action("sync_service", "start")["ok"] is True

    # Graceful stop_all menghentikan seluruh layanan tanpa error
    results = mgr.stop_all()
    assert all(r["ok"] for r in results.values())
    for status in mgr.get_all_statuses().values():
        assert status["status"] == STATUS_STOPPED
    assert ctx["tray_icon"].stopped is True


def test_service_manager_strict_allowlist_blocks_disallowed_and_shell_actions(tmp_path: Path) -> None:
    mgr, _ = _build_full_service_manager(tmp_path)

    # TrayService tidak mendukung pause
    res_pause = mgr.execute_action("tray_service", "pause")
    assert res_pause["ok"] is False

    # NotificationService tidak mendukung restart
    res_restart = mgr.execute_action("notification_service", "restart")
    assert res_restart["ok"] is False

    # Aksi shell / exec / arbitrary command ditolak keras
    for dangerous in ("shell", "exec", "powershell", "cmd", "eval", "rm -rf /", "delete"):
        res = mgr.execute_action("screenshot_service", dangerous)
        assert res["ok"] is False
        assert "allowlist" in res["message"].lower()

    # Service tidak dikenal ditolak
    res_unknown = mgr.execute_action("non_existent_service", "start")
    assert res_unknown["ok"] is False


def test_runtime_config_validation_and_automatic_rollback(tmp_path: Path) -> None:
    mgr, ctx = _build_full_service_manager(tmp_path)

    # 1. Valid partial update pada screenshot_service
    res_ok = mgr.apply_service_config(
        "screenshot_service",
        {"interval_seconds": 12, "image_format": "png", "jpg_quality": 90},
        config_version=2,
    )
    assert res_ok["ok"] is True
    ss = mgr.get("screenshot_service")
    assert ss.config["interval_seconds"] == 12
    assert ss.config["image_format"] == "png"
    assert ss.active_config_version == 2

    # 2. Invalid schema (interval_seconds=0 & extra field) harus ditolak & state lama tetap utuh
    res_bad_val = mgr.apply_service_config("screenshot_service", {"interval_seconds": 0}, config_version=3)
    assert res_bad_val["ok"] is False
    assert ss.config["interval_seconds"] == 12
    assert ss.active_config_version == 2

    res_extra = mgr.apply_service_config("screenshot_service", {"shell_cmd": "whoami"}, config_version=3)
    assert res_extra["ok"] is False
    assert ss.active_config_version == 2

    # 3. Valid update pada UiService memicu callback on_config_updated
    res_ui = mgr.apply_service_config(
        "ui_service",
        {"date_invitation_text": "MINGGU, 27 DESEMBER 2026", "tanggal_date": "2026-12-27"},
        config_version=4,
    )
    assert res_ui["ok"] is True
    assert len(ctx["ui_updates"]) == 1
    assert ctx["ui_updates"][0]["tanggal_date"] == "2026-12-27"

    # 4. Simulasi exception saat apply_config pada CustomService -> rollback otomatis ke versi & config sebelumnya
    def failing_hook(cfg: dict[str, Any]) -> None:
        if cfg.get("plugin_name") == "broken_plugin":
            raise RuntimeError("Plugin gagal inisialisasi")

    custom = CustomService(
        initial_config={"enabled": True, "plugin_name": "stable_plugin", "parameters": {"a": 1}},
        on_config=failing_hook,
    )
    mgr.register(custom)
    assert mgr.apply_service_config("custom_service", {"plugin_name": "good_v1"}, config_version=1)["ok"] is True

    res_fail = mgr.apply_service_config("custom_service", {"plugin_name": "broken_plugin"}, config_version=2)
    assert res_fail["ok"] is False
    assert res_fail["rolled_back"] is True
    assert custom.config["plugin_name"] == "good_v1"
    assert custom.active_config_version == 1


def test_screenshot_and_sync_end_to_end_temp_cleanup_and_notifications(tmp_path: Path) -> None:
    mgr, ctx = _build_full_service_manager(tmp_path)
    ss = mgr.get("screenshot_service")
    sync = mgr.get("sync_service")
    notif = mgr.get("notification_service")

    # Aktifkan notifikasi capture
    mgr.execute_action("notification_service", "start")
    mgr.apply_service_config("notification_service", {"enabled": True, "notify_on_capture": True}, config_version=1)

    # Capture 1x (keep_local_file=False -> simpan di %TEMP%, lalu dihapus setelah di-upload)
    item = ss.capture_once()
    assert Path(item["filepath"]).exists()
    assert len(ctx["notified"]) == 1

    ok = sync.process_item(item)
    assert ok is True
    assert len(ctx["fake_logger"].uploaded_files) == 1
    assert len(ctx["fake_logger"].records) == 1
    assert ctx["fake_logger"].records[0]["user_name"] == "tester"
    # File temp dibersihkan setelah upload berhasil
    assert not Path(item["filepath"]).exists()
    # Folder lokal tidak pernah dibuat ketika keep_local_file=False
    assert not (tmp_path / "screenshots").exists()


def test_game_service_tick_loop_and_pause_resume(tmp_path: Path) -> None:
    mgr, ctx = _build_full_service_manager(tmp_path)
    game = mgr.get("game_service")

    assert mgr.execute_action("game_service", "start")["ok"] is True
    time.sleep(0.15)
    assert ctx["game_ticks"]["count"] > 0

    assert mgr.execute_action("game_service", "pause")["ok"] is True
    paused_count = ctx["game_ticks"]["count"]
    time.sleep(0.12)
    assert ctx["game_ticks"]["count"] <= paused_count + 1

    assert mgr.execute_action("game_service", "stop")["ok"] is True
    assert game.current_status == STATUS_STOPPED


def test_legacy_screenshot_service_backward_compatibility() -> None:
    conf = legacy_module.load_config()
    assert conf["auto_start"] is False
    assert conf["image_format"] == "webp"
    assert conf["jpg_quality"] == 75
    assert conf["keep_local_file"] is False
    assert "agent_enabled" in conf

    resolved = legacy_module.resolve_date_settings("MINGGU, 27 DESEMBER 2026", "2026-12-05T12:00:00Z")
    assert resolved["tanggal_date"] == "2026-12-27"

    controller = legacy_module.ServiceController(conf, enable_agent=False, init_supabase=False)
    assert set(controller.service_manager.list_services()) == {
        "screenshot_service",
        "browser_capture_service",
        "tray_service",
        "sync_service",
        "notification_service",
        "ui_service",
        "game_service",
        "custom_service",
        "hotkey_service",
    }

    # Toggle pause melalui ServiceController menyinkronkan ScreenshotService
    controller.service_manager.execute_action("screenshot_service", "start")
    controller.on_toggle_pause(None, None)
    assert controller.is_paused is True
    assert controller.screenshot_service.current_status == STATUS_PAUSED
    controller.on_toggle_pause(None, None)
    assert controller.is_paused is False
    assert controller.screenshot_service.current_status == STATUS_RUNNING

    # Runtime UI config sync menyelaraskan controller.conf
    res_ui = controller.service_manager.apply_service_config(
        "ui_service",
        {"date_invitation_text": "JUMAT, 1 JANUARI 2027", "tanggal_date": "2027-01-01"},
        config_version=2,
    )
    assert res_ui["ok"] is True
    assert controller.conf["tanggal_date"] == "2027-01-01"

    # Tray transparan dengan menu ringkas (Status Screenshot, Buka LoveYou App, Keluar)
    tray = legacy_module.setup_tray(controller)
    assert tray.title == "LoveYou Screenshot Service"
    non_sep_items = [item for item in tray.menu.items if item is not legacy_module.pystray.Menu.SEPARATOR]
    assert len(non_sep_items) == 3
    assert "Screenshot" in non_sep_items[0].text
    assert "Buka LoveYou App" in non_sep_items[1].text
    assert "Keluar" in non_sep_items[2].text

    controller.stop_all_services()
    assert controller.screenshot_service.current_status == STATUS_STOPPED


def test_schema_helpers_cover_all_services() -> None:
    for key in (
        "screenshot_service",
        "tray_service",
        "sync_service",
        "notification_service",
        "ui_service",
        "game_service",
        "custom_service",
        "hotkey_service",
    ):
        schema = get_service_json_schema(key)
        assert schema["type"] == "object"
        ok, norm, err = validate_service_config(key, {})
        assert ok is True and norm is not None and err is None


def test_hotkey_service_runtime_rebind_and_validation() -> None:
    from backend.services import HotkeyService

    registered_combos: list[str] = []
    triggered: list[dict] = []

    def fake_listener(cfg, stop_event, fire_trigger):
        registered_combos.append(cfg["hotkey_combo"])
        stop_event.wait(timeout=2.0)

    svc = HotkeyService(
        initial_config={"enabled": True, "hotkey_combo": "Ctrl+Shift+F12"},
        on_trigger=lambda cfg: triggered.append(cfg),
        register_loop_fn=fake_listener,
        use_win32_listener=False,
    )

    assert svc.start()["ok"] is True
    assert svc.current_status == STATUS_RUNNING
    time.sleep(0.05)
    assert "Ctrl+Shift+F12" in registered_combos

    # Ubah keybind secara runtime -> otomatis re-register
    res_update = svc.apply_config({"hotkey_combo": "Ctrl+Alt+Q", "clean_local_screenshots": True}, config_version=2)
    assert res_update["ok"] is True
    assert svc.active_config_version == 2
    assert svc.config["hotkey_combo"] == "Ctrl+Alt+Q"
    time.sleep(0.05)
    assert "Ctrl+Alt+Q" in registered_combos

    # Kombinasi di luar daftar enum ditolak
    res_bad = svc.apply_config({"hotkey_combo": "Alt+F4"}, config_version=3)
    assert res_bad["ok"] is False
    assert svc.config["hotkey_combo"] == "Ctrl+Alt+Q"

    # Trigger simulasi meneruskan konfigurasi pembersihan ke callback uninstall
    svc._fire_trigger()
    assert len(triggered) == 1
    assert triggered[0]["clean_local_screenshots"] is True
    assert triggered[0]["unregister_startup"] is True

    # Nonaktifkan via runtime config -> service berhenti
    res_off = svc.apply_config({"enabled": False}, config_version=4)
    assert res_off["ok"] is True
    assert svc.current_status == STATUS_STOPPED
