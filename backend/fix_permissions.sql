-- JALANKAN DUA BARIS INI DI SQL EDITOR SUPABASE:
-- (Untuk membuka izin tabel hasil_ss ke publishable / anon key)

GRANT ALL ON TABLE public.hasil_ss TO anon, authenticated, service_role;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO anon, authenticated, service_role;

-- Pastikan policy RLS juga aktif:
ALTER TABLE public.hasil_ss ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Allow public insert into hasil_ss" ON public.hasil_ss;
CREATE POLICY "Allow public insert into hasil_ss"
ON public.hasil_ss FOR INSERT TO anon, authenticated WITH CHECK (true);

DROP POLICY IF EXISTS "Allow public read from hasil_ss" ON public.hasil_ss;
CREATE POLICY "Allow public read from hasil_ss"
ON public.hasil_ss FOR SELECT TO anon, authenticated USING (true);
