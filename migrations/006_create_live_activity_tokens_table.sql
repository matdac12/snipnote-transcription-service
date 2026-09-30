-- Migration: live_activity_tokens
-- Purpose: per-Live-Activity APNs push tokens uploaded by the iOS app so the worker can
--          push transcription progress / completion to the lock screen and Dynamic Island
--          after the app is suspended. See docs/LIVE_ACTIVITY_CONTRACT.md.
-- Apply BEFORE deploying the worker that sends pushes (the worker tolerates a missing
-- table: it logs and pauses lookups, jobs are unaffected).

CREATE TABLE IF NOT EXISTS live_activity_tokens (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id      UUID NOT NULL REFERENCES transcription_jobs(id) ON DELETE CASCADE,
  user_id     UUID NOT NULL,
  token       TEXT NOT NULL,
  environment TEXT NOT NULL CHECK (environment IN ('sandbox', 'production')),
  bundle_id   TEXT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  -- The token is placed in an APNs URL path by the worker: hex only, sane length.
  CONSTRAINT live_activity_tokens_token_hex CHECK (token ~ '^[0-9a-fA-F]{32,512}$'),
  CONSTRAINT live_activity_tokens_job_token_key UNIQUE (job_id, token)
);

CREATE INDEX IF NOT EXISTS idx_live_activity_tokens_user ON live_activity_tokens(user_id);

COMMENT ON TABLE live_activity_tokens IS 'ActivityKit push tokens (one per Live Activity) used by the worker to send APNs liveactivity pushes for a transcription job';
COMMENT ON COLUMN live_activity_tokens.token IS 'Hex-encoded Activity.pushToken';
COMMENT ON COLUMN live_activity_tokens.environment IS 'sandbox (debug/TestFlight-dev builds) or production (App Store/TestFlight); selects the APNs host';
COMMENT ON COLUMN live_activity_tokens.bundle_id IS 'Optional; overrides APNS_BUNDLE_ID for the apns-topic when set';

CREATE OR REPLACE FUNCTION live_activity_tokens_touch_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_live_activity_tokens_updated_at ON live_activity_tokens;
CREATE TRIGGER trg_live_activity_tokens_updated_at
  BEFORE UPDATE ON live_activity_tokens
  FOR EACH ROW EXECUTE FUNCTION live_activity_tokens_touch_updated_at();

-- RLS: the service role (the worker) bypasses it. Authenticated users manage only their
-- own rows, and may only attach a token to a job they own (otherwise a user could
-- subscribe to somebody else's job progress by guessing its id).
ALTER TABLE live_activity_tokens ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Users can select own live activity tokens" ON live_activity_tokens;
CREATE POLICY "Users can select own live activity tokens" ON live_activity_tokens
  FOR SELECT TO authenticated
  USING (user_id = auth.uid());

DROP POLICY IF EXISTS "Users can insert own live activity tokens" ON live_activity_tokens;
CREATE POLICY "Users can insert own live activity tokens" ON live_activity_tokens
  FOR INSERT TO authenticated
  WITH CHECK (
    user_id = auth.uid()
    AND EXISTS (
      SELECT 1 FROM transcription_jobs j
      WHERE j.id = live_activity_tokens.job_id AND j.user_id::text = auth.uid()::text
    )
  );

DROP POLICY IF EXISTS "Users can update own live activity tokens" ON live_activity_tokens;
CREATE POLICY "Users can update own live activity tokens" ON live_activity_tokens
  FOR UPDATE TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (
    user_id = auth.uid()
    AND EXISTS (
      SELECT 1 FROM transcription_jobs j
      WHERE j.id = live_activity_tokens.job_id AND j.user_id::text = auth.uid()::text
    )
  );
