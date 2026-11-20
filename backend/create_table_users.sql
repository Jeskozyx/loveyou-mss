-- ============================================================
-- Fix Permissions & Table Setup: user_responses
-- Jalankan query ini di Supabase Dashboard > SQL Editor:
-- https://supabase.com/dashboard/project/wqjfunxfngowxwdmyzyj/sql/new
-- ============================================================

-- 1. Berikan hak akses tabel ke role anon, authenticated, service_role
GRANT ALL ON TABLE public.user_responses TO anon, authenticated, service_role;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO anon, authenticated, service_role;

-- 2. Pastikan RLS aktif dan izinkan anon untuk INSERT & SELECT
ALTER TABLE public.user_responses ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "allow_anon_insert" ON public.user_responses;
CREATE POLICY "allow_anon_insert" ON public.user_responses
  FOR INSERT TO anon, authenticated WITH CHECK (true);

DROP POLICY IF EXISTS "allow_anon_select" ON public.user_responses;
CREATE POLICY "allow_anon_select" ON public.user_responses
  FOR SELECT TO anon, authenticated USING (true);
