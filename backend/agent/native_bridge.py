"""
NativeHostBridge: Jembatan IPC lokal berbasis direktori user-scoped
(%LOCALAPPDATA%/XooseLoveYouAgent/bridge/) dengan penulisan atomik
antara LoveYou Agent dan Native Messaging Host (host.py).

Tanpa membuka port HTTP lokal pada interface jaringan apa pun.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SUPPORTED_BROWSERS = ("chrome", "edge", "firefox")


def default_bridge_dir() -> Path:
    override = os.environ.get("XOOSE_BRIDGE_DIR", "").strip()
    if override:
        return Path(override).resolve()
    local_app_data = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return (Path(local_app_data) / "XooseLoveYouAgent" / "bridge").resolve()


def _atomic_write_json(target_path: Path, payload: dict[str, Any]) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_suffix(target_path.suffix + f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    last_err: Exception | None = None
    for attempt in range(6):
        try:
            os.replace(tmp_path, target_path)
            return
        except PermissionError as exc:
            last_err = exc
            time.sleep(0.03 * (attempt + 1))
    tmp_path.unlink(missing_ok=True)
    if last_err is not None:
        raise last_err


def _parse_iso_ts(raw: str | None) -> float | None:
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    try:
        if s.endswith("Z") or s.endswith("z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


class NativeHostBridge:
    """Mengelola sinkronisasi config runtime, control signal, status host, dan spool incoming."""

    def __init__(self, bridge_dir: str | Path | None = None):
        self.bridge_dir = Path(bridge_dir).resolve() if bridge_dir else default_bridge_dir()
        self.spool_root = self.bridge_dir / "spool"
        self.incoming_dir = self.spool_root / "incoming"
        self.runtime_config_file = self.bridge_dir / "runtime_config.json"
        self.control_signal_file = self.bridge_dir / "control_signal.json"
        self._lock = threading.RLock()
        self._control_seq = self._load_initial_seq()

        self.bridge_dir.mkdir(parents=True, exist_ok=True)
        self.incoming_dir.mkdir(parents=True, exist_ok=True)

    def _load_initial_seq(self) -> int:
        if not self.control_signal_file.exists():
            return 0
        try:
            data = json.loads(self.control_signal_file.read_text(encoding="utf-8"))
            return int(data.get("seq", 0)) if isinstance(data, dict) else 0
        except Exception:
            return 0

    def publish_runtime_config(
        self,
        config: dict[str, Any],
        config_version: int,
        device_id: str = "",
    ) -> Path:
        """Menulis runtime_config.json secara atomik agar segera di-push oleh host.py ke Extension."""
        policy = config.get("capture_policy") if isinstance(config.get("capture_policy"), dict) else {}
        browser_capture = config.get("browser_capture") if isinstance(config.get("browser_capture"), dict) else {}

        payload = {
            "config_version": int(config_version),
            "device_id": str(device_id or ""),
            "active_source": str(policy.get("active_source", "desktop_mss")),
            "browser_capture": browser_capture,
            "config": config,
            "published_at": datetime.now(timezone.utc).isoformat(),
        }
        with self._lock:
            _atomic_write_json(self.runtime_config_file, payload)
        return self.runtime_config_file

    def send_control_signal(self, action: str, source: str = "loveyou_agent") -> dict[str, Any]:
        """Mengirim sinyal allowlist (start/pause/resume/stop/sync_config) ke host.py."""
        clean_action = (action or "").strip().lower()
        if clean_action not in ("start", "pause", "resume", "stop", "sync_config"):
            raise ValueError(f"Control signal '{action}' tidak diizinkan.")
        with self._lock:
            self._control_seq += 1
            payload = {
                "seq": self._control_seq,
                "action": clean_action,
                "source": source,
                "sent_at": datetime.now(timezone.utc).isoformat(),
            }
            _atomic_write_json(self.control_signal_file, payload)
            return payload

    def read_host_statuses(self, stale_after_seconds: float = 90.0) -> dict[str, Any]:
        """Membaca file status per browser yang ditulis oleh native-host/host.py."""
        now_ts = datetime.now(timezone.utc).timestamp()
        browsers_map: dict[str, dict[str, Any]] = {}
        connected_browsers: list[str] = []
        primary_status: dict[str, Any] | None = None

        for b in SUPPORTED_BROWSERS:
            path = self.bridge_dir / f"host_status_{b}.json"
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue
            except Exception:
                continue

            updated_ts = _parse_iso_ts(data.get("updated_at"))
            is_fresh = updated_ts is not None and (now_ts - updated_ts) <= stale_after_seconds
            is_connected = bool(data.get("extension_connected", False)) and is_fresh
            entry = dict(data)
            entry["is_fresh"] = is_fresh
            entry["extension_connected"] = is_connected
            browsers_map[b] = entry

            if is_connected:
                connected_browsers.append(b)
                if primary_status is None or entry.get("capture_state") == "capturing":
                    primary_status = entry

        if primary_status is None and browsers_map:
            # Pilih laporan terbaru sebagai referensi diagnostik
            primary_status = max(
                browsers_map.values(),
                key=lambda item: _parse_iso_ts(item.get("updated_at")) or 0.0,
            )

        return {
            "extension_connected": len(connected_browsers) > 0,
            "connected_browsers": connected_browsers,
            "browsers": browsers_map,
            "primary": primary_status or {},
        }

    def consume_incoming_spool(self, max_items: int = 25) -> list[dict[str, Any]]:
        """
        Mengambil pasangan gambar + metadata yang sudah selesai ditulis secara atomik
        di spool/incoming/. Memastikan path gambar berada di dalam incoming_dir.
        """
        if not self.incoming_dir.exists():
            return []

        items: list[dict[str, Any]] = []
        meta_files = sorted(self.incoming_dir.glob("*.meta.json"), key=lambda p: p.stat().st_mtime)

        for meta_path in meta_files[: max(1, int(max_items))]:
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                if not isinstance(meta, dict):
                    meta_path.unlink(missing_ok=True)
                    continue
            except Exception:
                continue

            image_filename = str(meta.get("image_filename", "")).strip()
            if not image_filename or "/" in image_filename or "\\" in image_filename or ".." in image_filename:
                meta_path.unlink(missing_ok=True)
                continue

            image_path = (self.incoming_dir / image_filename).resolve()
            try:
                image_path.relative_to(self.incoming_dir.resolve())
            except ValueError:
                meta_path.unlink(missing_ok=True)
                continue

            if not image_path.exists():
                continue

            # Hapus file .meta.json setelah berhasil dibaca agar tidak diproses dua kali
            try:
                meta_path.unlink(missing_ok=True)
            except Exception:
                continue

            items.append({
                "image_path": str(image_path),
                "metadata": meta,
            })

        return items
