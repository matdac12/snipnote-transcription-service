import os
from supabase import create_client, Client
from typing import Optional, Dict, Any
from datetime import datetime

# Initialize Supabase client
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    raise ValueError(
        "Missing required environment variables: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set"
    )

# Create Supabase client with service role key (bypasses RLS)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

print(f"✅ Supabase client initialized for: {SUPABASE_URL}")


def create_job(
    user_id: str,
    meeting_id: str,
    audio_url: str | None = None,
    is_chunked: bool = False,
    total_chunks: int = 1,
    duration: float | None = None,
    language: str | None = None,
    transcription_provider: str = "openai"
) -> Dict[str, Any]:
    """
    Create a new transcription job with status='pending'

    Args:
        user_id: UUID of the user creating the job
        meeting_id: UUID of the meeting to transcribe
        audio_url: URL to the audio file (optional for chunked jobs)
        is_chunked: Whether this is a chunked upload job
        total_chunks: Total number of audio chunks (for chunked jobs)
        duration: Total audio duration in seconds (for chunked jobs)
        language: ISO-639-1 language code (e.g., "en", "it"). None for auto-detect

    Returns:
        Dict containing the created job data including job_id

    Raises:
        Exception: If job creation fails
    """
    from transcription_provider import validate_transcription_provider
    validate_transcription_provider(transcription_provider)
    try:
        data = {
            "user_id": user_id,
            "meeting_id": meeting_id,
            "status": "pending",
            "is_chunked": is_chunked,
            "total_chunks": total_chunks,
            "chunks_processed": 0,
            "transcription_provider": transcription_provider
        }

        # Only add audio_url if provided (not required for chunked jobs)
        if audio_url:
            data["audio_url"] = audio_url

        # Add duration if provided (for chunked jobs)
        if duration:
            data["duration"] = duration

        # Add language if provided (for explicit language specification)
        if language:
            data["language"] = language

        response = supabase.table("transcription_jobs").insert(data).execute()

        if response.data and len(response.data) > 0:
            job = response.data[0]
            job_type = "chunked" if is_chunked else "regular"
            print(f"✅ Created {job_type} job {job['id']} for user {user_id} (chunks: {total_chunks})")
            return job
        else:
            raise Exception("Failed to create job: No data returned")

    except Exception as e:
        print(f"❌ Error creating job: {e}")
        raise


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    """
    Retrieve a transcription job by ID

    Args:
        job_id: UUID of the job to retrieve

    Returns:
        Dict containing job data or None if not found

    Raises:
        Exception: If query fails
    """
    try:
        response = supabase.table("transcription_jobs").select("*").eq("id", job_id).execute()

        if response.data and len(response.data) > 0:
            return response.data[0]
        else:
            print(f"⚠️ Job {job_id} not found")
            return None

    except Exception as e:
        print(f"❌ Error retrieving job {job_id}: {e}")
        raise


def update_job_status(
    job_id: str,
    status: str,
    transcript: Optional[str] = None,
    duration: Optional[float] = None,
    error: Optional[str] = None
) -> Dict[str, Any]:
    """
    Update job status and related fields

    Args:
        job_id: UUID of the job to update
        status: New status ('pending', 'processing', 'completed', 'failed')
        transcript: Transcription text (for completed jobs)
        duration: Audio duration in seconds (for completed jobs)
        error: Error message (for failed jobs)

    Returns:
        Dict containing updated job data

    Raises:
        Exception: If update fails
    """
    try:
        update_data: Dict[str, Any] = {"status": status}

        if transcript is not None:
            update_data["transcript"] = transcript

        if duration is not None:
            update_data["duration"] = duration

        if error is not None:
            update_data["error_message"] = error

        # Set completed_at timestamp when job completes
        if status == "completed":
            update_data["completed_at"] = datetime.utcnow().isoformat()

        response = supabase.table("transcription_jobs").update(update_data).eq("id", job_id).execute()

        if response.data and len(response.data) > 0:
            job = response.data[0]
            print(f"✅ Updated job {job_id} to status: {status}")
            return job
        else:
            raise Exception(f"Failed to update job {job_id}: No data returned")

    except Exception as e:
        print(f"❌ Error updating job {job_id}: {e}")
        raise


def update_job_progress(
    job_id: str,
    progress: int,
    stage: str
) -> Dict[str, Any]:
    """
    Update job progress and current stage

    Args:
        job_id: UUID of the job to update
        progress: Progress percentage (0-100)
        stage: Human-readable stage description

    Returns:
        Dict containing updated job data

    Raises:
        Exception: If update fails
    """
    try:
        update_data = {
            "progress_percentage": progress,
            "current_stage": stage
        }

        response = supabase.table("transcription_jobs").update(update_data).eq("id", job_id).execute()

        if response.data and len(response.data) > 0:
            return response.data[0]
        else:
            raise Exception(f"Failed to update job {job_id} progress: No data returned")

    except Exception as e:
        print(f"❌ Error updating job {job_id} progress: {e}")
        raise


def update_job_with_results(
    job_id: str,
    transcript: str,
    overview: str,
    summary: str,
    actions: list,
    duration: float
) -> Dict[str, Any]:
    """
    Update job with all AI-generated results

    Args:
        job_id: UUID of the job to update
        transcript: Full meeting transcript
        overview: 1-sentence overview
        summary: Comprehensive meeting summary
        actions: List of action items
        duration: Audio duration in seconds

    Returns:
        Dict containing updated job data

    Raises:
        Exception: If update fails
    """
    try:
        update_data = {
            "status": "completed",
            "transcript": transcript,
            "overview": overview,
            "summary": summary,
            "actions": actions,  # Supabase client handles JSONB conversion automatically
            "duration": duration,
            "progress_percentage": 100,  # Mark as 100% complete
            "current_stage": "Complete",
            "completed_at": datetime.utcnow().isoformat()
        }

        response = supabase.table("transcription_jobs").update(update_data).eq("id", job_id).execute()

        if response.data and len(response.data) > 0:
            job = response.data[0]
            print(f"✅ Updated job {job_id} with complete AI results")
            return job
        else:
            raise Exception(f"Failed to update job {job_id}: No data returned")

    except Exception as e:
        print(f"❌ Error updating job {job_id} with results: {e}")
        raise


def get_audio_chunks(meeting_id: str, user_id: str) -> list[Dict[str, Any]]:
    """
    Fetch the audio chunks of one user's meeting, ordered by chunk_index

    `user_id` is REQUIRED and always filtered on: the worker uses the service key, and
    meeting ids alone must never be enough to read someone else's chunks (audit A2).
    Callers pass the job's user_id.

    Args:
        meeting_id: UUID of the meeting
        user_id: UUID of the job's owner

    Returns:
        List of audio chunk dictionaries, ordered by chunk_index

    Raises:
        Exception: If query fails
    """
    if not user_id:
        raise ValueError("user_id is required to fetch audio chunks")
    try:
        response = (
            supabase.table("audio_chunks")
            .select("*")
            .eq("meeting_id", meeting_id)
            .eq("user_id", user_id)
            .order("chunk_index")
            .execute()
        )

        if response.data:
            print(f"✅ Found {len(response.data)} audio chunks for meeting {meeting_id}")
            return response.data
        else:
            print(f"⚠️ No audio chunks found for meeting {meeting_id}")
            return []

    except Exception as e:
        print(f"❌ Error fetching audio chunks for meeting {meeting_id}: {e}")
        raise


# --- Ownership / quota lookups used by POST /jobs (service key; RLS does not apply) ----------

ACTIVE_JOB_STATUSES = ["awaiting_upload", "pending", "processing"]


def get_recording_row(user_id: str, meeting_id: str) -> Optional[Dict[str, Any]]:
    """The `recordings` row the app inserts right after uploading a meeting's audio
    (columns: user_id, meeting_id, file_path, duration seconds, file_size), or None."""
    response = (
        supabase.table("recordings").select("file_path, duration, file_size")
        .eq("user_id", user_id).eq("meeting_id", meeting_id).limit(1).execute()
    )
    return (response.data or [None])[0]


def get_chunk_rows(user_id: str, meeting_id: str) -> list[Dict[str, Any]]:
    """Uploaded `audio_chunks` rows of a meeting (owner-filtered)."""
    response = (
        supabase.table("audio_chunks").select("chunk_index, duration_seconds, file_size")
        .eq("user_id", user_id).eq("meeting_id", meeting_id).order("chunk_index").execute()
    )
    return response.data or []


def list_active_jobs(user_id: str, limit: int = 200) -> list[Dict[str, Any]]:
    """The user's jobs that are still queued or running (awaiting_upload, pending, processing)."""
    def query(statuses):
        return (
            supabase.table("transcription_jobs")
            .select("id, meeting_id, status, is_chunked, duration, created_at")
            .eq("user_id", user_id).in_("status", statuses)
            .order("created_at", desc=True).limit(limit).execute()
        ).data or []
    try:
        return query(ACTIVE_JOB_STATUSES)
    except Exception:
        # Migration 006 (enum value awaiting_upload) not applied yet: PostgREST rejects the
        # unknown enum literal. Fall back to the statuses every schema has.
        return query(["pending", "processing"])


def update_chunk_transcript(chunk_id: str, transcript: str) -> Dict[str, Any]:
    """
    Update a chunk with its transcript and mark as transcribed

    Args:
        chunk_id: UUID of the chunk to update
        transcript: Transcription text for this chunk

    Returns:
        Dict containing updated chunk data

    Raises:
        Exception: If update fails
    """
    try:
        update_data = {
            "transcript": transcript,
            "transcribed": True
        }

        response = supabase.table("audio_chunks").update(update_data).eq("id", chunk_id).execute()

        if response.data and len(response.data) > 0:
            chunk = response.data[0]
            print(f"✅ Updated chunk {chunk_id} with transcript ({len(transcript)} chars)")
            return chunk
        else:
            raise Exception(f"Failed to update chunk {chunk_id}: No data returned")

    except Exception as e:
        print(f"❌ Error updating chunk {chunk_id}: {e}")
        raise


def update_chunks_processed(job_id: str, chunks_processed: int) -> Dict[str, Any]:
    """
    Update the number of chunks processed for a job

    Args:
        job_id: UUID of the job to update
        chunks_processed: Number of chunks successfully processed

    Returns:
        Dict containing updated job data

    Raises:
        Exception: If update fails
    """
    try:
        update_data = {"chunks_processed": chunks_processed}

        response = supabase.table("transcription_jobs").update(update_data).eq("id", job_id).execute()

        if response.data and len(response.data) > 0:
            return response.data[0]
        else:
            raise Exception(f"Failed to update chunks_processed for job {job_id}: No data returned")

    except Exception as e:
        print(f"❌ Error updating chunks_processed for job {job_id}: {e}")
        raise


def increment_retry_count(job_id: str, error_message: str) -> Dict[str, Any]:
    """
    Increment the retry count for a job and reset status to pending for retry.

    Args:
        job_id: UUID of the job to update
        error_message: Error message from the failed attempt

    Returns:
        Dict containing updated job data

    Raises:
        Exception: If update fails
    """
    try:
        # First get current retry count
        job = get_job(job_id)
        if not job:
            raise Exception(f"Job {job_id} not found")

        current_retry = job.get("retry_count", 0) or 0
        new_retry_count = current_retry + 1

        update_data = {
            "retry_count": new_retry_count,
            "status": "pending",  # Reset to pending for next cron run
            "error_message": f"Retry {new_retry_count}: {error_message}",
            "progress_percentage": 0,
            "current_stage": f"Waiting for retry ({new_retry_count}/5)..."
        }

        response = supabase.table("transcription_jobs").update(update_data).eq("id", job_id).execute()

        if response.data and len(response.data) > 0:
            job = response.data[0]
            print(f"🔄 Job {job_id} queued for retry (attempt {new_retry_count}/5)")
            return job
        else:
            raise Exception(f"Failed to increment retry count for job {job_id}: No data returned")

    except Exception as e:
        print(f"❌ Error incrementing retry count for job {job_id}: {e}")
        raise


# ---------------------------------------------------------------------------
# Background upload support (upload_pending jobs)
#
# Storage REST calls go straight to {SUPABASE_URL}/storage/v1 with the service key.
# Signed upload URLs carry a bearer-style token in their query string: NEVER log
# them or anything derived from them (see create_signed_upload_url).
# ---------------------------------------------------------------------------
import base64
import json as _json
import time as _time
import urllib.parse as _urlparse

import httpx

import audio_access

RECORDINGS_BUCKET = os.getenv("RECORDINGS_BUCKET", "recordings")
STORAGE_BASE_URL = f"{SUPABASE_URL.rstrip('/')}/storage/v1"
# Supabase Storage signs upload URLs for a fixed 2 hours (server-side setting);
# used only when the token's own `exp` cannot be read.
DEFAULT_SIGNED_UPLOAD_SECONDS = 7200

AWAITING_UPLOAD = "awaiting_upload"

storage_http = httpx.Client(timeout=httpx.Timeout(15.0, connect=10.0))


class StorageError(RuntimeError):
    """Sanitized storage failure (never includes URLs, tokens or response bodies)."""


def _storage_headers(**extra: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {SUPABASE_SERVICE_KEY}", "apikey": SUPABASE_SERVICE_KEY, **extra}


def _quote_path(path: str) -> str:
    return "/".join(_urlparse.quote(part, safe="") for part in path.split("/"))


def _token_expiry(token: str) -> Optional[float]:
    """`exp` of the (storage-signed) upload token, read WITHOUT verifying it."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = _json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return float(exp) if isinstance(exp, (int, float)) else None
    except Exception:
        return None


def create_signed_upload_url(path: str, upsert: bool = True) -> Dict[str, Any]:
    """Mint a Supabase Storage signed upload URL for `path` in the recordings bucket.

    POST /storage/v1/object/upload/sign/<bucket>/<path> with the service key; the
    response is {"url": "/object/upload/sign/<bucket>/<path>?token=<jwt>"}. With
    `x-upsert: true` the token allows replacing an existing object (a retried upload).

    Returns {"upload_url": str, "expires_at": epoch seconds}. Raises StorageError.
    """
    headers = _storage_headers(**{"x-upsert": "true"} if upsert else {})
    try:
        response = storage_http.post(
            f"{STORAGE_BASE_URL}/object/upload/sign/{RECORDINGS_BUCKET}/{_quote_path(path)}",
            headers=headers,
        )
    except httpx.HTTPError as error:
        raise StorageError(f"signed upload URL request failed ({type(error).__name__})") from None
    if response.status_code >= 300:
        raise StorageError(f"signed upload URL request returned HTTP {response.status_code}")
    try:
        relative = response.json()["url"]
        token = _urlparse.parse_qs(_urlparse.urlparse(relative).query)["token"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        raise StorageError("signed upload URL response was malformed") from None
    upload_url = relative if relative.startswith("http") else f"{STORAGE_BASE_URL}/{relative.lstrip('/')}"
    expires_at = _token_expiry(token) or (_time.time() + DEFAULT_SIGNED_UPLOAD_SECONDS)
    return {"upload_url": upload_url, "expires_at": expires_at}


def get_storage_object_size(path: str) -> Optional[int]:
    """Size in bytes of a finished object, or None if it is absent or not finished.

    Uses POST /storage/v1/object/list/<bucket> (prefix + exact-name match); the
    per-object `metadata.size` is only populated once the upload has completed, which
    is what makes an exact size comparison a reliable "upload finished" signal.
    Raises StorageError when storage itself cannot be queried.
    """
    directory, _, name = path.rpartition("/")
    try:
        response = storage_http.post(
            f"{STORAGE_BASE_URL}/object/list/{RECORDINGS_BUCKET}",
            headers=_storage_headers(),
            json={"prefix": directory, "search": name, "limit": 100, "offset": 0},
        )
    except httpx.HTTPError as error:
        raise StorageError(f"storage list failed ({type(error).__name__})") from None
    if response.status_code >= 300:
        raise StorageError(f"storage list returned HTTP {response.status_code}")
    try:
        entries = response.json()
    except ValueError:
        raise StorageError("storage list response was malformed") from None
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and entry.get("name") == name:
            size = (entry.get("metadata") or {}).get("size")
            return int(size) if isinstance(size, (int, float)) and not isinstance(size, bool) else None
    return None


def delete_storage_object(path: str) -> None:
    """Best-effort removal of an object (used for expired, never-promoted uploads)."""
    try:
        supabase.storage.from_(RECORDINGS_BUCKET).remove([path])
    except Exception as error:
        print(f"   ⚠️ Could not delete storage object for expired upload ({type(error).__name__})")


class DownloadTooLarge(StorageError):
    """The object is bigger than the download cap. Text matches jobs.is_retryable_error's
    permanent patterns ("exceeds maximum"), so such a job fails instead of retrying."""


def _stream_storage_object(path: str, write, max_bytes: Optional[int] = None) -> int:
    """GET an object with the service key and feed it to `write(bytes)` in 1 MiB blocks.

    The caller must pass a validated path (audio_access.validate_storage_path): this
    function trusts it. Redirects are never followed, and the download is aborted as soon
    as it exceeds `max_bytes` (default: MAX_DOWNLOAD_BYTES, 300 MiB), by Content-Length
    first and by counting streamed bytes otherwise. Returns the number of bytes written.
    """
    limit = max_bytes or audio_access.max_download_bytes()
    url = f"{STORAGE_BASE_URL}/object/authenticated/{RECORDINGS_BUCKET}/{_quote_path(path)}"
    total = 0
    try:
        with storage_http.stream("GET", url, headers=_storage_headers(), follow_redirects=False,
                                 timeout=httpx.Timeout(600.0, connect=15.0)) as response:
            if response.status_code >= 300:
                raise StorageError(f"storage download returned HTTP {response.status_code}")
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > limit:
                raise DownloadTooLarge(f"audio file exceeds maximum download size of {limit} bytes")
            for block in response.iter_bytes(1024 * 1024):
                total += len(block)
                if total > limit:
                    raise DownloadTooLarge(f"audio file exceeds maximum download size of {limit} bytes")
                write(block)
    except httpx.HTTPError as error:
        raise StorageError(f"storage download failed ({type(error).__name__})") from None
    return total


def download_storage_object_to_file(path: str, dest_path: str, max_bytes: Optional[int] = None) -> None:
    """Stream a bucket object to disk using the service key (constant memory, size-capped)."""
    try:
        with open(dest_path, "wb") as out:
            _stream_storage_object(path, out.write, max_bytes)
    except BaseException:
        try:
            os.remove(dest_path)
        except OSError:
            pass
        raise


def download_storage_object_bytes(path: str, max_bytes: Optional[int] = None) -> bytes:
    """Download a bucket object into memory (size-capped); for the pydub/regular path."""
    buffer = bytearray()
    _stream_storage_object(path, buffer.extend, max_bytes)
    return bytes(buffer)


def _rows(response) -> list:
    return response.data or []


def find_upload_job(user_id: str, meeting_id: str) -> Optional[Dict[str, Any]]:
    """Newest non-failed upload-mode job of this meeting (idempotency lookup)."""
    response = (
        supabase.table("transcription_jobs").select("*")
        .eq("user_id", user_id).eq("meeting_id", meeting_id)
        .not_.is_("storage_path", "null").neq("status", "failed")
        .order("created_at", desc=True).limit(1).execute()
    )
    rows = _rows(response)
    return rows[0] if rows else None


def is_unique_violation(error: Exception) -> bool:
    text = f"{getattr(error, 'code', '')} {error}".lower()
    return "23505" in text or "duplicate key" in text or "unique constraint" in text


def insert_upload_job(data: Dict[str, Any]) -> Dict[str, Any]:
    response = supabase.table("transcription_jobs").insert(data).execute()
    rows = _rows(response)
    if not rows:
        raise Exception("Failed to create job: No data returned")
    return rows[0]


def transition_job_status(job_id: str, from_status: str, to_status: str, **fields: Any) -> Optional[Dict[str, Any]]:
    """Atomic compare-and-set: UPDATE ... WHERE id=? AND status=<from_status>.

    Returns the updated row, or None when another worker/loop/request already moved
    the job (nothing is written in that case).
    """
    response = (
        supabase.table("transcription_jobs").update({"status": to_status, **fields})
        .eq("id", job_id).eq("status", from_status).execute()
    )
    rows = _rows(response)
    return rows[0] if rows else None


def update_awaiting_job(job_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
    """Update columns of a job only while it is still awaiting_upload (CAS on status)."""
    response = (
        supabase.table("transcription_jobs").update(fields)
        .eq("id", job_id).eq("status", AWAITING_UPLOAD).execute()
    )
    rows = _rows(response)
    return rows[0] if rows else None


def list_awaiting_upload_jobs(limit: int = 200) -> list:
    response = (
        supabase.table("transcription_jobs").select("*")
        .eq("status", AWAITING_UPLOAD).order("created_at").limit(limit).execute()
    )
    return _rows(response)


# --- Live Activity (APNs) support -------------------------------------------------------
# Used by apns.py from a background thread. Tokens are uploaded by the iOS app
# (RLS: own rows only); the service key bypasses RLS to read/delete them.

def update_job_stage(job_id: str, stage: str) -> None:
    """Persist transcription_jobs.stage (queued|preparing|transcribing|summarizing|done|failed).

    Deliberately separate from update_job_progress: if migration 009 has not been
    applied yet this raises (caught by apns.py) instead of breaking job processing.
    """
    supabase.table("transcription_jobs").update({"stage": stage}).eq("id", job_id).execute()


def get_live_activity_tokens(job_id: str) -> list[Dict[str, Any]]:
    """Live Activity push tokens registered for a job (may be empty)."""
    response = (
        supabase.table("live_activity_tokens")
        .select("token, environment, bundle_id")
        .eq("job_id", job_id)
        .execute()
    )
    return response.data or []


def delete_live_activity_token(job_id: str, token: str) -> None:
    """Remove one token (APNs said it is dead: 410 / BadDeviceToken / Unregistered)."""
    supabase.table("live_activity_tokens").delete().eq("job_id", job_id).eq("token", token).execute()


def delete_live_activity_tokens(job_id: str) -> None:
    """Remove every token of a job (its Live Activity has ended)."""
    supabase.table("live_activity_tokens").delete().eq("job_id", job_id).execute()
