"""Policy for POST /jobs (legacy mode) and GET /jobs/{id}: identity, ownership, quotas.

Which checks are ALWAYS ON and which depend on AUTH_MODE (audit A2/A3). The rule: anything
a legitimate old app build (no Authorization header, sends `user_id` in the body, uploads
before creating the job) already satisfies is always on; anything that needs a token or that
could conceivably trip a legitimate old client is mode-dependent.

ALWAYS ON (every AUTH_MODE):
  * user_id / meeting_id must be UUIDs (the app sends `uuidString`)
  * non-chunked jobs need an `audio_url` that passes audio_access (Supabase host, recordings/<user_id>/...)
  * total_chunks in [1, MAX_CHUNKS_PER_JOB]; duration in (0, MAX_JOB_DURATION_SECONDS]
  * an already queued/running job of the same meeting is returned instead of creating a duplicate
  * per-user cap on concurrent queued/running jobs (MAX_ACTIVE_JOBS_PER_USER, default 20; 0 = off)
  * (log and enforce, i.e. whenever tokens are looked at) a valid token whose user differs from
    the body's user_id -> 403: no old client sends tokens, so this cannot hit one

MODE-DEPENDENT:
  * token required (401)                                           enforce only
  * `recordings` row (regular) / `audio_chunks` rows (chunked) for (user_id, meeting_id)
    must exist and match the URL path                               enforce: 404; log: logged as "would reject"; off: skipped
  * (minutes flag, separate) balance check -> 402                   only with SERVER_MINUTES_DEBIT_ENABLED, any mode

Lookups that fail (table missing, DB hiccup) never block a request: they are logged and skipped.
"""
import math
import os
from typing import Any, Dict, Optional

from fastapi import HTTPException

import audio_access
import supabase_client as db

DEFAULT_MAX_ACTIVE_JOBS = 20
DEFAULT_MAX_DURATION_SECONDS = 12 * 3600


def _env_int(name: str, default: int, allow_zero: bool = False) -> int:
    try:
        value = int(os.getenv(name, '') or default)
    except ValueError:
        return default
    return value if value > 0 or (allow_zero and value == 0) else default


def max_active_jobs() -> int:
    return _env_int('MAX_ACTIVE_JOBS_PER_USER', DEFAULT_MAX_ACTIVE_JOBS, allow_zero=True)


def max_duration_seconds() -> int:
    return _env_int('MAX_JOB_DURATION_SECONDS', DEFAULT_MAX_DURATION_SECONDS)


def minutes_for(seconds: Optional[float]) -> int:
    """Same rounding as the app's debit: max(1, ceil(seconds / 60))."""
    return max(1, math.ceil((seconds or 0) / 60)) if seconds else 0


def _log(message: str) -> None:
    print(f'🔐 [policy] {message}', flush=True)


def validate_shape(user_id: Optional[str], meeting_id: str, is_chunked: bool, total_chunks: int,
                   duration: Optional[float], audio_url: Optional[str]) -> Optional[str]:
    """Always-on request validation. Returns the audio storage path (regular jobs) or None."""
    if not audio_access.is_uuid(user_id):
        raise HTTPException(status_code=422, detail='user_id must be a UUID')
    if not audio_access.is_uuid(meeting_id):
        raise HTTPException(status_code=422, detail='meeting_id must be a UUID')
    if not 1 <= total_chunks <= audio_access.max_chunks_per_job():
        raise HTTPException(status_code=422, detail=f'total_chunks must be between 1 and {audio_access.max_chunks_per_job()}')
    if duration is not None and not 0 < duration <= max_duration_seconds():
        raise HTTPException(status_code=422, detail=f'duration must be between 0 and {max_duration_seconds()} seconds')
    if is_chunked:
        return None
    try:
        return audio_access.storage_path_from_url(audio_url, user_id)
    except audio_access.AudioAccessError as error:
        raise HTTPException(status_code=400, detail=str(error))


def check_ownership(mode: str, user_id: str, meeting_id: str, is_chunked: bool, total_chunks: int,
                    storage_path: Optional[str]) -> Dict[str, Any]:
    """Mode-dependent: the uploaded audio for (user_id, meeting_id) must exist.

    Returns {'seconds': best-known audio duration from the app's own rows or None} for the
    minutes check. Never raises except the enforce-mode 404."""
    info: Dict[str, Any] = {'seconds': None}
    if mode == 'off':
        return info
    violation = None
    try:
        if is_chunked:
            rows = db.get_chunk_rows(user_id, meeting_id)
            if not rows:
                violation = 'no audio_chunks rows for this user and meeting'
            elif len(rows) < total_chunks:
                violation = f'only {len(rows)} of {total_chunks} audio_chunks rows uploaded'
            info['seconds'] = sum(float(r.get('duration_seconds') or 0) for r in rows) or None
        else:
            row = db.get_recording_row(user_id, meeting_id)
            if not row:
                violation = 'no recordings row for this user and meeting'
            else:
                if storage_path and row.get('file_path') and row['file_path'].lower() != storage_path.lower():
                    violation = 'audio_url does not match the recordings row'
                info['seconds'] = float(row['duration']) if row.get('duration') else None
    except Exception as error:  # table missing / DB hiccup: never block on a failed lookup
        _log(f'ownership lookup skipped ({type(error).__name__})')
        return info
    if violation:
        if mode == 'enforce':
            raise HTTPException(status_code=404, detail='No uploaded audio found for this meeting')
        _log(f'mode=log would reject POST /jobs: {violation}')
    return info


def find_or_cap_active(user_id: str, meeting_id: str) -> Optional[Dict[str, Any]]:
    """Always on. Returns an existing active job of this meeting (idempotent create), raises 429
    when the user already has too many active jobs. Lookup failures are logged and skipped."""
    try:
        active = db.list_active_jobs(user_id)
    except Exception as error:
        _log(f'active-job lookup skipped ({type(error).__name__})')
        return None
    for job in active:
        if str(job.get('meeting_id', '')).lower() == meeting_id.lower() and job.get('status') in ('pending', 'processing'):
            return job  # (awaiting_upload jobs belong to the upload_pending flow: never handed to a legacy create)
    limit = max_active_jobs()
    if limit and len(active) >= limit:
        raise HTTPException(status_code=429, detail='Too many transcription jobs in progress, try again later',
                            headers={'Retry-After': '60'})
    return None
