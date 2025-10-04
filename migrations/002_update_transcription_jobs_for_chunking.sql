-- Migration: Add chunked upload support to transcription_jobs table
-- Description: Adds columns to track chunked transcription progress
-- Author: System
-- Date: 2025-10-04

-- Add chunking support columns to transcription_jobs table
ALTER TABLE transcription_jobs
  ADD COLUMN IF NOT EXISTS is_chunked BOOLEAN DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS total_chunks INTEGER DEFAULT 1,
  ADD COLUMN IF NOT EXISTS chunks_processed INTEGER DEFAULT 0;

-- Create index for efficient chunked job queries
CREATE INDEX IF NOT EXISTS idx_transcription_jobs_chunked ON transcription_jobs(is_chunked, status)
  WHERE is_chunked = TRUE;

-- Add comments for documentation
COMMENT ON COLUMN transcription_jobs.is_chunked IS 'TRUE if this job uses chunked upload (file >15MB)';
COMMENT ON COLUMN transcription_jobs.total_chunks IS 'Total number of audio chunks for this job';
COMMENT ON COLUMN transcription_jobs.chunks_processed IS 'Number of chunks successfully transcribed (for progress tracking)';
