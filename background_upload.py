"""Background upload: job creation (API side) and promotion / expiry (worker side).

Flow
  1. iOS calls POST /jobs {upload_pending: true, ...} with its Supabase JWT.
     `create_upload_job` creates a job with status `awaiting_upload`, mints a signed
     upload URL for `<user_id>/<meeting_id>.<ext>` and returns it. Calling again for
     the same meeting returns the same job with a FRESH signed URL (idempotent).
  2. The app hands the file to a background URLSession (raw PUT) and may be killed.
  3. Every worker loop calls `promote_uploaded_jobs()`: when the object exists and
     its size equals `expected_bytes` the job is flipped to `pending` (compare-and-set
     on status, so concurrent loops/workers cannot double-promote); jobs whose upload
     deadline has passed are failed with `upload_expired` and their object removed.

Nothing here logs signed URLs or tokens.
"""
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import supabase_client as db
from transcription_provider import validate_transcription_provider

DEFAULT_TTL_SECONDS = 6 * 3600
DEFAULT_MAX_UPLOAD_BYTES = 300 * 1024 * 1024
DEFAULT_CONTENT_TYPES = {
    'm4a': 'audio/mp4', 'mp4': 'audio/mp4', 'aac': 'audio/aac', 'mp3': 'audio/mpeg',
    'wav': 'audio/wav', 'caf': 'audio/x-caf', 'flac': 'audio/flac', 'ogg': 'audio/ogg',
    'webm': 'audio/webm',
}
CONTENT_TYPE_RE = re.compile(r'^(audio|video)/[a-z0-9][a-z0-9.+-]*$')
EXPIRED_ERROR = 'upload_expired'
# Consecutive storage failures after which one promotion pass gives up (a storage
# outage must not stall the worker loop for the whole awaiting backlog).
MAX_CONSECUTIVE_STORAGE_FAILURES = 3


class UploadRequestError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, ''))
        return value if value >= 0 else default
    except ValueError:
        return default


def ttl_seconds() -> int:
    return _env_int('UPLOAD_PENDING_TTL_SECONDS', DEFAULT_TTL_SECONDS) or DEFAULT_TTL_SECONDS


def max_upload_bytes() -> int:
    return _env_int('MAX_UPLOAD_BYTES', DEFAULT_MAX_UPLOAD_BYTES) or DEFAULT_MAX_UPLOAD_BYTES


def size_tolerance_bytes() -> int:
    """Allowed |actual - expected|. Default 0: a raw PUT stores exactly the bytes sent and
    the app knows the file size exactly, so anything else is a truncated/other file."""
    return _env_int('UPLOAD_SIZE_TOLERANCE_BYTES', 0)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_ts(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def validate_upload_request(meeting_id: str, expected_bytes: Optional[int], file_extension: Optional[str],
                            content_type: Optional[str]) -> Dict[str, Any]:
    """Return normalised {meeting_id, ext, content_type}; raises UploadRequestError(400)."""
    try:
        meeting = str(uuid.UUID(str(meeting_id)))  # canonical lowercase: this becomes a storage path
    except ValueError:
        raise UploadRequestError(400, 'meeting_id must be a UUID') from None
    if expected_bytes is None or isinstance(expected_bytes, bool) or expected_bytes <= 0:
        raise UploadRequestError(400, 'expected_bytes is required and must be positive')
    if expected_bytes > max_upload_bytes():
        raise UploadRequestError(413, f'expected_bytes exceeds the {max_upload_bytes()} byte limit')
    ext = (file_extension or 'm4a').strip().lstrip('.').lower()
    if ext not in DEFAULT_CONTENT_TYPES:
        raise UploadRequestError(400, 'Unsupported file_extension')
    ctype = (content_type or DEFAULT_CONTENT_TYPES[ext]).strip().lower()
    if not CONTENT_TYPE_RE.match(ctype):
        raise UploadRequestError(400, 'Unsupported content_type')
    return {'meeting_id': meeting, 'ext': ext, 'content_type': ctype}


def storage_path_for(user_id: str, meeting_id: str, ext: str) -> str:
    # Same convention as SupabaseManager.uploadAudioRecording: <userId>/<meetingId>.<ext>
    return f'{user_id.lower()}/{meeting_id}.{ext}'


def _response(job: Dict[str, Any], signed: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        'job_id': job['id'], 'status': job['status'], 'created_at': job['created_at'],
        'storage_path': job.get('storage_path'), 'expected_bytes': job.get('expected_bytes'),
        'upload_deadline': job.get('upload_deadline'),
    }
    if signed:
        out.update(
            upload_url=signed['upload_url'],
            upload_method='PUT',
            upload_headers={'Content-Type': job.get('content_type') or 'application/octet-stream', 'x-upsert': 'true'},
            expires_at=_iso(datetime.fromtimestamp(signed['expires_at'], timezone.utc)),
        )
    return out


def create_upload_job(
    *, user_id: str, meeting_id: str, expected_bytes: Optional[int], file_extension: Optional[str] = None,
    content_type: Optional[str] = None, duration: Optional[float] = None, language: Optional[str] = None,
    provider: str = 'openai', now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Create (or re-arm) an `awaiting_upload` job. Blocking: call from a worker thread."""
    validate_transcription_provider(provider)
    if language is not None and not re.fullmatch(r'[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?', language):
        raise UploadRequestError(400, 'language must be an ISO-639-1 code')
    req = validate_upload_request(meeting_id, expected_bytes, file_extension, content_type)
    now = now or datetime.now(timezone.utc)
    deadline = _iso(now + timedelta(seconds=ttl_seconds()))
    path = storage_path_for(user_id, req['meeting_id'], req['ext'])

    existing = db.find_upload_job(user_id, req['meeting_id'])
    if existing and existing['status'] != db.AWAITING_UPLOAD:
        # Already uploaded and queued/processing/done: nothing to upload again.
        return _response(existing, None)

    try:
        signed = db.create_signed_upload_url(path)
    except db.StorageError as error:
        print(f'❌ Could not mint signed upload URL: {error}')
        raise UploadRequestError(502, 'Could not create upload URL, retry shortly') from None

    if existing:
        job = db.update_awaiting_job(
            existing['id'], expected_bytes=expected_bytes, content_type=req['content_type'],
            storage_path=path, upload_deadline=deadline,
        )
        if job is None:  # promoted/expired between lookup and update
            current = db.find_upload_job(user_id, req['meeting_id'])
            if current and current['status'] != db.AWAITING_UPLOAD:
                return _response(current, None)
            raise UploadRequestError(409, 'Job state changed, retry')
        print(f'♻️ Re-armed upload job {job["id"]} for user {user_id}')
        return _response(job, signed)

    data: Dict[str, Any] = {
        'user_id': user_id, 'meeting_id': req['meeting_id'], 'status': db.AWAITING_UPLOAD,
        'is_chunked': False, 'total_chunks': 1, 'chunks_processed': 0,
        'transcription_provider': provider, 'expected_bytes': expected_bytes,
        'storage_path': path, 'content_type': req['content_type'], 'upload_deadline': deadline,
    }
    if duration:
        data['duration'] = duration
    if language:
        data['language'] = language
    try:
        job = db.insert_upload_job(data)
    except Exception as error:
        if not db.is_unique_violation(error):
            raise
        # Concurrent first call for the same meeting won the insert: return its job.
        job = db.find_upload_job(user_id, req['meeting_id'])
        if job is None:
            raise
        return _response(job, signed if job['status'] == db.AWAITING_UPLOAD else None)
    print(f'✅ Created upload job {job["id"]} for user {user_id} ({expected_bytes} bytes expected)')
    return _response(job, signed)


# ---------------------------------------------------------------------------
# Worker side
# ---------------------------------------------------------------------------

def _size_matches(actual: Optional[int], expected: Any) -> bool:
    if actual is None or not isinstance(expected, int) or expected <= 0:
        return False
    return abs(actual - expected) <= size_tolerance_bytes()


def _deadline(job: Dict[str, Any]) -> Optional[datetime]:
    return _parse_ts(job.get('upload_deadline')) or (
        (_parse_ts(job.get('created_at')) or datetime.now(timezone.utc)) + timedelta(seconds=ttl_seconds()))


def promote_uploaded_jobs(now: Optional[datetime] = None) -> Dict[str, int]:
    """Promote finished uploads to `pending`; fail and clean up expired ones.

    Safe to run from several loops/workers at once: both transitions are
    compare-and-set on `status = 'awaiting_upload'`, so exactly one caller wins each
    job. A job whose upload finished exactly at its deadline is promoted, not expired.
    Never raises (a failure here must not stop the worker from processing jobs).
    """
    stats = {'promoted': 0, 'expired': 0, 'waiting': 0, 'errors': 0}
    now = now or datetime.now(timezone.utc)
    try:
        jobs = db.list_awaiting_upload_jobs()
    except Exception as error:
        print(f'⚠️ promote_uploaded_jobs: could not list awaiting jobs ({type(error).__name__}: {error})')
        stats['errors'] += 1
        return stats

    failures = 0
    for job in jobs:
        job_id, path = job['id'], job.get('storage_path')
        try:
            # A row without a path can never be uploaded to: let it fall through to expiry.
            size = db.get_storage_object_size(path) if path else None
            failures = 0
        except Exception as error:
            failures += 1
            stats['errors'] += 1
            print(f'⚠️ promote_uploaded_jobs: storage check failed for job {job_id}: {error}')
            if failures >= MAX_CONSECUTIVE_STORAGE_FAILURES:
                print('⚠️ promote_uploaded_jobs: storage unavailable, aborting this pass')
                break
            continue  # unknown state: never expire a job we could not check

        try:
            if _size_matches(size, job.get('expected_bytes')):
                won = db.transition_job_status(
                    job_id, db.AWAITING_UPLOAD, 'pending',
                    progress_percentage=0, current_stage='Upload received, queued',
                )
                if won:
                    stats['promoted'] += 1
                    print(f'📥 Upload complete, job {job_id} promoted to pending ({size} bytes)')
                continue
            if now >= _deadline(job):
                won = db.transition_job_status(
                    job_id, db.AWAITING_UPLOAD, 'failed',
                    error_message=EXPIRED_ERROR, progress_percentage=0,
                    current_stage='Upload expired' if size is None else 'Upload incomplete (size mismatch)',
                )
                if won:  # only the winner deletes, so a promoted job's file is never removed
                    stats['expired'] += 1
                    detail = 'object absent' if size is None else f'size {size} != expected {job.get("expected_bytes")}'
                    print(f'⌛ Job {job_id} upload expired ({detail})')
                    if size is not None and path:
                        db.delete_storage_object(path)
                continue
            stats['waiting'] += 1
        except Exception as error:
            stats['errors'] += 1
            print(f'⚠️ promote_uploaded_jobs: job {job_id} update failed ({type(error).__name__}: {error})')
    return stats
