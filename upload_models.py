"""Strict wire contract: clients describe files, never storage paths or credentials."""
from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, model_validator

PLAIN_FILE_LIMIT = 15728640
SPEAKER_FILE_LIMIT = 524288000

class UploadFileRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    index: int = Field(ge=0)
    expected_bytes: int = Field(gt=0, le=SPEAKER_FILE_LIMIT)
    duration: float = Field(gt=0)
    extension: Literal['m4a', 'mp3', 'wav', 'ogg', 'flac']
    content_type: str
    @model_validator(mode='after')
    def media_pair(self):
        allowed = {'m4a': {'audio/mp4','audio/m4a','audio/x-m4a'}, 'mp3': {'audio/mp3','audio/mpeg'}, 'wav': {'audio/wav','audio/wave','audio/x-wav'}, 'ogg': {'audio/ogg'}, 'flac': {'audio/flac'}}
        if self.content_type not in allowed[self.extension]: raise ValueError('Unsupported audio MIME/extension pair')
        return self

class UploadBootstrapRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    meeting_id: UUID
    transcription_provider: Literal['openai', 'xai']
    language: str | None = Field(default=None, max_length=16)
    duration: float = Field(gt=0)
    files: list[UploadFileRequest] = Field(min_length=1, max_length=1000)
    speaker_labels: bool = False
    @model_validator(mode='after')
    def ordered(self):
        if [f.index for f in self.files] != list(range(len(self.files))): raise ValueError('File indexes must be consecutive from zero')
        if abs(sum(f.duration for f in self.files)-self.duration) > max(0.1, self.duration * 0.001): raise ValueError('File durations must cover the recording')
        if not self.speaker_labels and any(f.expected_bytes > PLAIN_FILE_LIMIT for f in self.files):
            raise ValueError('File exceeds 15728640 bytes')
        return self

class UploadFileResponse(BaseModel):
    index: int
    verified: bool
    upload_url: str | None = None
    method: str | None = None
    headers: dict[str,str] | None = None
    expires_at: datetime | None = None

class UploadSessionResponse(BaseModel):
    session_id: UUID
    status: Literal['awaiting_upload','queued','expired','cancelled']
    job_id: UUID | None = None
    upload_deadline: datetime
    files: list[UploadFileResponse]
