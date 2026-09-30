-- Migration 012: row level security for audio_chunks (audit A4; OPTIONAL but recommended)
--
-- Migration 001 created audio_chunks WITHOUT enabling RLS. If that is still true in production, anybody holding the
-- public anon key can read every user's chunk rows (including per-chunk transcripts), insert rows pointing the worker
-- at arbitrary storage paths, or delete rows. (The worker now also validates paths and filters by user_id, see
-- audio_access.py; this is the database-side half.) UNVERIFIABLE FROM THE REPOS whether it was enabled by hand.
--
-- The iOS app's only use (SupabaseManager.uploadAudioChunk): INSERT a row for itself
--   (meeting_id, user_id, chunk_index, total_chunks, file_path = "<lowercase user uuid>/<meeting>_chunk_<n>.m4a",
--    file_size, duration_seconds, [uploaded_at, transcribed, transcript, created_at]).
-- It never reads, updates or deletes chunk rows. The worker (service role) bypasses RLS.
--
-- Rules for `authenticated`: SELECT own rows; INSERT own rows whose file_path lies inside their own storage folder.
-- No UPDATE/DELETE (the app does not need them). `anon`: nothing.
-- !! Applying this breaks chunk uploads if the app ever stores a file_path outside "<user uuid>/". Idempotent.
-- Rollback: ALTER TABLE public.audio_chunks DISABLE ROW LEVEL SECURITY;

ALTER TABLE public.audio_chunks ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Users can read their own audio chunks" ON public.audio_chunks;
CREATE POLICY "Users can read their own audio chunks"
    ON public.audio_chunks
    FOR SELECT TO authenticated
    USING (auth.uid() = user_id);

DROP POLICY IF EXISTS "Users can insert their own audio chunks" ON public.audio_chunks;
CREATE POLICY "Users can insert their own audio chunks"
    ON public.audio_chunks
    FOR INSERT TO authenticated
    WITH CHECK (auth.uid() = user_id AND file_path LIKE auth.uid()::text || '/%' AND file_path NOT LIKE '%..%');

REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON public.audio_chunks FROM authenticated;
REVOKE ALL ON public.audio_chunks FROM anon;
