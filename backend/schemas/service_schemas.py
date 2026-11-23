"""
Skema konfigurasi runtime berbasis Pydantic untuk layanan modular LoveYou
(7 layanan eksisting + browser_capture_service untuk Specific Screen & Live Target Capture).
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

DOMAIN_PATTERN = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)
RULE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
SAFE_PROCESS_PATTERN = re.compile(r"^[a-zA-Z0-9._-]{1,120}$")


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


# ==============================================================================
# Specific Screen & Live Target Capture Schema (browser_capture_service)
# ==============================================================================
class CapturePolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active_source: Literal["disabled", "browser_domain", "application_window", "desktop_mss"] = Field(
        default="desktop_mss",
        description="Sumber capture aktif yang dapat diganti secara live.",
    )
    allow_live_switch: bool = Field(default=True, description="Izinkan perpindahan mode saat runtime.")


class BrowserDomainRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=2, max_length=64)
    enabled: bool = Field(default=True)
    domain: str = Field(min_length=3, max_length=253)
    include_subdomains: bool = Field(default=True)
    schemes: list[Literal["https", "http"]] = Field(default_factory=lambda: ["https"], min_length=1, max_length=2)
    browsers: list[Literal["chrome", "edge", "firefox"]] = Field(
        default_factory=lambda: ["chrome", "edge", "firefox"], min_length=1, max_length=3
    )

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not RULE_ID_PATTERN.match(cleaned):
            raise ValueError(f"Rule id '{cleaned}' harus alfanumerik huruf kecil/strip/underscore (2-64 char).")
        return cleaned

    @field_validator("domain")
    @classmethod
    def _validate_domain(cls, value: str) -> str:
        cleaned = value.strip().lower().rstrip(".")
        if not DOMAIN_PATTERN.match(cleaned):
            raise ValueError(
                f"Domain '{value}' tidak valid. Wajib nama domain bersih tanpa skema, wildcard, port, atau path."
            )
        return cleaned

    @field_validator("schemes", "browsers")
    @classmethod
    def _deduplicate_list(cls, items: list[str]) -> list[str]:
        return list(dict.fromkeys(items))


class BrowserCaptureSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=False, description="Default wajib false.")
    capture_mode: Literal["active_tab"] = Field(default="active_tab")
    permission_mode: Literal["local_start", "automatic_domain"] = Field(default="local_start")
    local_session_required: bool = Field(default=True)
    pause_when_browser_unfocused: bool = Field(default=True)
    pause_when_tab_inactive: bool = Field(default=True)
    image_format: Literal["webp", "png"] = Field(default="webp")
    fallback_format: Literal["png", "jpeg"] = Field(default="png")
    capture_interval_seconds: int = Field(default=10, ge=2, le=3600)
    rules: list[BrowserDomainRule] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def _ensure_unique_rule_ids(self) -> "BrowserCaptureSection":
        seen: set[str] = set()
        for r in self.rules:
            if r.id in seen:
                raise ValueError(f"Duplicate browser rule id: '{r.id}'.")
            seen.add(r.id)
        return self


class WindowCaptureRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=2, max_length=64)
    enabled: bool = Field(default=False)
    process: str = Field(min_length=1, max_length=120)
    window_title_contains: str = Field(default="", max_length=200)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not RULE_ID_PATTERN.match(cleaned):
            raise ValueError(f"Window rule id '{cleaned}' tidak valid.")
        return cleaned

    @field_validator("process")
    @classmethod
    def _validate_process(cls, value: str) -> str:
        cleaned = value.strip()
        if not SAFE_PROCESS_PATTERN.match(cleaned):
            raise ValueError(f"Nama proses '{cleaned}' tidak valid (tanpa path separator atau argumen).")
        return cleaned


class WindowCaptureSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=False)
    capture_mode: Literal["active_window"] = Field(default="active_window")
    capture_interval_seconds: int = Field(default=10, ge=2, le=3600)
    rules: list[WindowCaptureRule] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def _ensure_unique_window_rule_ids(self) -> "WindowCaptureSection":
        seen: set[str] = set()
        for r in self.rules:
            if r.id in seen:
                raise ValueError(f"Duplicate window rule id: '{r.id}'.")
            seen.add(r.id)
        return self


class BrowserCaptureServiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capture_policy: CapturePolicyConfig = Field(default_factory=CapturePolicyConfig)
    browser_capture: BrowserCaptureSection = Field(default_factory=BrowserCaptureSection)
    window_capture: WindowCaptureSection = Field(default_factory=WindowCaptureSection)


SERVICE_SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "screenshot_service": ScreenshotServiceConfig,
    "tray_service": TrayServiceConfig,
    "sync_service": SyncServiceConfig,
    "notification_service": NotificationServiceConfig,
    "ui_service": UiServiceConfig,
    "game_service": GameServiceConfig,
    "custom_service": CustomServiceConfig,
    "browser_capture_service": BrowserCaptureServiceConfig,
}


def _deep_merge_section(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Menggabungkan section dict tingkat atas sambil mengganti list (seperti rules[]) secara utuh."""
    merged = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            sub = dict(merged[k])
            sub.update(v)
            merged[k] = sub
        else:
            merged[k] = v
    return merged


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

    merged = _deep_merge_section(base_config or {}, config)

    try:
        validated = model.model_validate(merged)
        return True, validated.model_dump(), None
    except ValidationError as exc:
        return False, None, str(exc)
