-- Migration: Create audio_chunks table for chunked audio upload support
-- Description: Stores metadata for each audio chunk uploaded to Supabase Storage
-- Author: System
-- Date: 2025-10-04

-- Create audio_chunks table
CREATE TABLE IF NOT EXISTS audio_chunks (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  meeting_id UUID NOT NULL,
  user_id UUID NOT NULL,
  chunk_index INTEGER NOT NULL,
  total_chunks INTEGER NOT NULL,
  file_path TEXT NOT NULL,
  file_size INTEGER NOT NULL,
  duration_seconds NUMERIC NOT NULL,
  uploaded_at TIMESTAMPTZ DEFAULT NOW(),
  transcribed BOOLEAN DEFAULT FALSE,
  transcript TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),

  -- Ensure no duplicate chunks for same meeting
  CONSTRAINT audio_chunks_unique UNIQUE(meeting_id, chunk_index)
);

-- Create indexes for efficient querying
CREATE INDEX IF NOT EXISTS idx_audio_chunks_meeting ON audio_chunks(meeting_id);
CREATE INDEX IF NOT EXISTS idx_audio_chunks_transcribed ON audio_chunks(meeting_id, transcribed);
CREATE INDEX IF NOT EXISTS idx_audio_chunks_user ON audio_chunks(user_id);

-- Add comment for documentation
COMMENT ON TABLE audio_chunks IS 'Stores individual audio chunks for meetings with files >15MB that require chunked upload';
COMMENT ON COLUMN audio_chunks.chunk_index IS 'Zero-based index of this chunk in the sequence';
COMMENT ON COLUMN audio_chunks.total_chunks IS 'Total number of chunks for this meeting';
COMMENT ON COLUMN audio_chunks.file_path IS 'Supabase Storage path: userId/meetingId_chunk_N.m4a';
COMMENT ON COLUMN audio_chunks.duration_seconds IS 'Duration of this chunk in seconds (for time-based splitting)';
