"""
Modul layanan modular LoveYou (BaseService, ServiceManager, dan 7 service terpisah).
"""

from .base import (
    ALLOWED_LIFECYCLE_ACTIONS,
    STATUS_FAILED,
    STATUS_PAUSED,
    STATUS_RUNNING,
    STATUS_STOPPED,
    STATUS_UNKNOWN,
    BaseService,
)
from .browser_capture import BrowserCaptureService
from .custom import CustomService
from .game import GameService
from .hotkey import HotkeyService
from .manager import ServiceManager
from .notification import NotificationService
from .screenshot import ScreenshotService
from .sync import SyncService
from .tray import TrayService
from .ui import UiService

__all__ = [
    "BaseService",
    "ServiceManager",
    "BrowserCaptureService",
    "ScreenshotService",
    "TrayService",
    "SyncService",
    "NotificationService",
    "UiService",
    "GameService",
    "CustomService",
    "HotkeyService",
    "STATUS_RUNNING",
    "STATUS_STOPPED",
    "STATUS_PAUSED",
    "STATUS_FAILED",
    "STATUS_UNKNOWN",
    "ALLOWED_LIFECYCLE_ACTIONS",
]
