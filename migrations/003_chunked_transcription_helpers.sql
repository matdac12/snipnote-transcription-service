-- Migration: Helper functions for chunked transcription
-- Description: Utility functions to manage and query chunked audio transcriptions
-- Author: System
-- Date: 2025-10-04

-- Function: Get all chunks for a meeting ordered by chunk_index
CREATE OR REPLACE FUNCTION get_meeting_chunks(p_meeting_id UUID)
RETURNS TABLE (
  id UUID,
  chunk_index INTEGER,
  total_chunks INTEGER,
  file_path TEXT,
  file_size INTEGER,
  duration_seconds NUMERIC,
  transcribed BOOLEAN,
  transcript TEXT,
  uploaded_at TIMESTAMPTZ
) AS $$
BEGIN
  RETURN QUERY
  SELECT
    ac.id,
    ac.chunk_index,
    ac.total_chunks,
    ac.file_path,
    ac.file_size,
    ac.duration_seconds,
    ac.transcribed,
    ac.transcript,
    ac.uploaded_at
  FROM audio_chunks ac
  WHERE ac.meeting_id = p_meeting_id
  ORDER BY ac.chunk_index ASC;
END;
$$ LANGUAGE plpgsql;

-- Function: Check if all chunks are uploaded for a meeting
CREATE OR REPLACE FUNCTION are_all_chunks_uploaded(p_meeting_id UUID)
RETURNS BOOLEAN AS $$
DECLARE
  v_total_chunks INTEGER;
  v_uploaded_chunks INTEGER;
BEGIN
  -- Get expected total chunks from any chunk record
  SELECT total_chunks INTO v_total_chunks
  FROM audio_chunks
  WHERE meeting_id = p_meeting_id
  LIMIT 1;

  -- Count actually uploaded chunks
  SELECT COUNT(*) INTO v_uploaded_chunks
  FROM audio_chunks
  WHERE meeting_id = p_meeting_id;

  -- Return TRUE if all chunks are uploaded
  RETURN v_total_chunks IS NOT NULL AND v_uploaded_chunks = v_total_chunks;
END;
$$ LANGUAGE plpgsql;

-- Function: Check if all chunks are transcribed for a meeting
CREATE OR REPLACE FUNCTION are_all_chunks_transcribed(p_meeting_id UUID)
RETURNS BOOLEAN AS $$
DECLARE
  v_total_chunks INTEGER;
  v_transcribed_chunks INTEGER;
BEGIN
  -- Get expected total chunks
  SELECT total_chunks INTO v_total_chunks
  FROM audio_chunks
  WHERE meeting_id = p_meeting_id
  LIMIT 1;

  -- Count transcribed chunks
  SELECT COUNT(*) INTO v_transcribed_chunks
  FROM audio_chunks
  WHERE meeting_id = p_meeting_id AND transcribed = TRUE;

  -- Return TRUE if all chunks are transcribed
  RETURN v_total_chunks IS NOT NULL AND v_transcribed_chunks = v_total_chunks;
END;
$$ LANGUAGE plpgsql;

-- Function: Get merged transcript for a meeting
CREATE OR REPLACE FUNCTION get_merged_transcript(p_meeting_id UUID)
RETURNS TEXT AS $$
DECLARE
  v_merged_transcript TEXT;
BEGIN
  -- Concatenate all transcripts in order with newlines
  SELECT string_agg(transcript, E'\n' ORDER BY chunk_index)
  INTO v_merged_transcript
  FROM audio_chunks
  WHERE meeting_id = p_meeting_id AND transcript IS NOT NULL;

  RETURN v_merged_transcript;
END;
$$ LANGUAGE plpgsql;

-- Function: Calculate transcription progress percentage
CREATE OR REPLACE FUNCTION get_transcription_progress(p_meeting_id UUID)
RETURNS INTEGER AS $$
DECLARE
  v_total_chunks INTEGER;
  v_transcribed_chunks INTEGER;
  v_progress INTEGER;
BEGIN
  -- Get total chunks
  SELECT total_chunks INTO v_total_chunks
  FROM audio_chunks
  WHERE meeting_id = p_meeting_id
  LIMIT 1;

  -- If no chunks, return 0
  IF v_total_chunks IS NULL THEN
    RETURN 0;
  END IF;

  -- Count transcribed chunks
  SELECT COUNT(*) INTO v_transcribed_chunks
  FROM audio_chunks
  WHERE meeting_id = p_meeting_id AND transcribed = TRUE;

  -- Calculate percentage
  v_progress := ROUND((v_transcribed_chunks::NUMERIC / v_total_chunks::NUMERIC) * 100);

  RETURN v_progress;
END;
$$ LANGUAGE plpgsql;

-- Function: Get total duration of all chunks for a meeting
CREATE OR REPLACE FUNCTION get_total_chunk_duration(p_meeting_id UUID)
RETURNS NUMERIC AS $$
DECLARE
  v_total_duration NUMERIC;
BEGIN
  SELECT SUM(duration_seconds) INTO v_total_duration
  FROM audio_chunks
  WHERE meeting_id = p_meeting_id;

  RETURN COALESCE(v_total_duration, 0);
END;
$$ LANGUAGE plpgsql;

-- Function: Get total file size of all chunks for a meeting
CREATE OR REPLACE FUNCTION get_total_chunk_size(p_meeting_id UUID)
RETURNS BIGINT AS $$
DECLARE
  v_total_size BIGINT;
BEGIN
  SELECT SUM(file_size) INTO v_total_size
  FROM audio_chunks
  WHERE meeting_id = p_meeting_id;

  RETURN COALESCE(v_total_size, 0);
END;
$$ LANGUAGE plpgsql;

-- Add comments for documentation
COMMENT ON FUNCTION get_meeting_chunks IS 'Retrieves all chunks for a meeting ordered by chunk_index';
COMMENT ON FUNCTION are_all_chunks_uploaded IS 'Returns TRUE if all expected chunks are uploaded';
COMMENT ON FUNCTION are_all_chunks_transcribed IS 'Returns TRUE if all chunks are transcribed';
COMMENT ON FUNCTION get_merged_transcript IS 'Returns concatenated transcript from all chunks in order';
COMMENT ON FUNCTION get_transcription_progress IS 'Returns transcription progress as percentage (0-100)';
COMMENT ON FUNCTION get_total_chunk_duration IS 'Returns total duration of all chunks in seconds';
COMMENT ON FUNCTION get_total_chunk_size IS 'Returns total file size of all chunks in bytes';
