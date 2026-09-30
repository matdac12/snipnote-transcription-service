-- Copy of the iOS repo migrations that define public.transcription_jobs and its RLS policies
-- (SnipNote/supabase/migrations/20251003..20260930105323 at commit 78f04d8), used as the test baseline.
-- The ai_model_config seed INSERT of the provider migration is omitted (not relevant here).

-- ===== 20251003_create_transcription_jobs.sql =====
-- Create enum for job status
CREATE TYPE transcription_job_status AS ENUM ('pending', 'processing', 'completed', 'failed');

-- Create transcription_jobs table
CREATE TABLE IF NOT EXISTS public.transcription_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    meeting_id UUID NOT NULL,
    audio_url TEXT NOT NULL,
    status transcription_job_status NOT NULL DEFAULT 'pending',
    transcript TEXT,
    duration FLOAT,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

-- Create indexes for efficient querying
CREATE INDEX idx_transcription_jobs_user_id ON public.transcription_jobs(user_id);
CREATE INDEX idx_transcription_jobs_status ON public.transcription_jobs(status);
CREATE INDEX idx_transcription_jobs_created_at ON public.transcription_jobs(created_at DESC);
CREATE INDEX idx_transcription_jobs_meeting_id ON public.transcription_jobs(meeting_id);

-- Create updated_at trigger function
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Create trigger to automatically update updated_at
CREATE TRIGGER update_transcription_jobs_updated_at
    BEFORE UPDATE ON public.transcription_jobs
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- Enable Row Level Security
ALTER TABLE public.transcription_jobs ENABLE ROW LEVEL SECURITY;

-- RLS Policy: Users can only insert jobs for themselves
CREATE POLICY "Users can create their own transcription jobs"
    ON public.transcription_jobs
    FOR INSERT
    WITH CHECK (auth.uid() = user_id);

-- RLS Policy: Users can only read their own jobs
CREATE POLICY "Users can view their own transcription jobs"
    ON public.transcription_jobs
    FOR SELECT
    USING (auth.uid() = user_id);

-- RLS Policy: Users can update their own jobs (for retry scenarios)
CREATE POLICY "Users can update their own transcription jobs"
    ON public.transcription_jobs
    FOR UPDATE
    USING (auth.uid() = user_id);

-- RLS Policy: Service role can do anything (for worker processing)
CREATE POLICY "Service role has full access to transcription jobs"
    ON public.transcription_jobs
    FOR ALL
    USING (auth.role() = 'service_role');

-- Add comment to table
COMMENT ON TABLE public.transcription_jobs IS 'Tracks server-side audio transcription jobs for SnipNote meetings';
COMMENT ON COLUMN public.transcription_jobs.status IS 'Job status: pending (queued), processing (in progress), completed (success), failed (error)';
COMMENT ON COLUMN public.transcription_jobs.audio_url IS 'Supabase Storage URL or public URL to audio file';
COMMENT ON COLUMN public.transcription_jobs.duration IS 'Audio duration in seconds';


-- ===== 20251004_add_ai_fields_to_transcription_jobs.sql =====
-- Add AI-generated content fields to transcription_jobs table
ALTER TABLE public.transcription_jobs
    ADD COLUMN overview TEXT,
    ADD COLUMN summary TEXT,
    ADD COLUMN actions JSONB;

-- Add comments for new columns
COMMENT ON COLUMN public.transcription_jobs.overview IS 'AI-generated 1-sentence meeting overview (short summary)';
COMMENT ON COLUMN public.transcription_jobs.summary IS 'AI-generated full meeting summary';
COMMENT ON COLUMN public.transcription_jobs.actions IS 'AI-extracted action items as JSON array';


-- ===== 20251004_add_progress_tracking.sql =====
-- Add progress tracking columns to transcription_jobs table
-- This enables real-time progress updates during long transcription jobs

ALTER TABLE public.transcription_jobs
ADD COLUMN progress_percentage INTEGER DEFAULT 0,
ADD COLUMN current_stage TEXT;

-- Add comments for documentation
COMMENT ON COLUMN transcription_jobs.progress_percentage IS 'Progress from 0-100 representing job completion percentage';
COMMENT ON COLUMN transcription_jobs.current_stage IS 'Human-readable stage description (e.g., "Transcribing chunk 2/5...")';

-- Add index for efficient progress queries
CREATE INDEX idx_transcription_jobs_progress ON transcription_jobs(progress_percentage) WHERE status = 'processing';


-- ===== 20260306_make_transcription_job_audio_url_nullable.sql =====
-- Local-model transcription completes on device and does not require a stored audio asset.
ALTER TABLE public.transcription_jobs
ALTER COLUMN audio_url DROP NOT NULL;

COMMENT ON COLUMN public.transcription_jobs.audio_url IS
'Supabase Storage URL or public URL to audio file when a transcription job depends on remote audio; null for local-only jobs.';


-- ===== 20260930105323_add_transcription_provider.sql =====
-- Additive: legacy jobs remain OpenAI; existing policies and grants are unchanged.
ALTER TABLE public.transcription_jobs
  ADD COLUMN IF NOT EXISTS transcription_provider text NOT NULL DEFAULT 'openai';

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT FROM pg_constraint
    WHERE conrelid = 'public.transcription_jobs'::regclass
      AND conname = 'transcription_jobs_transcription_provider_check'
  ) THEN
    ALTER TABLE public.transcription_jobs
      ADD CONSTRAINT transcription_jobs_transcription_provider_check
      CHECK (transcription_provider IN ('openai', 'xai'));
  END IF;
END $$;


