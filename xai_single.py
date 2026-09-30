"""xAI whole-file transcription: ONE request instead of two-level chunking.

Flow (all on disk, nothing decoded in RAM, safe for MemoryMax=1500M):
  1. stream the audio (one file, or the iOS upload chunks in order) into a
     private temp dir,
  2. ffmpeg normalises/concatenates to a single compact mono 16 kHz MP3,
  3. one multipart POST to xAI (body streamed from disk),
  4. temp dir is always removed.

`run_single_request` returns None whenever the caller should use the existing
chunked path instead (kill switch off, file too big, ffmpeg problem, request
failed with a non-auth error after retries). Auth errors (401/403) and a
missing key are raised: chunking cannot fix them.

Env knobs (read at call time):
  XAI_SINGLE_REQUEST_ENABLED      default true   kill switch (false = old path)
  XAI_SINGLE_REQUEST_MAX_BYTES    default 100 MiB  cap on the file we upload
  XAI_STT_TIMEOUT_SECONDS         default 900    read/write timeout of the request
  XAI_SINGLE_REQUEST_MAX_ATTEMPTS default 3      attempts on transient errors
  XAI_AUDIO_BITRATE_KBPS          default 48     mono 16 kHz MP3 bitrate
  XAI_FFMPEG_TIMEOUT_SECONDS      default 1800   cap on the ffmpeg run
  XAI_WORK_DIR                    default system temp dir (must be a real disk,
                                  not tmpfs: tmpfs is charged to MemoryMax)
"""
import os
import shutil
import subprocess
import tempfile
import time
from typing import Callable, List, Optional

from transcription_provider import (
    TranscriptionProviderError, TranscriptionResult, is_auth_provider_error,
    require_xai_key, transcribe_xai_file,
)

DEFAULT_MAX_BYTES = 100 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 900.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BITRATE_KBPS = 48
DEFAULT_FFMPEG_TIMEOUT_SECONDS = 1800.0

Fetcher = Callable[[str], None]  # writes one audio file to the given path
Progress = Callable[[int, str], None]  # (0-100 within this step, stage text)


class AudioPreparationError(RuntimeError):
    pass


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, ''))
        return value if value > 0 else default
    except ValueError:
        return default


def single_request_enabled() -> bool:
    return os.getenv('XAI_SINGLE_REQUEST_ENABLED', 'true').strip().lower() not in ('0', 'false', 'no', 'off')


def should_use_single_request(provider: str) -> bool:
    return provider == 'xai' and single_request_enabled()


def prepare_audio(inputs: List[str], output_path: str) -> None:
    """Concatenate `inputs` (in order) into one mono 16 kHz MP3 with ffmpeg.

    Why re-encode instead of `-c copy` concat: the iOS upload chunks are separately
    exported files (possibly different codecs/params, each with its own container
    headers), so byte-joining or the concat demuxer is unsafe. The concat *filter*
    decodes each part, and per-input aformat/aresample makes them uniform. Mono
    16 kHz at ~48 kbps is plenty for speech, keeps a 2 h meeting around 40 MB and
    is well below the upload cap; ffmpeg streams, so RAM stays small.
    """
    if not inputs:
        raise AudioPreparationError('No audio to prepare')
    bitrate = int(_env_float('XAI_AUDIO_BITRATE_KBPS', DEFAULT_BITRATE_KBPS))
    cmd = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y']
    for path in inputs:
        cmd += ['-i', path]
    normalise = 'aresample=16000,aformat=sample_fmts=fltp:channel_layouts=mono'
    if len(inputs) == 1:
        cmd += ['-map', '0:a:0', '-af', normalise]
    else:
        graph = ''.join(f'[{i}:a:0]{normalise}[a{i}];' for i in range(len(inputs)))
        graph += ''.join(f'[a{i}]' for i in range(len(inputs)))
        graph += f'concat=n={len(inputs)}:v=0:a=1[out]'
        cmd += ['-filter_complex', graph, '-map', '[out]']
    cmd += ['-vn', '-map_metadata', '-1', '-c:a', 'libmp3lame', '-b:a', f'{bitrate}k', '-ar', '16000', '-ac', '1', output_path]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=_env_float('XAI_FFMPEG_TIMEOUT_SECONDS', DEFAULT_FFMPEG_TIMEOUT_SECONDS),
        )
    except FileNotFoundError:
        raise AudioPreparationError('ffmpeg is not installed') from None
    except subprocess.TimeoutExpired:
        raise AudioPreparationError('ffmpeg timed out') from None
    if proc.returncode != 0 or not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        raise AudioPreparationError(f'ffmpeg failed (exit {proc.returncode}): {(proc.stderr or "")[-300:].strip()}')


def probe_duration(path: str) -> Optional[float]:
    try:
        proc = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=nw=1:nk=1', path],
            capture_output=True, text=True, timeout=60,
        )
        value = float(proc.stdout.strip())
        return value if proc.returncode == 0 and value > 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


STALE_WORKDIR_SECONDS = 6 * 3600
WORKDIR_PREFIX = 'snipnote-xai-'


def sweep_stale_workdirs(root: Optional[str] = None) -> None:
    """Remove leftovers of jobs killed mid-run (OOM/SIGKILL skips the normal cleanup)."""
    root = root or os.getenv('XAI_WORK_DIR') or tempfile.gettempdir()
    try:
        for name in os.listdir(root):
            path = os.path.join(root, name)
            if name.startswith(WORKDIR_PREFIX) and os.path.isdir(path) \
                    and time.time() - os.path.getmtime(path) > STALE_WORKDIR_SECONDS:
                shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def run_single_request(
    fetchers: List[Fetcher], language: Optional[str] = None, progress: Optional[Progress] = None,
) -> Optional[TranscriptionResult]:
    """Transcribe everything in one xAI request, or return None to use chunking.

    Raises for: missing key, 401/403, and download failures (those are ordinary job
    errors handled by the worker's retry logic; re-downloading in the chunked path
    would not help).
    """
    def report(pct: int, stage: str) -> None:
        if progress:
            progress(pct, stage)

    require_xai_key()
    sweep_stale_workdirs()
    with tempfile.TemporaryDirectory(prefix=WORKDIR_PREFIX, dir=os.getenv('XAI_WORK_DIR') or None) as workdir:
        report(0, 'Downloading audio...')
        inputs = []
        source_bytes = 0
        for index, fetch in enumerate(fetchers):
            dest = os.path.join(workdir, f'part_{index:04d}')
            fetch(dest)
            source_bytes += os.path.getsize(dest)
            inputs.append(dest)

        report(30, 'Preparing audio...')
        prepared = os.path.join(workdir, 'audio.mp3')
        try:
            prepare_audio(inputs, prepared)
        except AudioPreparationError as error:
            print(f'   ⚠️ xAI single request: audio preparation failed, using chunked path: {error}')
            return None
        for path in inputs:  # free disk before the long upload
            os.remove(path)

        max_bytes = int(_env_float('XAI_SINGLE_REQUEST_MAX_BYTES', DEFAULT_MAX_BYTES))
        size = os.path.getsize(prepared)
        if size > max_bytes:
            print(f'   ⚠️ xAI single request: prepared audio {size / 1024 / 1024:.1f} MB exceeds '
                  f'{max_bytes / 1024 / 1024:.0f} MB limit, using chunked path')
            return None
        duration = probe_duration(prepared)

        report(45, 'Transcribing audio...')
        print(f'   🎤 xAI single request: {size / 1024 / 1024:.1f} MB, duration={duration}')
        try:
            result = transcribe_xai_file(
                prepared, language,
                timeout=_env_float('XAI_STT_TIMEOUT_SECONDS', DEFAULT_TIMEOUT_SECONDS),
                max_attempts=int(_env_float('XAI_SINGLE_REQUEST_MAX_ATTEMPTS', DEFAULT_MAX_ATTEMPTS)),
                filename='audio.mp3',
            )
        except TranscriptionProviderError as error:
            if is_auth_provider_error(error):
                raise
            print(f'   ⚠️ xAI single request failed ({error}), using chunked path')
            return None

        result.duration = duration or result.duration or source_bytes / 32000
        report(100, 'Transcription complete')
        return result
