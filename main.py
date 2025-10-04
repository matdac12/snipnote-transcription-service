from fastapi import FastAPI, File, UploadFile, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn
import os
import warnings
from transcribe import transcribe_audio
from supabase_client import create_job, get_job

# Suppress pydub regex warnings in Python 3.13+
warnings.filterwarnings("ignore", category=SyntaxWarning, module="pydub")

app = FastAPI(title="SnipNote Transcription Service")

# Simple API key authentication for MVP
# Full JWT/Supabase auth will be added in Phase 4
API_KEY = os.getenv("API_KEY", "")


async def verify_api_key(x_api_key: str = Header(None)):
    """
    Simple API key validation for MVP

    In production (Phase 4), this will be replaced with Supabase JWT validation
    """
    if not API_KEY:
        # If no API key is set, allow all requests (for local testing)
        return True

    if not x_api_key or x_api_key != API_KEY:
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API key. Include X-API-Key header."
        )
    return True


# Request/Response Models
class CreateJobRequest(BaseModel):
    user_id: str
    meeting_id: str
    audio_url: str | None = None  # Optional for chunked jobs
    is_chunked: bool = False
    total_chunks: int = 1
    duration: float | None = None


class CreateJobResponse(BaseModel):
    job_id: str
    status: str
    created_at: str


class JobStatusResponse(BaseModel):
    id: str
    user_id: str
    meeting_id: str
    audio_url: str | None = None      # Optional for chunked jobs
    status: str
    transcript: str | None = None
    overview: str | None = None      # AI-generated 1-sentence overview
    summary: str | None = None        # AI-generated full summary
    actions: list | None = None       # AI-extracted action items
    duration: float | None = None
    error_message: str | None = None
    progress_percentage: int = 0      # Progress from 0-100
    current_stage: str | None = None  # Human-readable stage description
    created_at: str
    updated_at: str
    completed_at: str | None = None

# Allow all origins for testing (will restrict later)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def health_check():
    return {"status": "healthy", "service": "snipnote-transcription"}


@app.post("/jobs", response_model=CreateJobResponse)
async def create_transcription_job(
    request: CreateJobRequest,
    authenticated: bool = Depends(verify_api_key)
):
    """
    Create a new transcription job (regular or chunked)

    The job will be queued with status='pending' and processed by the background worker.

    For chunked jobs:
    - Set is_chunked=true
    - Provide total_chunks and duration
    - Audio chunks should be pre-uploaded to audio_chunks table
    - Worker will fetch chunks from database using meeting_id
    """
    try:
        # Create job in Supabase
        job = create_job(
            user_id=request.user_id,
            meeting_id=request.meeting_id,
            audio_url=request.audio_url,
            is_chunked=request.is_chunked,
            total_chunks=request.total_chunks,
            duration=request.duration
        )

        return CreateJobResponse(
            job_id=job["id"],
            status=job["status"],
            created_at=job["created_at"]
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create job: {str(e)}")


@app.get("/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job_status(
    job_id: str,
    authenticated: bool = Depends(verify_api_key)
):
    """
    Get the status of a transcription job

    Returns job details including status, transcript (if completed), and timestamps.
    """
    try:
        job = get_job(job_id)

        if not job:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

        return JobStatusResponse(**job)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to retrieve job: {str(e)}")


@app.post("/transcribe")
async def transcribe(file: UploadFile = File(...)):
    try:
        # Read audio file
        audio_data = await file.read()

        # Transcribe
        result = transcribe_audio(audio_data, file.filename)

        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
