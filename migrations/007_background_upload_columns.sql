-- Migration 007: columns and indexes for background upload
-- Part 2 of 2. Requires migration 006 to have COMMITTED first (the partial index
-- below compares status with 'awaiting_upload'; see the ALTER TYPE notes in 006).
-- Additive and idempotent; old app builds and the legacy API paths are unaffected.

ALTER TABLE transcription_jobs
  ADD COLUMN IF NOT EXISTS expected_bytes   bigint,
  ADD COLUMN IF NOT EXISTS storage_path     text,
  ADD COLUMN IF NOT EXISTS content_type     text,
  ADD COLUMN IF NOT EXISTS upload_deadline  timestamptz;

COMMENT ON COLUMN transcription_jobs.expected_bytes  IS 'Exact size of the single audio file the client will upload (background upload mode). The worker promotes the job only when the stored object has exactly this size.';
COMMENT ON COLUMN transcription_jobs.storage_path    IS 'Object path in the recordings bucket (<user_id>/<meeting_id>.<ext>). Non-NULL marks a background-upload job; the worker then reads the audio from storage instead of audio_url.';
COMMENT ON COLUMN transcription_jobs.content_type    IS 'Content-Type the client must send with the raw PUT upload.';
COMMENT ON COLUMN transcription_jobs.upload_deadline IS 'After this time an awaiting_upload job is failed with upload_expired by the worker. Extended each time a fresh signed URL is issued.';

-- Idempotency per meeting for background-upload jobs: at most ONE non-failed job per
-- (user_id, meeting_id) that has a storage_path. Deliberately partial: a plain unique
-- index on (user_id, meeting_id) would break the legacy flows, which may create several
-- jobs for one meeting (retry / re-transcribe). A failed job (e.g. upload_expired)
-- does not block creating a new one.
CREATE UNIQUE INDEX IF NOT EXISTS uq_transcription_jobs_upload_meeting
  ON transcription_jobs (user_id, meeting_id)
  WHERE storage_path IS NOT NULL AND status <> 'failed';

-- Promotion / expiry sweep: the worker scans awaiting_upload jobs oldest first.
CREATE INDEX IF NOT EXISTS idx_transcription_jobs_awaiting_upload
  ON transcription_jobs (created_at)
  WHERE status = 'awaiting_upload';
