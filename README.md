# SnipNote Transcription Service

Server-side audio transcription using OpenAI Whisper API.

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

4. Test endpoint:
   ```bash
   curl -X POST http://localhost:8000/transcribe \
     -F "file=@test-audio.m4a"
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

### Legacy Sync Endpoint
- `POST /transcribe` - Upload audio file for synchronous transcription
  - Accepts: multipart/form-data with `file` field
  - Returns: `{"transcript": "...", "duration": 123.5}`

## Testing Endpoints

### Test Job Creation
```bash
curl -X POST https://your-render-url.onrender.com/jobs \
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
curl -X GET https://your-render-url.onrender.com/jobs/{job_id} \
  -H "X-API-Key: your-api-key"
```
