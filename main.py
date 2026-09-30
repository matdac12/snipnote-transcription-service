from fastapi import FastAPI, HTTPException, Header, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import AliasChoices, BaseModel, Field, model_validator
from typing import Literal
import uvicorn
import os
import warnings
from supabase_client import create_job, get_job
import auth
import background_upload
import audio_access

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
    # Legacy mode requires user_id in the body. upload_pending mode IGNORES it for
    # authorization: the user is taken from the verified Supabase JWT.
    user_id: str | None = None
    meeting_id: str
    audio_url: str | None = None  # Optional for chunked jobs
    is_chunked: bool = False
    total_chunks: int = 1
    duration: float | None = None
    # `provider` is accepted as an alias (used by the upload_pending contract)
    transcription_provider: Literal["openai", "xai"] = Field(
        "openai", validation_alias=AliasChoices("transcription_provider", "provider"))
    language: str | None = None  # ISO-639-1 code (e.g., "en", "it"). None for auto-detect
    # --- background upload mode ---
    upload_pending: bool = False
    expected_bytes: int | None = None
    file_extension: str | None = None  # e.g. "m4a" (default), "mp3", "wav"
    content_type: str | None = None    # default derived from the extension

    @model_validator(mode="after")
    def _legacy_requires_user_id(self):
        if not self.upload_pending and not self.user_id:
            raise ValueError("user_id is required")
        return self


class CreateJobResponse(BaseModel):
    job_id: str
    status: str
    created_at: str
    # Only present (non-null) in upload_pending mode; legacy responses are unchanged
    # because the endpoint uses response_model_exclude_none.
    upload_url: str | None = None
    upload_method: str | None = None
    upload_headers: dict[str, str] | None = None
    expires_at: str | None = None
    upload_deadline: str | None = None
    storage_path: str | None = None
    expected_bytes: int | None = None


class JobStatusResponse(BaseModel):
    transcription_provider: Literal["openai", "xai"] = "openai"
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
    language: str | None = None       # ISO-639-1 code used for transcription
    error_message: str | None = None
    progress_percentage: int = 0      # Progress from 0-100
    current_stage: str | None = None  # Human-readable stage description
    created_at: str
    updated_at: str
    completed_at: str | None = None
    # Background upload jobs (null otherwise)
    expected_bytes: int | None = None
    upload_deadline: str | None = None

# CORS: the iOS app is not a browser and needs none. Default = no CORS headers at all.
# Set CORS_ALLOWED_ORIGINS to a comma-separated list of exact origins if a web client is added.
def cors_allowed_origins() -> list[str]:
    return [o.strip() for o in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",") if o.strip() and o.strip() != "*"]


_cors_origins = cors_allowed_origins()
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-API-Key"],
    )

@app.exception_handler(auth.AuthError)
async def auth_error_handler(request: Request, exc: auth.AuthError):
    headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=headers)


@app.get("/")
async def health_check():
    return {"status": "healthy", "service": "snipnote-transcription"}


@app.post("/jobs", response_model=CreateJobResponse, response_model_exclude_none=True)
async def create_transcription_job(
    request: CreateJobRequest,
    authenticated: bool = Depends(verify_api_key),
    authorization: str | None = Header(None),
):
    """
    Create a new transcription job (regular or chunked)

    The job will be queued with status='pending' and processed by the background worker.

    For chunked jobs:
    - Set is_chunked=true
    - Provide total_chunks and duration
    - Audio chunks should be pre-uploaded to audio_chunks table
    - Worker will fetch chunks from database using meeting_id

    Background upload (upload_pending=true): requires `Authorization: Bearer <Supabase
    user JWT>`; see docs/BACKGROUND_UPLOAD_CONTRACT.md. Creates an `awaiting_upload` job
    and returns a signed upload URL; the worker starts the job once the file lands.
    """
    if request.upload_pending:
        return await create_upload_pending_job(request, authorization)
    try:
        # Create job in Supabase
        job = create_job(
            user_id=request.user_id,
            meeting_id=request.meeting_id,
            audio_url=request.audio_url,
            is_chunked=request.is_chunked,
            total_chunks=request.total_chunks,
            duration=request.duration,
            language=request.language,
            transcription_provider=request.transcription_provider
        )

        return CreateJobResponse(
            job_id=job["id"],
            status=job["status"],
            created_at=job["created_at"]
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create job: {str(e)}")


async def identify_caller(authorization: str | None, endpoint: str):
    """AUTH_MODE-aware identity for the legacy endpoints (see auth.py).

    Returns the verified user, or None when the request is anonymous and the mode allows it.
    Raises 401/503 in `enforce` mode. Always counts the outcome (no PII) for the rollout."""
    mode = auth.auth_mode()
    if mode == "off":
        return None
    result = await run_in_threadpool(auth.resolve_user, authorization)
    auth.note_request(endpoint, "authenticated" if result.user else result.problem)
    if result.user:
        return result.user
    if mode == "enforce":
        if result.problem == "unavailable":
            raise HTTPException(status_code=503, detail="Authentication service unavailable")
        raise HTTPException(status_code=401, detail="Missing or invalid bearer token",
                            headers={"WWW-Authenticate": "Bearer"})
    return None


async def create_upload_pending_job(request: CreateJobRequest, authorization: str | None):
    # JWT verification may fetch a JWKS and every step below is blocking I/O: keep it
    # off the event loop.
    user = await run_in_threadpool(auth.authenticate_header, authorization)
    if request.user_id and request.user_id.lower() != user.user_id:
        raise HTTPException(status_code=403, detail="user_id does not match the authenticated user")
    try:
        result = await run_in_threadpool(
            lambda: background_upload.create_upload_job(
                user_id=user.user_id,  # from the verified token, never from the body
                meeting_id=request.meeting_id,
                expected_bytes=request.expected_bytes,
                file_extension=request.file_extension,
                content_type=request.content_type,
                duration=request.duration,
                language=request.language,
                provider=request.transcription_provider,
            )
        )
    except background_upload.UploadRequestError as error:
        raise HTTPException(status_code=error.status_code, detail=error.detail)
    except Exception as error:
        # Generic body: error text could contain storage/DB internals.
        print(f"❌ upload_pending job creation failed: {type(error).__name__}")
        raise HTTPException(status_code=500, detail="Failed to create upload job")
    return CreateJobResponse(**result)


@app.get("/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job_status(
    job_id: str,
    authenticated: bool = Depends(verify_api_key),
    authorization: str | None = Header(None),
):
    """
    Get the status of a transcription job

    Returns job details including status, transcript (if completed), and timestamps.

    Ownership (AUTH_MODE, see auth.py): `enforce` = 401 without a valid token and 404 unless the
    job belongs to the token's user. `log` (default) = anonymous requests are still served (old app
    builds send no token) but `audio_url` is withheld; a valid token of ANOTHER user always gets 404.
    `off` = no token handling, `audio_url` is still withheld. `user_id` is always returned (the
    iOS decoder requires it).
    """
    if not audio_access.is_uuid(job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    user = await identify_caller(authorization, "GET /jobs")
    try:
        job = await run_in_threadpool(get_job, job_id)
    except Exception as e:
        print(f"❌ Failed to retrieve job {job_id}: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve job")

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    is_owner = bool(user) and str(job.get("user_id", "")).lower() == user.user_id
    if user and not is_owner:
        raise HTTPException(status_code=404, detail="Job not found")  # never reveal that it exists
    response = JobStatusResponse(**job)
    if not is_owner:
        response.audio_url = None  # optional in the iOS model; a storage URL is a capability
    return response


@app.api_route("/transcribe", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
async def transcribe_removed():
    """Removed: the synchronous endpoint was unauthenticated and ran OpenAI/xAI inside the API
    process (credit burn, event-loop DoS). The iOS app never called it; use POST /jobs.

    Declares no body parameters on purpose: FastAPI then never reads the (up to nginx's
    `client_max_body_size`) upload, it just answers 410."""
    return JSONResponse(status_code=410, content={"detail": "POST /transcribe has been removed. Use POST /jobs."})

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
