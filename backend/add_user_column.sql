-- ============================================================
-- SQL: Tambahkan kolom user_name ke tabel hasil_ss
-- Jalankan query ini di Supabase Dashboard > SQL Editor:
-- https://supabase.com/dashboard/project/wqjfunxfngowxwdmyzyj/sql/new
-- ============================================================

-- 1. Tambahkan kolom user_name jika belum ada
ALTER TABLE public.hasil_ss 
  ADD COLUMN IF NOT EXISTS user_name TEXT DEFAULT 'anonymous';

-- 2. Buat index agar query filter per-user sangat cepat
CREATE INDEX IF NOT EXISTS idx_hasil_ss_user_name 
  ON public.hasil_ss(user_name);

-- 3. Berikan izin akses ke role anon & authenticated
GRANT ALL ON TABLE public.hasil_ss TO anon, authenticated, service_role;

-- 4. Komentar kolom
COMMENT ON COLUMN public.hasil_ss.user_name IS 'Nama user yang sedang aktif saat screenshot diambil';
