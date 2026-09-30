-- Migration 010: stop signed-in users from creating or re-queuing worker jobs through PostgREST (audit A4)
--
-- Problem: the original policies (SnipNote/supabase/migrations/20251003_create_transcription_jobs.sql) let any
-- authenticated user INSERT and UPDATE their own transcription_jobs rows with NO column or status limits, and the
-- worker polls the TABLE (status = 'pending'). So a signed-in user could, with the public anon key + their JWT,
-- insert a 'pending' row with an arbitrary audio_url / provider / chunk count (SSRF, free transcription, cost) or
-- flip an old row back to 'pending', bypassing every check in the API. Checking a JWT in POST /jobs is not enough.
--
-- What the iOS app legitimately writes (SupabaseManager.saveCompletedTranscriptionJob, the ONLY client write,
-- verified by grep of SnipNote/*.swift; there is no client DELETE): for on-device transcriptions it INSERTs or UPDATEs
-- its own row with status = 'completed' and the result columns
--   user_id, meeting_id, audio_url, status, transcript, duration, error_message, completed_at,
--   overview, summary, actions, progress_percentage, current_stage.
-- Everything else (queueing, retries, upload columns, stage, billing columns, provider, chunk counters) belongs to the
-- API / worker, which use the service role and bypass RLS.
--
-- New rules for role `authenticated`:
--   INSERT  only rows with user_id = auth.uid() AND status = 'completed', and only those columns.
--   UPDATE  only own rows, result in status = 'completed', and only those columns (so a row can never be
--           set back to pending/processing/awaiting_upload, and worker-owned columns cannot be touched).
--           Rows may START in any status: the on-device fallback legitimately completes a job whose server run
--           is still pending/processing or has failed.
--   SELECT  unchanged (own rows).   DELETE/TRUNCATE: revoked (no client delete exists).   anon: nothing.
--
-- !! REQUIRES AN iOS CHECK BEFORE APPLYING !!  Any future client write outside the list above will fail with
-- "permission denied for column ...". In particular the app's INSERT/UPDATE payload must stay exactly the set above.
-- UNVERIFIABLE FROM THE REPOS: the live policies/grants. The DO block at the end WARNs about any other permissive
-- INSERT/UPDATE/ALL policy on the table (it would OR with these and defeat them); check its output and the
-- verification queries in DEPLOYMENT.md. Idempotent; apply in the Supabase SQL editor (single run is fine).
-- Rollback: re-create the two original policies from 20251003 and `GRANT INSERT, UPDATE ON transcription_jobs TO authenticated`.

-- 1. Policies (names of the originals are dropped so they cannot linger).
DROP POLICY IF EXISTS "Users can create their own transcription jobs" ON public.transcription_jobs;
DROP POLICY IF EXISTS "Users can update their own transcription jobs" ON public.transcription_jobs;
DROP POLICY IF EXISTS "Users can insert completed transcription results" ON public.transcription_jobs;
DROP POLICY IF EXISTS "Users can update own rows to completed results" ON public.transcription_jobs;

CREATE POLICY "Users can insert completed transcription results"
    ON public.transcription_jobs
    FOR INSERT TO authenticated
    WITH CHECK (auth.uid() = user_id AND status = 'completed');

CREATE POLICY "Users can update own rows to completed results"
    ON public.transcription_jobs
    FOR UPDATE TO authenticated
    USING (auth.uid() = user_id)
    WITH CHECK (auth.uid() = user_id AND status = 'completed');

-- 2. Column privileges (RLS cannot restrict columns; grants can). Table-level INSERT/UPDATE are what Supabase's
--    default privileges hand to `authenticated`; replace them with column lists.
REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON public.transcription_jobs FROM authenticated;
REVOKE ALL ON public.transcription_jobs FROM anon;

GRANT INSERT (user_id, meeting_id, audio_url, status, transcript, duration, error_message, completed_at,
              overview, summary, actions, progress_percentage, current_stage)
  ON public.transcription_jobs TO authenticated;
GRANT UPDATE (user_id, meeting_id, audio_url, status, transcript, duration, error_message, completed_at,
              overview, summary, actions, progress_percentage, current_stage)
  ON public.transcription_jobs TO authenticated;
-- (SELECT is untouched: GRANT SELECT stays as it was; the SELECT policy restricts rows to auth.uid().)

-- 3. Flag anything that would silently re-open the hole.
DO $$
DECLARE p record;
BEGIN
  FOR p IN
    SELECT policyname, cmd, roles::text AS roles, permissive
    FROM pg_policies
    WHERE schemaname = 'public' AND tablename = 'transcription_jobs'
      AND cmd IN ('INSERT', 'UPDATE', 'ALL')
      AND policyname NOT IN ('Users can insert completed transcription results',
                             'Users can update own rows to completed results',
                             'Service role has full access to transcription jobs')
  LOOP
    RAISE WARNING 'transcription_jobs has another % policy "%" (roles %, permissive=%): review it, it may defeat migration 010', p.cmd, p.policyname, p.roles, p.permissive;
  END LOOP;
  IF NOT (SELECT relrowsecurity FROM pg_class WHERE oid = 'public.transcription_jobs'::regclass) THEN
    RAISE EXCEPTION 'RLS is not enabled on public.transcription_jobs: refusing to continue';
  END IF;
END $$;
