"""
==============================================================================
VIEWER HASIL SCREENSHOT DENGAN FILTER PER-USER (SUPABASE & LOKAL)
==============================================================================
Script ini memfasilitasi pengguna untuk melihat hasil tangkapan layar:
1. Mengambil data dari tabel Supabase `hasil_ss`.
2. Mendukung pemisahan hasil screenshot per-user (misal: roedi, jiawyk, dll).
3. Menyediakan filter interaktif per-user di galeri HTML.
4. Menampilkan tabel ringkasan di konsol / terminal.
==============================================================================
"""

import os
import sys
import re
import argparse
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


def extract_user_from_record(r: dict) -> str:
    """Mengekstrak nama user dari record: kolom user_name, notes, atau storage_path."""
    if r.get("user_name") and r["user_name"].strip():
        return r["user_name"].strip()

    # Ekstrak dari notes: 'User: roedi | Window: ...'
    notes = r.get("notes") or ""
    m = re.search(r"User:\s*([^\s\|]+)", notes)
    if m:
        return m.group(1).strip()

    # Ekstrak dari storage_path: 'screenshots/roedi/ss_...jpg'
    spath = r.get("storage_path") or ""
    parts = spath.replace("\\", "/").split("/")
    if len(parts) >= 3 and parts[0] == "screenshots":
        return parts[1]

    return "anonymous"


def generate_html_gallery(records: list[dict], output_path: str, supabase_url: str, default_user: str = "ALL"):
    """Membuat file HTML galeri modern dengan filter interaktif per-user."""
    # Kumpulkan daftar user unik
    user_counts = {}
    for r in records:
        u = extract_user_from_record(r)
        user_counts[u] = user_counts.get(u, 0) + 1

    # Bangun tombol filter user
    filter_buttons_html = f"""
    <button class="filter-btn active" data-filter="ALL" onclick="filterUser('ALL')">
        👥 Semua ({len(records)})
    </button>
    """
    for u, count in sorted(user_counts.items(), key=lambda x: x[0]):
        filter_buttons_html += f"""
        <button class="filter-btn" data-filter="{u}" onclick="filterUser('{u}')">
            👤 {u} ({count})
        </button>
        """

    # Bangun kartu-kartu screenshot
    rows_html = ""
    for r in records:
        user_name = extract_user_from_record(r)
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

        if not img_url and r.get("storage_path"):
            img_url = f"{supabase_url}/storage/v1/object/public/{r['storage_path']}"

        img_tag = (
            f'<a href="{img_url}" target="_blank"><img src="{img_url}" loading="lazy" alt="{filename}" class="thumb" onerror="this.src=\'data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 width=%22300%22 height=%22180%22><rect width=%22100%%22 height=%22100%%22 fill=%22%231a0010%22/><text x=%2250%%22 y=%2250%%22 fill=%22%23e8225a%22 font-size=%2214%22 text-anchor=%22middle%22 dy=%22.3em%22>Gambar Tidak Dapat Dimuat</text></svg>\'"/></a>'
            if img_url
            else '<div class="no-img">Belum Terupload ke Storage</div>'
        )

        badge_class = "user-badge-named" if user_name != "anonymous" else "user-badge-anon"

        rows_html += f"""
        <div class="card" data-user="{user_name}">
            <div class="img-container">
                {img_tag}
                <div class="card-overlay-user {badge_class}">👤 {user_name}</div>
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
    <title>Galeri Screenshot per User - LoveYou Service</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
        body {{ background-color: #0d0007; color: #fce4ec; padding: 24px; min-height: 100vh; }}
        header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; padding-bottom: 16px; border-bottom: 2px solid #e8225a33; flex-wrap: wrap; gap: 12px; }}
        h1 {{ font-size: 24px; display: flex; align-items: center; gap: 8px; color: #ff6b9d; text-shadow: 0 0 12px #e8225a66; }}
        .stats {{ color: #c97ea8; font-size: 14px; margin-top: 4px; }}
        
        /* Filter Bar */
        .filter-section {{ margin-bottom: 24px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; background: #1a0010; padding: 12px 16px; border-radius: 8px; border: 1px solid #e8225a44; }}
        .filter-label {{ font-size: 13px; font-weight: 600; color: #f5c842; letter-spacing: 1px; text-transform: uppercase; }}
        .filter-btn {{ background: transparent; color: #c97ea8; border: 1px solid #e8225a44; padding: 6px 14px; border-radius: 20px; font-size: 13px; cursor: pointer; transition: all 0.2s; font-weight: 500; }}
        .filter-btn:hover {{ background: #e8225a22; color: #fff; border-color: #e8225a; }}
        .filter-btn.active {{ background: #e8225a; color: #fff; border-color: #e8225a; box-shadow: 0 0 12px #e8225a88; }}
        
        .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 20px; }}
        .card {{ background: #1a0010; border-radius: 10px; overflow: hidden; border: 1px solid #e8225a33; transition: transform 0.2s, box-shadow 0.2s; display: flex; flex-direction: column; }}
        .card:hover {{ transform: translateY(-4px); box-shadow: 0 8px 24px #e8225a33; border-color: #e8225a; }}
        .img-container {{ width: 100%; height: 190px; background: #050003; overflow: hidden; display: flex; align-items: center; justify-content: center; position: relative; }}
        .thumb {{ width: 100%; height: 100%; object-fit: cover; transition: transform 0.3s; }}
        .thumb:hover {{ transform: scale(1.04); }}
        .card-overlay-user {{ position: absolute; top: 10px; right: 10px; font-size: 11px; font-weight: 700; padding: 3px 10px; border-radius: 12px; backdrop-filter: blur(4px); }}
        .user-badge-named {{ background: rgba(232, 34, 90, 0.85); color: #fff; box-shadow: 0 0 8px #e8225a; }}
        .user-badge-anon {{ background: rgba(50, 50, 50, 0.7); color: #ccc; }}
        .no-img {{ color: #c97ea8; font-size: 13px; text-align: center; padding: 20px; }}
        .card-info {{ padding: 14px; flex-grow: 1; display: flex; flex-direction: column; gap: 8px; }}
        .card-title {{ font-size: 14px; font-weight: 500; color: #fdf3e3; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
        .card-meta {{ display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: #c97ea8; }}
        .card-actions {{ margin-top: auto; display: flex; justify-content: space-between; align-items: center; padding-top: 8px; border-top: 1px solid #300020; }}
        .badge {{ background: #260017; color: #ffd6e7; font-size: 11px; padding: 3px 8px; border-radius: 4px; font-family: monospace; border: 1px solid #e8225a22; }}
        .btn-view {{ font-size: 12px; color: #f5c842; text-decoration: none; font-weight: 600; }}
        .btn-view:hover {{ text-decoration: underline; color: #ffe082; }}
        .btn-refresh {{ background: #e8225a; color: white; border: none; padding: 8px 18px; border-radius: 6px; font-weight: 600; cursor: pointer; text-decoration: none; box-shadow: 0 0 10px #e8225a66; }}
        .btn-refresh:hover {{ background: #ff2a6d; }}
    </style>
</head>
<body>
    <header>
        <div>
            <h1>❤️ Galeri Screenshot per User</h1>
            <p class="stats">Total {len(records)} screenshot tersimpan di database Supabase</p>
        </div>
        <div>
            <button class="btn-refresh" onclick="location.reload()">🔄 Refresh Data</button>
        </div>
    </header>

    <!-- Filter per User -->
    <div class="filter-section">
        <span class="filter-label">Filter User:</span>
        {filter_buttons_html}
    </div>

    <main>
        <div class="grid" id="galleryGrid">
            {rows_html if rows_html else '<p style="color:#c97ea8; grid-column: 1/-1; text-align: center; padding: 40px;">Belum ada screenshot yang tercatat di tabel Supabase.</p>'}
        </div>
    </main>

    <script>
        function filterUser(selectedUser) {{
            // Update tombol active
            document.querySelectorAll('.filter-btn').forEach(btn => {{
                if (btn.getAttribute('data-filter') === selectedUser) {{
                    btn.classList.add('active');
                }} else {{
                    btn.classList.remove('active');
                }}
            }});

            // Filter kartu
            const cards = document.querySelectorAll('.card');
            let visibleCount = 0;
            cards.forEach(card => {{
                const user = card.getAttribute('data-user');
                if (selectedUser === 'ALL' || user === selectedUser) {{
                    card.style.display = 'flex';
                    visibleCount++;
                }} else {{
                    card.style.display = 'none';
                }}
            }});
        }}

        // Inisialisasi default filter jika ada
        const defaultFilter = "{default_user}";
        if (defaultFilter && defaultFilter !== "ALL") {{
            filterUser(defaultFilter);
        }}
    </script>
</body>
</html>
"""
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_content)


def view_results(open_browser: bool = True, filter_user: str = None):
    conf = load_config()
    print("=" * 65)
    print("   MEMUAT DATA HASIL SCREENSHOT DARI SUPABASE")
    print("=" * 65)
    print(f" URL    : {conf['url']}")
    print(f" Tabel  : {conf['table']}")
    if filter_user:
        print(f" Filter : User '{filter_user}'")
    print("-" * 65)

    try:
        supabase = create_client(conf["url"], conf["key"])
        query = supabase.table(conf["table"]).select("*").order("created_at", desc=True).limit(100)
        res = query.execute()
        records = res.data or []
    except Exception as e:
        print(f"[ERROR] Gagal membaca data dari Supabase: {e}")
        return

    # Filter records jika diminta
    if filter_user:
        records = [r for r in records if extract_user_from_record(r).lower() == filter_user.lower()]

    print(f"[OK] Berhasil memuat {len(records)} data screenshot.\n")
    if records:
        print(f"{'ID':<6} | {'User':<12} | {'Waktu (UTC)':<19} | {'Ukuran':<9} | {'Jendela Aktif':<25}")
        print("-" * 90)
        for r in records[:15]:
            rid = str(r.get("id", "-"))
            user = extract_user_from_record(r)[:11]
            created = str(r.get("created_at", "-"))[:19]
            size = f"{r.get('file_size_kb', 0)} KB"
            window = (r.get("active_window") or "-")[:24]
            print(f"{rid:<6} | {user:<12} | {created:<19} | {size:<9} | {window:<25}")
        if len(records) > 15:
            print(f"... dan {len(records) - 15} data lainnya.")

    # Buat file galeri HTML
    base_dir = get_base_dir()
    html_path = os.path.join(base_dir, "hasil_ss_gallery.html")
    generate_html_gallery(records, html_path, conf["url"], default_user=filter_user or "ALL")
    print(f"\n[OK] Galeri visual dengan filter per-user disimpan di: {html_path}")

    if open_browser:
        print("[INFO] Membuka galeri di browser...")
        webbrowser.open(f"file:///{os.path.abspath(html_path)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Viewer Galeri Hasil Screenshot per User")
    parser.add_argument("--user", type=str, default=None, help="Filter screenshot hanya untuk user tertentu (misal: roedi)")
    parser.add_argument("--no-browser", action="store_true", help="Jangan buka browser otomatis")
    args = parser.parse_args()

    view_results(open_browser=not args.no_browser, filter_user=args.user)
