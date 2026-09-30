-- Migration: machine-readable stage on transcription_jobs
-- Purpose: stable stage identifiers for the iOS Live Activity / polling clients.
-- `current_stage` stays free text ("Transcribing chunk 2/4...") for old clients.
-- Additive and nullable: NULL means "not reported yet" (treat as queued while status is
-- pending; old rows stay NULL). The worker tolerates the column being absent.

ALTER TABLE transcription_jobs
  ADD COLUMN IF NOT EXISTS stage TEXT NULL;

COMMENT ON COLUMN transcription_jobs.stage IS 'Machine-readable stage: queued, preparing, transcribing, summarizing, done, failed. NULL = not reported. current_stage remains human-readable free text.';
