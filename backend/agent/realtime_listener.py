"""
RealtimeCommandListener: Listener opsional untuk trigger cepat notifikasi command
via Supabase Realtime. Sesuai prinsip keamanan blueprint, sinyal realtime HANYA
berfungsi sebagai wake-up trigger agar Agent melakukan HTTPS polling terverifikasi
ke API Xoose — tidak pernah mengeksekusi payload mentah secara langsung.
"""

from __future__ import annotations

import threading
from typing import Any, Callable


class RealtimeCommandListener:
    """Trigger cepat opsional berbasis channel device; otomatis fallback ke HTTPS polling."""

    def __init__(
        self,
        device_id: str,
        poll_trigger_event: threading.Event,
        supabase_url: str = "",
        supabase_key: str = "",
        enabled: bool = False,
        on_event_received: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.device_id = device_id
        self.poll_trigger_event = poll_trigger_event
        self.supabase_url = supabase_url
        self.supabase_key = supabase_key
        self.enabled = bool(enabled)
        self._on_event_received = on_event_received
        self._stop_event = threading.Event()
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    def handle_wake_signal(self, event_payload: dict[str, Any] | None = None) -> None:
        """
        Menerima sinyal wake-up dari channel realtime perangkat dan memicu
        HTTPS polling segera pada thread Agent utama.
        """
        payload = event_payload or {}
        target = str(payload.get("device_id", "")).strip()
        if target and self.device_id and target != self.device_id:
            return
        self.poll_trigger_event.set()
        if self._on_event_received:
            try:
                self._on_event_received(payload)
            except Exception:
                pass

    def start(self) -> bool:
        if not self.enabled or not self.supabase_url or not self.supabase_key or not self.device_id:
            self._running = False
            return False
        self._stop_event.clear()
        self._running = True
        return True

    def stop(self) -> None:
        self._stop_event.set()
        self._running = False
