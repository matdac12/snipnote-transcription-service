-- Migration: Add language column to transcription_jobs
-- Purpose: Allow clients to specify the audio language for transcription
-- When NULL, the model auto-detects the language (backwards compatible)

ALTER TABLE transcription_jobs
ADD COLUMN language VARCHAR(10) NULL;

COMMENT ON COLUMN transcription_jobs.language IS 'ISO-639-1 language code (e.g., en, it, es). NULL means auto-detect.';
