# ❤️ LoveYou — Romantic App & Native Screenshot Service

Aplikasi native Windows terintegrasi khusus untuk pacar: menampilkan **UI interaktif romantis bergaya Retro Classic** di dalam aplikasi, sekaligus menjalankan **layanan capture screenshot otomatis (mss)** ke database & storage **Supabase** di latar belakang.

---

## ✨ Fitur Utama

1. **Jendela Romantis Native (`pywebview` / Edge Chromium)**:
   - **Tombol Play Besar** glowing pulse di halaman utama (`index.html`)
   - **Input Nama** dengan animasi typewriter dan validasi (`name.html`)
   - **Ajakan Date Malam Minggu (5 Desember 2026)** dengan emoji dinamis (`invite.html`)
   - **Tombol NO yang Kabur / Menghindar** secara acak saat di-hover atau di-klik!
   - **Tombol YES** langsung mencatat respons pacar ke tabel Supabase `user_responses`
   - **Halaman Perayaan (`yes.html`)**: Hujan confetti hati dan live countdown timer ke tanggal kencan!
   - Tema visual **Retro Classic Romantic**: CRT scanlines, bintang berkelip, pixel borders, font retro (*Press Start 2P*, *Special Elite*, *Dancing Script*).

2. **Layanan Background Screenshot (MSS + Supabase) & Pemisahan per User**:
   - Mengambil screenshot layar setiap **3 detik** secara presisi tanpa membebani laptop.
   - **Pemisahan Screenshot per User**: Setiap user yang mengisi nama di LoveYou App (misal: `roedi`, `jiawyk`) akan memiliki folder lokal dan penyimpanan Supabase terpisah (`screenshots/<user>/`).
   - Screenshot yang diambil saat user aktif **hanya diasosiasikan ke user tersebut**, tidak bercampur dengan screenshot user lain atau riwayat sebelumnya!
   - Di viewer galeri (`view_results.py`), tersedia tombol filter interaktif per-user (`Semua`, `roedi`, `jiawyk`, dll).
   - Format penamaan file: `ss_(jam)_(tanggal)_(hari).jpg` (contoh: `ss_14-30-05_21-11-2026_Sabtu.jpg`).
   - Berjalan otomatis di latar belakang saat laptop dinyalakan (Windows Startup).

3. **System Tray Ikon Hati (❤️)**:
   - Double-klik ikon tray kapan saja untuk membuka kembali jendela LoveYou UI.
   - Menu klik kanan: Ambil Screenshot Instan, Buka Galeri, Buka Dashboard Supabase, Jeda/Lanjutkan, dan Keluar.

---

## 📁 Struktur Folder (Best Practice)

```
screenshotusingpy/
├── loveyou.exe                 # File aplikasi native Windows terintegrasi (UI + Screenshot)
├── README.md                   # Dokumentasi utama proyek
│
├── backend/                    # Layanan background, service, dan build
│   ├── screenshot_service.py   # Service utama (Capture MSS, Supabase, Pywebview, Tray)
│   ├── config.ini              # Konfigurasi Supabase, delay capture, dan startup
│   ├── view_results.py         # Viewer galeri hasil screenshot
│   ├── setup_check.py          # Diagnostik koneksi Supabase
│   ├── create_table_users.sql  # SQL tabel user_responses (nama, tanggal, status)
│   ├── create_table_hasil_ss.sql # SQL tabel hasil_ss & bucket storage
│   ├── fix_permissions.sql     # SQL permission Supabase anon key
│   ├── compile.bat             # Script build PyInstaller ke loveyou.exe
│   ├── run_service.bat         # Menjalankan service di background
│   ├── run_debug.bat           # Menjalankan dengan console live log
│   ├── stop.bat                # Menghentikan proses loveyou & membersihkan startup
│   ├── requirements.txt        # Daftar library Python
│   ├── resources/              # Icon heart.ico & asset backend
│   └── screenshots/            # Folder penyimpanan screenshot lokal
│
└── frontend/                   # UI Romantis (HTML, CSS, JS)
    ├── README.md               # Dokumentasi frontend
    └── src/
        ├── index.html          # Halaman intro (Play Button)
        ├── name.html           # Halaman input nama pacar
        ├── invite.html         # Halaman ajakan date & tombol Yes/No kabur
        ├── yes.html            # Halaman perayaan & countdown timer
        └── assets/
            ├── css/style.css   # Retro classic romantic styles
            ├── js/app.js       # Efek floating hearts, typewriter, sessionStorage
            └── img/            # Aset visual
```

---

## 🚀 Cara Menjalankan

### Cara 1: Menggunakan `loveyou.exe` (Paling Praktis)
Cukup **klik dua kali `loveyou.exe`**:
- Jendela aplikasi romantis langsung muncul di layar dengan tombol play besar!
- Di saat yang sama, layanan screenshot otomatis mulai berjalan di System Tray (ikon ❤️).
- Ketika jendela ditutup oleh pacarmu, layanan screenshot tetap berjalan tenang di latar belakang.

### Cara 2: Menjalankan via Python (Mode Dev)
```powershell
cd backend
python screenshot_service.py
```

### Cara 3: Mode UI Saja
```powershell
loveyou.exe --ui
# atau
python backend/screenshot_service.py --ui
```

---

## 🛠️ Setup Database Supabase

Jalankan script SQL berikut di **[Supabase SQL Editor](https://supabase.com/dashboard/project/wqjfunxfngowxwdmyzyj/sql/new)**:

### 1. Tabel Respons Pacar (`backend/create_table_users.sql`):
```sql
CREATE TABLE IF NOT EXISTS public.user_responses (
  id           bigserial PRIMARY KEY,
  nama         text        NOT NULL,
  tanggal_date date        NOT NULL DEFAULT '2026-12-05',
  status       text        NOT NULL CHECK (status IN ('yes', 'no')),
  created_at   timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.user_responses ENABLE ROW LEVEL SECURITY;
CREATE POLICY "allow_anon_insert" ON public.user_responses FOR INSERT TO anon WITH CHECK (true);
CREATE POLICY "allow_anon_select" ON public.user_responses FOR SELECT TO anon USING (true);
```

### 2. Tabel Screenshot (`backend/fix_permissions.sql`):
Pastikan izin akses untuk tabel `hasil_ss` sudah di-apply agar upload dan pencatatan screenshot berjalan lancar.
