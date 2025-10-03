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

1. Create new Web Service on Render
2. Connect to this GitHub repository
3. Configure:
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn main:app --host 0.0.0.0 --port $PORT`
4. Add Environment Variable:
   - Key: `OPENAI_API_KEY`
   - Value: Your OpenAI API key
5. Deploy

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
