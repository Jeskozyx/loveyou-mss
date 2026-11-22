"""
Skema konfigurasi runtime berbasis Pydantic untuk 7 layanan modular LoveYou.
Digunakan oleh ServiceManager & ConfigSyncManager untuk memvalidasi perubahan
konfigurasi runtime sebelum diterapkan (dengan dukungan rollback otomatis).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ScreenshotServiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interval_seconds: int = Field(default=3, ge=1, le=3600, description="Jeda antar tangkapan layar (detik).")
    image_format: Literal["webp", "jpg", "jpeg", "png"] = Field(default="webp", description="Format gambar kompresi.")
    jpg_quality: int = Field(default=75, ge=10, le=100, description="Kualitas kompresi gambar (10-100).")
    keep_local_file: bool = Field(default=False, description="Simpan salinan file screenshot secara lokal.")
    local_folder: str = Field(default="screenshots", min_length=1, max_length=260, description="Folder penyimpanan lokal.")


class TrayServiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    icon: str = Field(default="heart.ico", min_length=1, max_length=120, description="Nama file ikon tray (.ico).")
    app_name: str = Field(default="LoveYou", min_length=1, max_length=100, description="Nama tampilan aplikasi.")
    minimalist_tooltip: bool = Field(default=True, description="Gunakan tooltip minimalis di System Tray.")


class SyncServiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    supabase_url: str = Field(default="", max_length=500, description="URL Supabase project (tanpa service_role key).")
    table_name: str = Field(default="hasil_ss", min_length=1, max_length=100, description="Nama tabel log screenshot.")
    bucket_name: str = Field(default="screenshots", min_length=1, max_length=100, description="Nama bucket Storage.")
    max_queue_size: int = Field(default=200, ge=10, le=2000, description="Kapasitas maksimum antrean upload.")
    max_upload_retries: int = Field(default=6, ge=1, le=20, description="Batas percobaan ulang upload jaringan.")
    retry_backoff_seconds: float = Field(default=4.0, ge=0.5, le=60.0, description="Jeda backoff retry (detik).")


class NotificationServiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=False, description="Aktifkan notifikasi sistem Windows.")
    notify_on_capture: bool = Field(default=False, description="Tampilkan notifikasi setiap screenshot.")
    notify_on_config_change: bool = Field(default=False, description="Tampilkan notifikasi perubahan konfigurasi.")
    notify_on_command: bool = Field(default=False, description="Tampilkan notifikasi saat remote command dieksekusi.")


class UiServiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window_title: str = Field(default="❤️ LoveYou", min_length=1, max_length=120, description="Judul jendela UI.")
    date_invitation_text: str = Field(default="SABTU, 5 DESEMBER 2026", min_length=1, max_length=200, description="Teks jadwal undangan.")
    date_countdown_target: str = Field(default="2026-12-05T12:00:00Z", min_length=4, max_length=64, description="Target ISO 8601 countdown.")
    tanggal_date: str = Field(default="2026-12-05", min_length=8, max_length=32, description="Tanggal target YYYY-MM-DD.")
    width: int = Field(default=560, ge=320, le=1920, description="Lebar jendela UI.")
    height: int = Field(default=760, ge=400, le=1200, description="Tinggi jendela UI.")
    theme: str = Field(default="romantic-dark", min_length=1, max_length=64, description="Nama tema antarmuka.")


class GameServiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=False, description="Aktifkan modul layanan game.")
    game_mode: str = Field(default="idle", min_length=1, max_length=64, description="Mode aktif modul game.")
    tick_rate_hz: int = Field(default=1, ge=1, le=60, description="Frekuensi tick loop game service (Hz).")
    auto_save_interval_seconds: int = Field(default=60, ge=5, le=3600, description="Interval autosave state game.")
    custom_params: dict[str, Any] = Field(default_factory=dict, description="Parameter tambahan spesifik game.")


class CustomServiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=False, description="Aktifkan custom plugin service.")
    plugin_name: str = Field(default="default_plugin", min_length=1, max_length=100, description="Nama modul plugin.")
    parameters: dict[str, Any] = Field(default_factory=dict, description="Parameter kustom bebas untuk plugin.")


SERVICE_SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "screenshot_service": ScreenshotServiceConfig,
    "tray_service": TrayServiceConfig,
    "sync_service": SyncServiceConfig,
    "notification_service": NotificationServiceConfig,
    "ui_service": UiServiceConfig,
    "game_service": GameServiceConfig,
    "custom_service": CustomServiceConfig,
}


def get_service_json_schema(service_key: str) -> dict[str, Any]:
    """Mengembalikan JSON Schema standar untuk service tertentu."""
    model = SERVICE_SCHEMA_MODELS.get(service_key)
    if not model:
        return {"type": "object", "properties": {}, "additionalProperties": True}
    return model.model_json_schema()


def validate_service_config(
    service_key: str,
    config: dict[str, Any],
    base_config: dict[str, Any] | None = None,
) -> tuple[bool, dict[str, Any] | None, str | None]:
    """
    Memvalidasi konfigurasi runtime (penuh atau partial patch terhadap base_config).
    Returns: (is_valid, normalized_dict_or_None, error_message_or_None)
    """
    if not isinstance(config, dict):
        return False, None, "Konfigurasi harus berupa JSON object (dict)."

    model = SERVICE_SCHEMA_MODELS.get(service_key)
    if not model:
        merged = dict(base_config or {})
        merged.update(config)
        return True, merged, None

    merged = dict(base_config or {})
    merged.update(config)

    try:
        validated = model.model_validate(merged)
        return True, validated.model_dump(), None
    except ValidationError as exc:
        return False, None, str(exc)
