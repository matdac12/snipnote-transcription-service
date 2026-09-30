# SnipNote Transcription Service

Server-side audio transcription using the OpenAI `gpt-transcribe` API.

Deployed on the `omni` VPS at `https://api.snipnote.app` — see [DEPLOYMENT.md](DEPLOYMENT.md).

## Local Development

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Set environment variable:
   ```bash
   export OPENAI_API_KEY=sk-...
   ```

3. Run server:
   ```bash
   python main.py
   ```

4. Test endpoint (there is no synchronous endpoint any more; see `POST /jobs` below):
   ```bash
   curl http://localhost:8000/
   ```

## Deployment to Render

### Option 1: Using render.yaml (Recommended)

The `render.yaml` file automatically configures both the web service and cron worker:

1. Push this repository to GitHub
2. In Render Dashboard, click "New" → "Blueprint"
3. Connect your GitHub repository
4. Render will detect `render.yaml` and create:
   - **Web Service**: API endpoints
   - **Cron Job**: Background worker (runs every 2 minutes)
5. Add environment variables to both services:
   - `OPENAI_API_KEY` - Your OpenAI API key
   - `SUPABASE_URL` - Your Supabase project URL
   - `SUPABASE_SERVICE_KEY` - Your Supabase service role key
   - `API_KEY` - (Optional) API key for endpoint authentication

### Option 2: Manual Setup

**Web Service:**
1. Create new Web Service on Render
2. Connect to this GitHub repository
3. Configure:
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn main:app --host 0.0.0.0 --port $PORT`
4. Add environment variables (see above)

**Cron Job:**
1. Create new Cron Job on Render
2. Connect to same GitHub repository
3. Configure:
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python worker.py`
   - **Schedule**: `*/2 * * * *` (every 2 minutes)
4. Add environment variables (see above)

## API Endpoints

### Health Check
- `GET /` - Health check
  - Returns: `{"status": "healthy", "service": "snipnote-transcription"}`

### Async Job Endpoints (NEW)
- `POST /jobs` - Create transcription job
  - Headers: `X-API-Key: <your-api-key>` (optional if API_KEY not set)
  - Body: `{"user_id": "...", "meeting_id": "...", "audio_url": "..."}`
  - Returns: `{"job_id": "...", "status": "pending", "created_at": "..."}`

- `GET /jobs/{job_id}` - Get job status
  - Headers: `X-API-Key: <your-api-key>` (optional if API_KEY not set)
  - Returns: Full job details including transcript if completed

### Background upload mode
- `POST /jobs` with `{"upload_pending": true, "meeting_id": "...", "expected_bytes": N, ...}` and
  `Authorization: Bearer <Supabase user JWT>` creates a job in the new status
  `awaiting_upload` and returns a Supabase Storage signed upload URL (`upload_url`).
  The app PUTs the original file there from a background URLSession; the worker promotes
  the job to `pending` once the object exists with the expected size (and expires it after
  `UPLOAD_PENDING_TTL_SECONDS`). Idempotent per `meeting_id`. Contract for the iOS side:
  [docs/BACKGROUND_UPLOAD_CONTRACT.md](docs/BACKGROUND_UPLOAD_CONTRACT.md); deployment:
  [DEPLOYMENT.md](DEPLOYMENT.md#background-upload-upload_pending). The user comes from the
  verified JWT (`auth.py`), never from the body. Legacy requests (no `upload_pending`) behave as before.

### Auth and limits (legacy endpoints)
`AUTH_MODE=off|log|enforce` (default `log`) controls whether `GET /jobs/{id}` and legacy `POST /jobs` need a Supabase
user token and ownership; old app builds keep working in `log`. Details: [DEPLOYMENT.md](DEPLOYMENT.md#security-hardening-audit-of-2026-09-30)
and [docs/SECURITY_AND_MINUTES_ROLLOUT.md](docs/SECURITY_AND_MINUTES_ROLLOUT.md). Optional server-side minutes debit
(`SERVER_MINUTES_DEBIT_ENABLED`, default off) is described there too.

### Removed: `/transcribe`
`POST /transcribe` (synchronous, unauthenticated, ran the provider inside the API process) now
answers **410 Gone**; the iOS app never called it. Use `POST /jobs`.

## Testing Endpoints

### Test Job Creation
```bash
curl -X POST https://api.snipnote.app/jobs \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-api-key" \
  -d '{
    "user_id": "123e4567-e89b-12d3-a456-426614174000",
    "meeting_id": "123e4567-e89b-12d3-a456-426614174001",
    "audio_url": "https://example.com/audio.m4a"
  }'
```

### Test Job Status
```bash
curl -X GET https://api.snipnote.app/jobs/{job_id} \
  -H "X-API-Key: your-api-key"
```

## Cloud transcription providers

New jobs can choose `transcription_provider: "openai"` or `"xai"`; omitted
selection defaults to OpenAI. Regular and chunked workers retain it for the whole
job, including retries, and never switch providers on failure. Unknown values
return 422. Status responses include the stored provider.

Models resolve from Supabase `ai_model_config` rows `transcription` and
`transcription_xai` (60-second cache). Only transcription changes; summary,
overview and actions keep their current configuration. xAI uses `XAI_API_KEY`
from the VPS environment; Supabase Edge Function secrets are configured
separately. xAI jobs transcribe the whole audio in one request (see
[DEPLOYMENT.md](DEPLOYMENT.md#xai-single-request-transcription)) with automatic
fallback to chunking; OpenAI keeps the chunked pipeline. Never store credentials in model rows or iOS. Follow the additive
migration, credentials, backend, then iOS order in
[DEPLOYMENT.md](DEPLOYMENT.md#saved-transcription-provider-release). Paid staging
smoke tests and deployment remain separate from offline implementation tests.

## Live Activity pushes (optional)

The worker can push transcription stage/progress to an iOS Live Activity / Dynamic Island
through APNs (`apns.py`), including a final "ready"/"failed" alert when the app is closed.
Enabled only when `APNS_KEY_P8`, `APNS_KEY_ID`, `APNS_TEAM_ID` and `APNS_BUNDLE_ID` are set;
otherwise nothing changes. Setup: [DEPLOYMENT.md](DEPLOYMENT.md#live-activity-apns-pushes).
Payload/ContentState contract for the iOS side: [docs/LIVE_ACTIVITY_CONTRACT.md](docs/LIVE_ACTIVITY_CONTRACT.md).
Migrations: `migrations/008_*` (token table, RLS) and `migrations/009_*` (`transcription_jobs.stage`); 006/007 belong to background upload.
