"""Cloud transcription routing. Provider failures never change providers."""
import io
import mimetypes
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, List, Optional

import httpx
from openai import OpenAI

from ai_config import create_transcription, get_task_config

XAI_DEFAULT_CONFIG = {
    'model': 'grok-voice-transcribe-2.0',
    'reasoning_effort': None,
    'verbosity': None,
    'fallback_model': None,
}


XAI_STT_URL = 'https://api.x.ai/v1/stt'
XAI_CHUNK_TIMEOUT_SECONDS = 120.0  # per-chunk requests (small files)


@dataclass
class TranscriptionResult:
    """Provider-neutral transcription output.

    `words` is the raw word list when the provider returns one (kept so speaker
    labels / timestamps can be added later without changing call sites).
    """
    text: str
    words: Optional[List[Any]] = None
    duration: Optional[float] = None
    provider: str = 'openai'


class TranscriptionProviderError(RuntimeError):
    """Sanitized, status-bearing error understood by the worker retry classifier."""
    def __init__(self, message: str, status_code: Optional[int] = None):
        self.status_code = status_code
        super().__init__(message)


def validate_transcription_provider(provider: str) -> str:
    if provider not in ('openai', 'xai'):
        raise ValueError('Invalid transcription provider')
    return provider


def _xai_request(http: httpx.Client, key: str, file, filename: str,
                 language: Optional[str], config: dict) -> TranscriptionResult:
    """One xAI STT exchange (plus the optional configured model fallback)."""
    mime_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'

    def call(model: str) -> httpx.Response:
        file.seek(0)
        options = {'model': model}
        if language:
            options.update(language=language, format='true')
        try:
            # httpx encodes data fields before files and streams file objects
            # from disk. Rebuild for every attempt.
            return http.post(
                XAI_STT_URL,
                headers={'Authorization': f'Bearer {key}'},
                data=options, files={'file': (filename, file, mime_type)},
            )
        except httpx.TimeoutException:
            raise TranscriptionProviderError('xAI transcription timed out') from None
        except httpx.RequestError:
            raise TranscriptionProviderError('xAI transcription connection error') from None

    response = call(config['model'])
    fallback = config['fallback_model']
    if response.status_code in (400, 404) and fallback and fallback != config['model']:
        response.close()
        response = call(fallback)
    if not response.is_success:
        raise TranscriptionProviderError(
            f'xAI transcription returned HTTP {response.status_code}', response.status_code,
        )
    try:
        output = response.json()
        text = output.get('text') if isinstance(output, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise ValueError('Invalid transcript')
    except (ValueError, TypeError):
        raise TranscriptionProviderError('xAI transcription returned invalid text (HTTP 502)', 502) from None
    words = output.get('words')
    duration = output.get('duration')
    return TranscriptionResult(
        text=text,
        words=words if isinstance(words, list) else None,
        duration=float(duration) if isinstance(duration, (int, float)) and not isinstance(duration, bool) else None,
        provider='xai',
    )


def require_xai_key() -> str:
    key = os.getenv('XAI_API_KEY')
    if not key:
        raise TranscriptionProviderError('xAI transcription is not configured')
    return key


def create_provider_transcription(
    client: OpenAI, file: io.BytesIO,
    language: Optional[str] = None, provider: str = 'openai',
) -> str:
    validate_transcription_provider(provider)
    if provider == 'openai':
        return create_transcription(client, file, language)

    key = require_xai_key()
    config = get_task_config('transcription_xai', XAI_DEFAULT_CONFIG)
    filename = getattr(file, 'name', 'audio.m4a')
    with httpx.Client(timeout=XAI_CHUNK_TIMEOUT_SECONDS) as http:
        return _xai_request(http, key, file, filename, language, config).text


def is_transient_provider_error(error: Exception) -> bool:
    """Timeouts, connection errors, 408/425/429 and 5xx are worth retrying."""
    if not isinstance(error, TranscriptionProviderError):
        return False
    status = error.status_code
    return status is None or status in (408, 425, 429) or status >= 500


def is_auth_provider_error(error: Exception) -> bool:
    return isinstance(error, TranscriptionProviderError) and error.status_code in (401, 403)


def transcribe_xai_file(
    path: str, language: Optional[str] = None, *,
    timeout: float, max_attempts: int = 3,
    backoff_seconds: tuple = (10.0, 30.0, 60.0),
    sleep: Callable[[float], None] = time.sleep,
    filename: Optional[str] = None,
) -> TranscriptionResult:
    """Send ONE xAI STT request for a whole audio file on disk.

    The multipart body is streamed from the file handle (never read into RAM).
    Transient failures are retried; anything else (including 401/403) is raised
    immediately. The caller decides about falling back.
    """
    key = require_xai_key()
    config = get_task_config('transcription_xai', XAI_DEFAULT_CONFIG)
    filename = filename or os.path.basename(path)
    # Large body: generous write/read budgets, short connect/pool budgets.
    limits = httpx.Timeout(timeout, connect=30.0, pool=30.0)
    attempts = max(1, max_attempts)
    with open(path, 'rb') as file, httpx.Client(timeout=limits) as http:
        for attempt in range(1, attempts + 1):
            try:
                return _xai_request(http, key, file, filename, language, config)
            except TranscriptionProviderError as error:
                if attempt >= attempts or not is_transient_provider_error(error):
                    raise
                delay = backoff_seconds[min(attempt - 1, len(backoff_seconds) - 1)]
                print(f'   ⚠️ xAI single request attempt {attempt}/{attempts} failed ({error}); retrying in {delay:.0f}s')
                sleep(delay)
    raise AssertionError('unreachable')
