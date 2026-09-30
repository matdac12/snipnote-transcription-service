"""Cloud transcription routing. Provider failures never change providers."""
import io
import mimetypes
import os
from typing import Optional

import httpx
from openai import OpenAI

from ai_config import create_transcription, get_task_config

XAI_DEFAULT_CONFIG = {
    'model': 'grok-voice-transcribe-2.0',
    'reasoning_effort': None,
    'verbosity': None,
    'fallback_model': None,
}


class TranscriptionProviderError(RuntimeError):
    """Sanitized, status-bearing error understood by the worker retry classifier."""
    def __init__(self, message: str, status_code: Optional[int] = None):
        self.status_code = status_code
        super().__init__(message)


def validate_transcription_provider(provider: str) -> str:
    if provider not in ('openai', 'xai'):
        raise ValueError('Invalid transcription provider')
    return provider


def create_provider_transcription(
    client: OpenAI, file: io.BytesIO,
    language: Optional[str] = None, provider: str = 'openai',
) -> str:
    validate_transcription_provider(provider)
    if provider == 'openai':
        return create_transcription(client, file, language)

    key = os.getenv('XAI_API_KEY')
    if not key:
        raise TranscriptionProviderError('xAI transcription is not configured')
    config = get_task_config('transcription_xai', XAI_DEFAULT_CONFIG)
    filename = getattr(file, 'name', 'audio.m4a')
    mime_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'

    def call(http: httpx.Client, model: str) -> httpx.Response:
        file.seek(0)
        options = {'model': model}
        if language:
            options.update(language=language, format='true')
        try:
            # httpx encodes data fields before files. Rebuild for every attempt.
            return http.post(
                'https://api.x.ai/v1/stt',
                headers={'Authorization': f'Bearer {key}'},
                data=options, files={'file': (filename, file, mime_type)},
            )
        except httpx.TimeoutException:
            raise TranscriptionProviderError('xAI transcription timed out') from None
        except httpx.RequestError:
            raise TranscriptionProviderError('xAI transcription connection error') from None

    with httpx.Client(timeout=120.0) as http:
        response = call(http, config['model'])
        fallback = config['fallback_model']
        if response.status_code in (400, 404) and fallback and fallback != config['model']:
            response.close()
            response = call(http, fallback)
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
        return text
