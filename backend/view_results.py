"""
==============================================================================
VIEWER HASIL SCREENSHOT (SUPABASE & LOKAL)
==============================================================================
Script ini memfasilitasi pengguna untuk melihat hasil tangkapan layar:
1. Mengambil data dari tabel Supabase `hasil_ss`.
2. Menampilkan tabel ringkasan di konsol / terminal.
3. Menghasilkan galeri HTML interaktif dan membukanya otomatis di browser.
==============================================================================
"""

import os
import sys
import webbrowser
import configparser
from datetime import datetime
from supabase import create_client


def get_base_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def load_config() -> dict:
    base_dir = get_base_dir()
    config_path = os.path.join(base_dir, "config.ini")
    config = configparser.ConfigParser()

    conf = {
        "url": "https://wqjfunxfngowxwdmyzyj.supabase.co",
        "key": "sb_publishable_u2lBe4CRF_2XEqbkniBjbw_p0gGEXgZ",
        "table": "hasil_ss",
        "bucket": "screenshots",
    }
    if os.path.exists(config_path):
        config.read(config_path, encoding="utf-8")
        if "SUPABASE" in config:
            conf["url"] = config["SUPABASE"].get("url", conf["url"]).strip()
            conf["key"] = config["SUPABASE"].get("key", conf["key"]).strip()
            conf["table"] = config["SUPABASE"].get("table_name", conf["table"]).strip()
            conf["bucket"] = config["SUPABASE"].get("bucket_name", conf["bucket"]).strip()
    return conf


def generate_html_gallery(records: list[dict], output_path: str, supabase_url: str):
    """Membuat file HTML galeri modern untuk melihat screenshot dengan mudah."""
    rows_html = ""
    for r in records:
        img_url = r.get("image_url") or ""
        created_at = r.get("created_at") or "-"
        try:
            dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            created_str = dt.strftime("%d %b %Y, %H:%M:%S")
        except Exception:
            created_str = created_at

        filename = r.get("filename") or "Unknown"
        device = r.get("device_name") or "-"
        active_window = r.get("active_window") or "-"
        resolution = r.get("resolution") or "-"
        file_size = r.get("file_size_kb") or 0

        # Jika image_url kosong atau tidak valid, coba fallback ke storage URL
        if not img_url and r.get("storage_path"):
            img_url = f"{supabase_url}/storage/v1/object/public/{r['storage_path']}"

        img_tag = (
            f'<a href="{img_url}" target="_blank"><img src="{img_url}" loading="lazy" alt="{filename}" class="thumb" onerror="this.src=\'data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 width=%22300%22 height=%22180%22><rect width=%22100%%22 height=%22100%%22 fill=%22%23333%22/><text x=%2250%%22 y=%2250%%22 fill=%22%23aaa%22 font-size=%2214%22 text-anchor=%22middle%22 dy=%22.3em%22>Gambar Tidak Dapat Dimuat</text></svg>\'"/></a>'
            if img_url
            else '<div class="no-img">Belum Terupload ke Storage</div>'
        )

        rows_html += f"""
        <div class="card">
            <div class="img-container">
                {img_tag}
            </div>
            <div class="card-info">
                <div class="card-title" title="{active_window}">📌 <strong>Jendela:</strong> {active_window}</div>
                <div class="card-meta">
                    <span>🕒 {created_str}</span>
                    <span>💻 {device}</span>
                    <span>📐 {resolution} ({file_size} KB)</span>
                </div>
                <div class="card-actions">
                    <span class="badge">{filename}</span>
                    {f'<a href="{img_url}" target="_blank" class="btn-view">Buka Gambar ↗</a>' if img_url else ''}
                </div>
            </div>
        </div>
        """

    html_content = f"""<!DOCTYPE html>
<html lang="id">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Galeri Hasil Screenshot - LoveYou Service</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
        body {{ background-color: #0f172a; color: #f8fafc; padding: 24px; }}
        header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; padding-bottom: 16px; border-bottom: 1px solid #334155; }}
        h1 {{ font-size: 24px; display: flex; align-items: center; gap: 8px; color: #f43f5e; }}
        .stats {{ color: #94a3b8; font-size: 14px; }}
        .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 20px; }}
        .card {{ background: #1e293b; border-radius: 12px; overflow: hidden; border: 1px solid #334155; transition: transform 0.2s, box-shadow 0.2s; display: flex; flex-direction: column; }}
        .card:hover {{ transform: translateY(-4px); box-shadow: 0 10px 20px rgba(0,0,0,0.4); border-color: #f43f5e; }}
        .img-container {{ width: 100%; height: 190px; background: #0b0f19; overflow: hidden; display: flex; align-items: center; justify-content: center; }}
        .thumb {{ width: 100%; height: 100%; object-fit: cover; transition: transform 0.3s; }}
        .thumb:hover {{ transform: scale(1.04); }}
        .no-img {{ color: #64748b; font-size: 13px; text-align: center; padding: 20px; }}
        .card-info {{ padding: 14px; flex-grow: 1; display: flex; flex-direction: column; gap: 8px; }}
        .card-title {{ font-size: 14px; font-weight: 500; color: #e2e8f0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
        .card-meta {{ display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: #94a3b8; }}
        .card-actions {{ margin-top: auto; pt: 8px; display: flex; justify-content: space-between; align-items: center; padding-top: 8px; border-top: 1px solid #273549; }}
        .badge {{ background: #334155; color: #cbd5e1; font-size: 11px; padding: 3px 8px; border-radius: 6px; font-family: monospace; }}
        .btn-view {{ font-size: 12px; color: #38bdf8; text-decoration: none; font-weight: 600; }}
        .btn-view:hover {{ text-decoration: underline; color: #7dd3fc; }}
        .btn-refresh {{ background: #f43f5e; color: white; border: none; padding: 8px 16px; border-radius: 8px; font-weight: 600; cursor: pointer; text-decoration: none; }}
        .btn-refresh:hover {{ background: #e11d48; }}
    </style>
</head>
<body>
    <header>
        <div>
            <h1>❤️ Galeri Hasil Screenshot</h1>
            <p class="stats">Total {len(records)} screenshot tersimpan di database Supabase</p>
        </div>
        <div>
            <button class="btn-refresh" onclick="location.reload()">🔄 Refresh Data</button>
        </div>
    </header>
    <main>
        <div class="grid">
            {rows_html if rows_html else '<p style="color:#94a3b8; grid-column: 1/-1; text-align: center; padding: 40px;">Belum ada screenshot yang tercatat di tabel Supabase.</p>'}
        </div>
    </main>
</body>
</html>
"""
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_content)


def view_results(open_browser: bool = True):
    conf = load_config()
    print("=" * 65)
    print("   MEMUAT DATA HASIL SCREENSHOT DARI SUPABASE")
    print("=" * 65)
    print(f" URL: {conf['url']}")
    print(f" Tabel: {conf['table']}")
    print("-" * 65)

    try:
        supabase = create_client(conf["url"], conf["key"])
        res = supabase.table(conf["table"]).select("*").order("created_at", desc=True).limit(50).execute()
        records = res.data or []
    except Exception as e:
        print(f"[ERROR] Gagal membaca data dari Supabase: {e}")
        return

    print(f"[OK] Berhasil mengambil {len(records)} data screenshot terbaru.\n")
    if records:
        print(f"{'ID':<6} | {'Waktu (UTC)':<20} | {'Ukuran':<10} | {'Jendela Aktif':<30} | {'URL Gambar'}")
        print("-" * 95)
        for r in records[:10]:
            rid = str(r.get("id", "-"))
            created = str(r.get("created_at", "-"))[:19]
            size = f"{r.get('file_size_kb', 0)} KB"
            window = (r.get("active_window") or "-")[:28]
            url = r.get("image_url") or "(Belum diupload)"
            print(f"{rid:<6} | {created:<20} | {size:<10} | {window:<30} | {url[:40]}...")
        if len(records) > 10:
            print(f"... dan {len(records) - 10} data lainnya.")

    # Buat file galeri HTML
    base_dir = get_base_dir()
    html_path = os.path.join(base_dir, "hasil_ss_gallery.html")
    generate_html_gallery(records, html_path, conf["url"])
    print(f"\n[OK] Galeri visual disimpan di: {html_path}")

    if open_browser:
        print("[INFO] Membuka galeri di browser...")
        webbrowser.open(f"file:///{os.path.abspath(html_path)}")


if __name__ == "__main__":
    view_results(open_browser=True)
