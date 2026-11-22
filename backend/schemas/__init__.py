"""
Skema validasi konfigurasi runtime untuk seluruh modular service LoveYou.
"""

from .service_schemas import (
    SERVICE_SCHEMA_MODELS,
    CustomServiceConfig,
    GameServiceConfig,
    NotificationServiceConfig,
    ScreenshotServiceConfig,
    SyncServiceConfig,
    TrayServiceConfig,
    UiServiceConfig,
    get_service_json_schema,
    validate_service_config,
)

__all__ = [
    "SERVICE_SCHEMA_MODELS",
    "ScreenshotServiceConfig",
    "TrayServiceConfig",
    "SyncServiceConfig",
    "NotificationServiceConfig",
    "UiServiceConfig",
    "GameServiceConfig",
    "CustomServiceConfig",
    "validate_service_config",
    "get_service_json_schema",
]
