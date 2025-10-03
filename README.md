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

- `GET /` - Health check
- `POST /transcribe` - Upload audio file for transcription
  - Accepts: multipart/form-data with `file` field
  - Returns: `{"transcript": "...", "duration": 123.5}`
