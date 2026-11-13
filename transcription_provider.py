"""Cloud transcription routing. Provider failures never change providers."""
import io
import mimetypes
import os
from typing import Optional

import httpx
from openai import OpenAI

from ai_config import create_transcription, get_task_config
from speaker_transcript import render_speaker_transcript, stitch_short_intrusions, turns_from_words
from vocabulary import terms_for_user

XAI_HTTP_TIMEOUT = 1800.0

XAI_DEFAULT_CONFIG = {
    'model': 'grok-voice-transcribe-2.0',
    'reasoning_effort': None,
    'verbosity': None,
    'fallback_model': None,
}


class TranscriptionProviderError(RuntimeError):
    """Sanitized, status-bearing error understood by the worker retry classifier."""
    def __init__(self, message: str, status_code: Optional[int] = None, retryable: Optional[bool] = None):
        self.status_code = status_code
        self.retryable = retryable if retryable is not None else (
            status_code is None or status_code in (408, 429) or status_code >= 500
        )
        super().__init__(message)


def validate_transcription_provider(provider: str) -> str:
    if provider not in ('openai', 'xai'):
        raise ValueError('Invalid transcription provider')
    return provider


def create_provider_transcription(
    client: OpenAI, file: io.BytesIO,
    language: Optional[str] = None, provider: str = 'openai',
    user_id: Optional[str] = None, diarize: bool = False,
) -> str:
    validate_transcription_provider(provider)
    terms = terms_for_user(user_id)
    if provider == 'openai':
        return create_transcription(client, file, language, keywords=terms)

    key = os.getenv('XAI_API_KEY')
    if not key:
        raise TranscriptionProviderError('xAI transcription is not configured', retryable=False)
    config = get_task_config('transcription_xai', XAI_DEFAULT_CONFIG)
    filename = getattr(file, 'name', 'audio.m4a')
    mime_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'

    def call(http: httpx.Client, model: str) -> httpx.Response:
        file.seek(0)
        # A list value repeats the field. Keyterms stay ahead of the file.
        options = {'model': model}
        if language:
            options.update(language=language, format='true')
        if terms:
            options['keyterm'] = terms
        if diarize:
            options['diarize'] = 'true'
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

    with httpx.Client(timeout=XAI_HTTP_TIMEOUT if diarize else 120.0) as http:
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
        except (ValueError, TypeError):
            raise TranscriptionProviderError(
                f'xAI transcription returned invalid response: malformed JSON (HTTP {response.status_code})',
                response.status_code, retryable=True,
            ) from None
        if diarize:
            return _speaker_transcript_from_output(output, response.status_code)
        text = output.get('text') if isinstance(output, dict) else None
        if not isinstance(text, str):
            raise TranscriptionProviderError(
                f'xAI transcription returned invalid response: missing or non-string text (HTTP {response.status_code})',
                response.status_code, retryable=True,
            )
        # A successful empty string means the provider recognized no speech.
        # Missing or non-string text remains an invalid response above.
        if not text.strip():
            print('   ℹ️ xAI returned HTTP 200 with no recognized speech')
            return ''
        return text


def _speaker_transcript_from_output(output, status_code: int) -> str:
    words = output.get('words') if isinstance(output, dict) else None
    if not isinstance(words, list):
        raise TranscriptionProviderError(
            f'xAI transcription returned invalid response: missing or non-list words (HTTP {status_code})',
            status_code, retryable=True,
        )
    text = output.get('text') if isinstance(output, dict) else None
    if not words:
        if not isinstance(text, str) or not text.strip():
            print('   ℹ️ xAI returned HTTP 200 with no recognized speech')
            return ''
        raise TranscriptionProviderError(
            f'xAI transcription returned invalid response: words missing speaker (HTTP {status_code})',
            status_code, retryable=False,
        )
    if any(not isinstance(word, dict) or 'speaker' not in word for word in words):
        raise TranscriptionProviderError(
            f'xAI transcription returned invalid response: words missing speaker (HTTP {status_code})',
            status_code, retryable=False,
        )
    return render_speaker_transcript(stitch_short_intrusions(turns_from_words(words)))
