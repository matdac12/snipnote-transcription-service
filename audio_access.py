"""Which stored audio the worker may fetch for a job (SSRF / cross-user / size guards).

Pure functions, no network and no Supabase client, so they are cheap to test.

Threat model (audit A2/A4): `transcription_jobs.audio_url` and `audio_chunks.file_path`
are attacker-controllable (legacy POST /jobs trusts the body; the tables are writable by
signed-in users over PostgREST). The worker holds the service key, so it must never
follow an arbitrary URL nor read another user's object.

Rules
  * Legacy `audio_url` must be an https URL on the configured Supabase host
    (SUPABASE_URL's host, plus AUDIO_URL_ALLOWED_HOSTS for a custom domain) whose path is
    `/storage/v1/object/(public|sign|authenticated)/<recordings bucket>/<job.user_id>/<...>`.
    The object is then fetched by PATH through the service-key storage API (never by the
    URL itself, so any `?token=` in a signed URL is ignored, redirects are not followed,
    and it keeps working once the bucket is made private).
  * Chunk `file_path`s must live under `<job.user_id>/` in the same bucket.
  * Every download has a hard size cap (MAX_DOWNLOAD_BYTES, default 300 MiB).
"""
import os
import re
from typing import Optional
from urllib.parse import unquote, urlsplit

DEFAULT_MAX_DOWNLOAD_BYTES = 300 * 1024 * 1024
DEFAULT_MAX_CHUNKS_PER_JOB = 200
_URL_PATH_RE = re.compile(r'^/storage/v1/object/(?:public|sign|authenticated)/(?P<bucket>[^/]+)/(?P<path>.+)$')
_UUID_RE = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')


class AudioAccessError(Exception):
    """Permanent (non-retryable) refusal. The text contains "invalid audio" on purpose:
    jobs.is_retryable_error classifies that as permanent, so the job fails at once.
    Messages never include the URL (it may carry a signed token)."""

    def __init__(self, reason: str):
        super().__init__(f'invalid audio location: {reason}')


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, '') or default)
    except ValueError:
        return default
    return value if value > 0 else default


def max_download_bytes() -> int:
    """Hard cap for one storage download (env MAX_DOWNLOAD_BYTES, default 300 MiB)."""
    return _env_int('MAX_DOWNLOAD_BYTES', DEFAULT_MAX_DOWNLOAD_BYTES)


def max_chunks_per_job() -> int:
    return _env_int('MAX_CHUNKS_PER_JOB', DEFAULT_MAX_CHUNKS_PER_JOB)


def recordings_bucket() -> str:
    return os.getenv('RECORDINGS_BUCKET', 'recordings')


def is_uuid(value: object) -> bool:
    return isinstance(value, str) and bool(_UUID_RE.match(value))


def _supabase_parts():
    return urlsplit(os.getenv('SUPABASE_URL', ''))


def _default_port(scheme: str) -> int:
    return 443 if scheme == 'https' else 80


def _allowed_hosts() -> set:
    hosts = set()
    base = _supabase_parts().hostname
    if base:
        hosts.add(base.lower())
    for extra in os.getenv('AUDIO_URL_ALLOWED_HOSTS', '').split(','):
        extra = extra.strip().lower()
        if extra:
            hosts.add(extra)
    return hosts


def validate_storage_path(path: object, user_id: object) -> str:
    """Return `path` (normalised) if it is `<user_id>/<name...>` with no traversal, else raise."""
    if not is_uuid(user_id):
        raise AudioAccessError('job has no valid user id')
    if not isinstance(path, str) or not path or len(path) > 512:
        raise AudioAccessError('missing or oversized storage path')
    if '\\' in path or '%' in path or any(ord(c) < 32 or ord(c) == 127 for c in path):
        raise AudioAccessError('storage path contains forbidden characters')
    segments = path.split('/')
    if len(segments) < 2 or any(s in ('', '.', '..') for s in segments):
        raise AudioAccessError('storage path is not <user>/<file>')
    if segments[0].lower() != str(user_id).lower():
        raise AudioAccessError('storage path belongs to another user')
    return '/'.join([segments[0].lower(), *segments[1:]])


def storage_path_from_url(audio_url: object, user_id: object) -> str:
    """Validate a legacy `audio_url` for `user_id` and return its storage path."""
    if not isinstance(audio_url, str) or not audio_url:
        raise AudioAccessError('job has no audio URL')
    try:
        parts = urlsplit(audio_url)
        port = parts.port  # raises ValueError on a bad port
    except ValueError:
        raise AudioAccessError('malformed URL') from None
    base = _supabase_parts()
    allowed_schemes = {'https'} | ({'http'} if base.scheme == 'http' else set())
    if parts.scheme not in allowed_schemes:
        raise AudioAccessError('URL scheme not allowed')
    if parts.username is not None or parts.password is not None:
        raise AudioAccessError('URL must not contain credentials')
    host = (parts.hostname or '').lower()
    if host not in _allowed_hosts():
        raise AudioAccessError('URL host is not the Supabase project host')
    expected_port = base.port if host == (base.hostname or '').lower() and base.port else _default_port(parts.scheme)
    if (port or _default_port(parts.scheme)) != expected_port:
        raise AudioAccessError('URL port not allowed')
    match = _URL_PATH_RE.match(unquote(parts.path))  # one decode; a leftover '%' is rejected below
    if not match:
        raise AudioAccessError('URL is not a storage object URL')
    if match.group('bucket') != recordings_bucket():
        raise AudioAccessError('URL points at another bucket')
    return validate_storage_path(match.group('path'), user_id)
