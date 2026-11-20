"""
==============================================================================
SETUP & DIAGNOSTIC CHECKER UNTUK SUPABASE
==============================================================================
Script ini memvalidasi koneksi ke Supabase, memeriksa apakah tabel hasil_ss
dan bucket screenshots sudah dibuat di Supabase, serta memberikan panduan
jika ada komponen yang belum disiapkan.
==============================================================================
"""

import sys
import configparser
from supabase import create_client

def main():
    print("=" * 65)
    print("   PEMERIKSAAN KONEKSI & STRUKTUR TABEL SUPABASE")
    print("=" * 65)

    config = configparser.ConfigParser()
    config.read("config.ini", encoding="utf-8")

    url = config.get("SUPABASE", "url", fallback="https://wqjfunxfngowxwdmyzyj.supabase.co").strip()
    key = config.get("SUPABASE", "key", fallback="sb_publishable_u2lBe4CRF_2XEqbkniBjbw_p0gGEXgZ").strip()
    table_name = config.get("SUPABASE", "table_name", fallback="hasil_ss").strip()
    bucket_name = config.get("SUPABASE", "bucket_name", fallback="screenshots").strip()

    print(f"[*] URL Project  : {url}")
    print(f"[*] API Key      : {key[:15]}...{key[-5:]}")
    print(f"[*] Target Tabel : {table_name}")
    print(f"[*] Target Bucket: {bucket_name}")
    print("-" * 65)

    # 1. Inisialisasi Client
    try:
        supabase = create_client(url, key)
        print("[+] [OK] Inisialisasi Supabase Client Berhasil.")
    except Exception as e:
        print(f"[-] [FAIL] Gagal inisialisasi Supabase Client: {e}")
        return

    # 2. Cek Akses Tabel di Database
    print(f"\n[*] Memeriksa tabel '{table_name}' di database Supabase...")
    table_ready = False
    try:
        res = supabase.table(table_name).select("*").limit(1).execute()
        print(f"[+] [OK] Tabel '{table_name}' DITEMUKAN dan dapat diakses!")
        print(f"    Jumlah data saat ini: {len(res.data)} baris sampel")
        table_ready = True
    except Exception as e:
        err_msg = str(e)
        if "PGRST205" in err_msg or "schema cache" in err_msg or "Could not find the table" in err_msg:
            print(f"[-] [PERHATIAN] Tabel '{table_name}' BELUM DIBUAT di database Supabase.")
            print(f"    --> Silakan buka Supabase Dashboard -> SQL Editor.")
            print(f"    --> Jalankan file: create_table_hasil_ss.sql")
        else:
            print(f"[-] [ERROR] Akses ke tabel gagal: {e}")

    # 3. Cek Storage Bucket
    print(f"\n[*] Memeriksa storage bucket '{bucket_name}'...")
    bucket_ready = False
    try:
        buckets = supabase.storage.list_buckets()
        found_names = [b.name for b in buckets] if buckets else []
        if bucket_name in found_names:
            print(f"[+] [OK] Bucket Storage '{bucket_name}' DITEMUKAN!")
            bucket_ready = True
        else:
            print(f"[-] [PERHATIAN] Bucket '{bucket_name}' belum ada dalam daftar bucket aktif.")
            print(f"    Bucket yang ditemukan: {found_names or 'Kosong'}")
            print(f"    --> Jalankan file create_table_hasil_ss.sql atau buat bucket di menu Storage.")
    except Exception as e:
        print(f"[-] [INFO] Pengecekan daftar bucket: {e}")

    # Kesimpulan
    print("\n" + "=" * 65)
    print("   RINGKASAN STATUS")
    print("=" * 65)
    if table_ready and bucket_ready:
        print("[STATUS]: SIAP DIGUNAKAN! Semua tabel & bucket sudah aktif.")
    else:
        print("[STATUS]: PERLU SETUP SQL DI SUPABASE DASHBOARD:")
        print("  1. Buka dashboard: https://supabase.com/dashboard/project/wqjfunxfngowxwdmyzyj")
        print("  2. Masuk ke menu 'SQL Editor' di panel kiri.")
        print("  3. Buka atau copy isi file 'create_table_hasil_ss.sql', lalu klik 'Run'.")
        print("  4. Jalankan script ini kembali untuk konfirmasi: python setup_check.py")
    print("=" * 65)

if __name__ == "__main__":
    main()
