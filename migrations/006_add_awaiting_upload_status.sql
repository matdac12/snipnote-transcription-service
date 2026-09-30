-- Migration 006: allow transcription_jobs.status = 'awaiting_upload'
-- Part 1 of 2 for background upload. Run 007 AFTER this one has COMMITTED.
--
-- !! TRANSACTION LIMITS OF `ALTER TYPE ... ADD VALUE` !!
--   * PostgreSQL < 12: cannot run inside a transaction block at all.
--   * PostgreSQL >= 12 (all current Supabase projects): may run inside a transaction,
--     but the NEW VALUE CANNOT BE USED until that transaction has committed (a later
--     statement in the same transaction that references 'awaiting_upload' fails with
--     "unsafe use of new value"). That is why this file only adds the value and the
--     partial indexes that mention it live in 007, which must be a separate run.
--   * Do not paste 006 and 007 into one Supabase SQL-editor run / one `supabase db push`
--     transaction; run 006, wait for it to finish, then run 007.
--   * Enum values cannot be removed later. Rolling back = stop creating such jobs and
--     leave the unused value in place.
--
-- The repo does not contain the base CREATE TABLE, so this handles the two ways a
-- `status` column is normally defined:
--   (a) a Postgres enum type  -> ALTER TYPE ... ADD VALUE IF NOT EXISTS
--   (b) text + CHECK constraint -> constraint is rebuilt with the extra value
--   (c) plain text, no constraint -> nothing to do
-- Safe to re-run.

DO $$
DECLARE
  col_type text;
  col_schema text;
  con record;
  new_def text;
BEGIN
  SELECT t.typname, n.nspname INTO col_type, col_schema
  FROM pg_attribute a
  JOIN pg_type t ON t.oid = a.atttypid
  JOIN pg_namespace n ON n.oid = t.typnamespace
  WHERE a.attrelid = 'public.transcription_jobs'::regclass
    AND a.attname = 'status' AND NOT a.attisdropped;

  IF col_type IS NULL THEN
    RAISE EXCEPTION 'transcription_jobs.status not found';
  END IF;

  IF EXISTS (SELECT 1 FROM pg_type WHERE typname = col_type AND typnamespace = (SELECT oid FROM pg_namespace WHERE nspname = col_schema) AND typtype = 'e') THEN
    EXECUTE format('ALTER TYPE %I.%I ADD VALUE IF NOT EXISTS %L', col_schema, col_type, 'awaiting_upload');
    RAISE NOTICE 'Added awaiting_upload to enum %.%', col_schema, col_type;
    RETURN;
  END IF;

  -- text column: look for a CHECK constraint that lists the statuses
  FOR con IN
    SELECT conname, pg_get_constraintdef(oid) AS def
    FROM pg_constraint
    WHERE conrelid = 'public.transcription_jobs'::regclass AND contype = 'c'
      AND pg_get_constraintdef(oid) ILIKE '%status%' AND pg_get_constraintdef(oid) ILIKE '%pending%'
  LOOP
    IF con.def LIKE '%awaiting_upload%' THEN
      RAISE NOTICE 'Constraint % already allows awaiting_upload', con.conname;
      CONTINUE;
    END IF;
    new_def := replace(con.def, '''pending''', '''awaiting_upload'', ''pending''');
    IF new_def = con.def THEN
      RAISE EXCEPTION 'Could not rewrite constraint % (%): extend it by hand to allow awaiting_upload', con.conname, con.def;
    END IF;
    EXECUTE format('ALTER TABLE public.transcription_jobs DROP CONSTRAINT %I', con.conname);
    EXECUTE format('ALTER TABLE public.transcription_jobs ADD CONSTRAINT %I %s', con.conname, new_def);
    RAISE NOTICE 'Rebuilt constraint %', con.conname;
  END LOOP;
END
$$;
