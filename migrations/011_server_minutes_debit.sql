-- Migration 011: server-side minutes debit for worker-processed jobs (audit B)
--
-- ============================ READ THIS BEFORE APPLYING ============================
-- The real minutes-ledger SQL (balance table, `debit_minutes`, `credit_minutes`,
-- `get_user_minutes_balance`, `grant_free_tier_minutes`) is NOT in any repo. Everything
-- below is built on what the iOS app (MinutesManager.swift) proves about their SIGNATURES:
--
--   get_user_minutes_balance()                                    -> integer   (auth.uid()-based)
--   debit_minutes(p_amount integer, p_meeting_id <text|uuid>)     -> integer   new balance (auth.uid()-based)
--   credit_minutes(p_amount, p_reason, p_apple_transaction_id, p_metadata)
--
-- and on the client's duplicate handling (a second debit for the same p_meeting_id fails
-- with an error containing "duplicate"/"already"/23505, which the app treats as success).
-- It does NOT prove: table/column names, whether debit_minutes is SECURITY DEFINER, whether
-- it lets the balance go negative (docs disagree: "never below 0" vs "allow negative"),
-- or that it really enforces uniqueness per meeting.
--
-- Strategy that minimises guessing: DO NOT touch the ledger tables. Impersonate the user
-- (transaction-local auth.uid()) and call the REAL functions, so the real ledger, its
-- idempotency and any analytics hooks stay the single source of truth. Our own table
-- (server_minutes_debits) only adds per-job/per-meeting idempotency for the SERVER side.
--
-- TODO(owner): adapt to real schema. Verify, in the Supabase SQL editor, BEFORE setting
-- SERVER_MINUTES_DEBIT_ENABLED=true on the VPS:
--   1. \df+ public.debit_minutes public.get_user_minutes_balance   (argument names/types, security)
--   2. begin; select set_config('request.jwt.claim.sub','<a test user uuid>',true);
--             select public.get_user_minutes_balance(); rollback;   (auth.uid() impersonation works)
--   3. select public.debit_minutes_for_job('<test user>','<new uuid>','<new uuid>',1) twice:
--      first 'debited', second 'already_debited'; then debit the same meeting through the
--      app-style call and confirm it is reported as already debited (real ledger dedupes by meeting).
--   4. Decide the shortfall policy below (default: clamp at zero, see debit_minutes_for_job).
-- This migration is ADDITIVE and inert until the worker flag is turned on. It aborts (nothing
-- is created) if the two functions it depends on do not exist.
-- Independent of 010 (RLS), but apply 010 too: it stops users from editing the columns added here.
-- ===================================================================================

DO $$
BEGIN
  IF to_regprocedure('public.get_user_minutes_balance()') IS NULL THEN
    RAISE EXCEPTION 'TODO(owner): adapt to real schema - public.get_user_minutes_balance() not found (see header of migration 011)';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                 WHERE n.nspname = 'public' AND p.proname = 'debit_minutes') THEN
    RAISE EXCEPTION 'TODO(owner): adapt to real schema - public.debit_minutes(...) not found (see header of migration 011)';
  END IF;
END $$;

-- ---- columns on the job (written by the worker / the function below; NULL = not billed) ----
ALTER TABLE public.transcription_jobs
  ADD COLUMN IF NOT EXISTS billable_seconds double precision,
  ADD COLUMN IF NOT EXISTS minutes_debited  integer,
  ADD COLUMN IF NOT EXISTS debited_at       timestamptz,
  ADD COLUMN IF NOT EXISTS debit_status     text;

COMMENT ON COLUMN public.transcription_jobs.billable_seconds IS 'Audio seconds the worker measured (ffprobe) and will bill; set together with the results. NULL on jobs the client saved itself (on-device flow) and on jobs finished before server billing existed: never swept.';
COMMENT ON COLUMN public.transcription_jobs.minutes_debited  IS 'Minutes actually taken from the balance by the server (0 when the client already debited this meeting or the balance was 0).';
COMMENT ON COLUMN public.transcription_jobs.debited_at       IS 'NULL + billable_seconds set = the server still owes a debit; the worker sweep retries it.';
COMMENT ON COLUMN public.transcription_jobs.debit_status     IS 'debited | partial | zero_balance | already_debited | already_debited_by_client | skipped_zero';

-- The sweep: completed, billable, not yet debited.
CREATE INDEX IF NOT EXISTS idx_transcription_jobs_undebited
  ON public.transcription_jobs (completed_at)
  WHERE status = 'completed' AND billable_seconds IS NOT NULL AND debited_at IS NULL;

-- ---- server-side debit record: exactly one row per (user, meeting) and per job ----
CREATE TABLE IF NOT EXISTS public.server_minutes_debits (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id           uuid NOT NULL,
  meeting_id        uuid NOT NULL,
  job_id            uuid NOT NULL,
  provider          text,
  billed_seconds    double precision,
  minutes_requested integer NOT NULL CHECK (minutes_requested >= 0),
  minutes_debited   integer NOT NULL CHECK (minutes_debited >= 0),
  shortfall_minutes integer NOT NULL DEFAULT 0 CHECK (shortfall_minutes >= 0),
  status            text NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_server_minutes_debits_meeting UNIQUE (user_id, meeting_id),
  CONSTRAINT uq_server_minutes_debits_job UNIQUE (job_id)
);
-- Deliberately no FK to auth.users / transcription_jobs: accounting rows outlive both.

COMMENT ON TABLE public.server_minutes_debits IS 'One row per server-side minutes debit (worker). Idempotency guard and cost/analytics record (provider, billed seconds, shortfall). Service role only.';

ALTER TABLE public.server_minutes_debits ENABLE ROW LEVEL SECURITY;   -- no policies: only the service role (bypasses RLS) can touch it
REVOKE ALL ON public.server_minutes_debits FROM PUBLIC, anon, authenticated;

-- ---- balance lookup for the API's create-time check (POST /jobs -> 402) ----
CREATE OR REPLACE FUNCTION public.get_minutes_balance_for_user(p_user_id uuid)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_balance integer;
  v_old_sub text := current_setting('request.jwt.claim.sub', true);
  v_old_claims text := current_setting('request.jwt.claims', true);
BEGIN
  -- Transaction-local impersonation so the auth.uid()-based function answers for this user.
  PERFORM set_config('request.jwt.claim.sub', p_user_id::text, true);
  PERFORM set_config('request.jwt.claims', json_build_object('sub', p_user_id::text, 'role', 'authenticated')::text, true);
  -- TODO(owner): adapt to real schema (name/return type of the existing balance function)
  EXECUTE 'SELECT public.get_user_minutes_balance()' INTO v_balance;
  PERFORM set_config('request.jwt.claim.sub', COALESCE(v_old_sub, ''), true);
  PERFORM set_config('request.jwt.claims', COALESCE(v_old_claims, ''), true);
  RETURN v_balance;
END;
$$;

-- ---- the debit: exactly once per (user, meeting) and per job ----
-- Returns jsonb {status, minutes_debited, minutes_requested, shortfall_minutes, balance_after}.
--   debited                   full amount taken
--   partial                   balance was lower than requested: clamp-at-zero policy (see below)
--   zero_balance              nothing to take
--   already_debited           this job/meeting was already debited by the server (repeat call)
--   already_debited_by_client the REAL ledger already holds a debit for this meeting
--                             (on-device fallback / client retry queue): nothing more is taken
--   skipped_zero              p_minutes <= 0
-- Policy (TODO(owner): confirm): CLAMP AT ZERO. The worker has already paid the provider and
-- finished the job; refusing or going negative would either lose the result or create debt.
-- The shortfall is recorded in server_minutes_debits.shortfall_minutes for follow-up. The
-- create-time check (402) is what actually prevents overdrafts.
CREATE OR REPLACE FUNCTION public.debit_minutes_for_job(
  p_user_id    uuid,
  p_meeting_id uuid,
  p_job_id     uuid,
  p_minutes    integer,
  p_seconds    double precision DEFAULT NULL,
  p_provider   text DEFAULT NULL
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_row         public.server_minutes_debits%ROWTYPE;
  v_balance     integer;
  v_amount      integer;
  v_status      text;
  v_new_balance integer;
  v_old_sub     text := current_setting('request.jwt.claim.sub', true);
  v_old_claims  text := current_setting('request.jwt.claims', true);
  v_result      jsonb;
BEGIN
  IF p_minutes IS NULL OR p_minutes <= 0 THEN
    UPDATE public.transcription_jobs SET minutes_debited = 0, debited_at = now(), debit_status = 'skipped_zero'
     WHERE id = p_job_id AND debited_at IS NULL;
    RETURN jsonb_build_object('status', 'skipped_zero', 'minutes_debited', 0, 'minutes_requested', 0, 'shortfall_minutes', 0);
  END IF;

  -- Serialise concurrent calls for the same (user, meeting): worker retry vs sweep.
  PERFORM pg_advisory_xact_lock(hashtextextended(p_user_id::text || ':' || p_meeting_id::text, 0));

  SELECT * INTO v_row FROM public.server_minutes_debits WHERE user_id = p_user_id AND meeting_id = p_meeting_id;
  IF NOT FOUND THEN
    SELECT * INTO v_row FROM public.server_minutes_debits WHERE job_id = p_job_id;
  END IF;
  IF FOUND THEN
    UPDATE public.transcription_jobs
       SET minutes_debited = v_row.minutes_debited, debited_at = COALESCE(debited_at, v_row.created_at), debit_status = COALESCE(debit_status, v_row.status)
     WHERE id = p_job_id;
    RETURN jsonb_build_object('status', 'already_debited', 'minutes_debited', v_row.minutes_debited,
                              'minutes_requested', v_row.minutes_requested, 'shortfall_minutes', v_row.shortfall_minutes);
  END IF;

  -- Act as the user for the existing auth.uid()-based ledger functions (transaction-local).
  PERFORM set_config('request.jwt.claim.sub', p_user_id::text, true);
  PERFORM set_config('request.jwt.claims', json_build_object('sub', p_user_id::text, 'role', 'authenticated')::text, true);

  -- TODO(owner): adapt to real schema (balance function name / return type)
  EXECUTE 'SELECT public.get_user_minutes_balance()' INTO v_balance;
  v_amount := LEAST(p_minutes, GREATEST(COALESCE(v_balance, 0), 0));   -- clamp at zero

  IF v_amount > 0 THEN
    BEGIN
      -- format(%L) yields an untyped literal, so it resolves whether p_meeting_id is text or uuid.
      -- TODO(owner): adapt to real schema (parameter names p_amount / p_meeting_id per MinutesManager.swift)
      EXECUTE format('SELECT public.debit_minutes(p_amount := %L, p_meeting_id := %L)', v_amount, p_meeting_id::text)
        INTO v_new_balance;
      v_status := CASE WHEN v_amount < p_minutes THEN 'partial' ELSE 'debited' END;
    EXCEPTION
      WHEN unique_violation THEN
        v_amount := 0; v_status := 'already_debited_by_client'; v_new_balance := v_balance;
      WHEN OTHERS THEN
        -- The app treats messages containing duplicate/already as "already debited". Anything
        -- else (insufficient balance, permissions, schema drift) must surface and be retried.
        IF SQLERRM ~* '(duplicate|already)' THEN
          v_amount := 0; v_status := 'already_debited_by_client'; v_new_balance := v_balance;
        ELSE
          RAISE;
        END IF;
    END;
  ELSE
    v_status := 'zero_balance'; v_new_balance := v_balance;
  END IF;

  PERFORM set_config('request.jwt.claim.sub', COALESCE(v_old_sub, ''), true);
  PERFORM set_config('request.jwt.claims', COALESCE(v_old_claims, ''), true);

  INSERT INTO public.server_minutes_debits
    (user_id, meeting_id, job_id, provider, billed_seconds, minutes_requested, minutes_debited, shortfall_minutes, status)
  VALUES
    (p_user_id, p_meeting_id, p_job_id, p_provider, p_seconds, p_minutes, v_amount,
     CASE WHEN v_status IN ('partial', 'zero_balance') THEN p_minutes - v_amount ELSE 0 END, v_status);

  UPDATE public.transcription_jobs
     SET minutes_debited = v_amount, debited_at = now(), debit_status = v_status
   WHERE id = p_job_id;

  v_result := jsonb_build_object('status', v_status, 'minutes_debited', v_amount, 'minutes_requested', p_minutes,
                                 'shortfall_minutes', CASE WHEN v_status IN ('partial', 'zero_balance') THEN p_minutes - v_amount ELSE 0 END,
                                 'balance_after', v_new_balance);
  RETURN v_result;
END;
$$;

-- Service role ONLY: these accept an arbitrary user id.
REVOKE ALL ON FUNCTION public.get_minutes_balance_for_user(uuid) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.debit_minutes_for_job(uuid, uuid, uuid, integer, double precision, text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.get_minutes_balance_for_user(uuid) TO service_role;
GRANT EXECUTE ON FUNCTION public.debit_minutes_for_job(uuid, uuid, uuid, integer, double precision, text) TO service_role;
