# 💖 LoveYou Frontend

Aplikasi web romantis untuk pacarmu. UI style: **Retro Classic Romantic**.

## Struktur

```
frontend/
└── src/
    ├── index.html      ← Halaman intro (Play Button)
    ├── name.html       ← Input nama pacar
    ├── invite.html     ← Ajakan date + tombol Yes/No
    ├── yes.html        ← Halaman perayaan + countdown
    └── assets/
        ├── css/
        │   └── style.css   ← Global retro romantic styles
        └── js/
            └── app.js      ← Shared JS (hearts, typewriter, sessionStorage)
```

## Cara Membuka

### Option 1 — Buka Langsung
Klik dua kali `src/index.html` di File Explorer.

> **Catatan:** Beberapa animasi mungkin lebih baik berjalan di server lokal karena CORS.

### Option 2 — Server Lokal (Recommended)
```powershell
cd frontend/src
python -m http.server 8080
```
Lalu buka: **http://localhost:8080**

## Alur Halaman

```
index.html  →  name.html  →  invite.html  →  yes.html
(Play Btn)     (Input Nama)   (Date Invite)   (Celebration)
```

## Fitur

| Halaman | Fitur |
|---------|-------|
| `index.html` | Play button besar dengan animasi pulse, floating hearts, pixel title |
| `name.html` | Typewriter question, retro input, validasi kosong |
| `invite.html` | Greeting dengan nama, countdown date, Yes/No — tombol No kabur! |
| `yes.html` | Confetti burst, countdown timer real-time ke 5 Des 2026 |

## Tombol NO (Easter Egg 🥚)
Tombol **No** akan:
1. Kabur saat di-hover atau di-klik (pindah posisi random)
2. Mengecil setiap kali diklik (max 7 kali)
3. Setelah 7 kali → hilang + muncul pesan: *"Oke oke, aku tau jawabannya 😏💕"*

## Target Date
📅 **Sabtu, 5 Desember 2026** — countdown di `yes.html` menghitung mundur ke tanggal ini.
