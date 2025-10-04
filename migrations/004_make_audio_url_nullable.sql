-- Migration: Make audio_url nullable for chunked jobs
-- Description: Chunked jobs don't have a single audio_url since audio is split into chunks
-- Author: System
-- Date: 2025-10-04

-- Make audio_url nullable for chunked jobs
ALTER TABLE transcription_jobs
  ALTER COLUMN audio_url DROP NOT NULL;

-- Update comment to reflect that audio_url is optional for chunked jobs
COMMENT ON COLUMN transcription_jobs.audio_url IS 'Supabase Storage URL or public URL to audio file (optional for chunked jobs)';
