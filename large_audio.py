"""Memory-safe transcription of ONE large audio file (1-2 h) with per-chunk providers.

The legacy regular path reads the whole file into RAM and decodes it with pydub
(a 1 h stereo file decodes to ~600 MB plus copies), which does not fit the worker's
MemoryMax=1500M. This module instead:

  1. splits the on-disk file with a single streaming `ffmpeg -f segment` pass into
     ~5 minute mono 16 kHz MP3 segments (nothing is decoded in Python),
  2. gives every segment except the last a small overlap by appending the first
     few seconds of the next segment (tiny per-segment ffmpeg run), so a word cut
     at a boundary is heard whole in one of the two neighbours,
  3. transcribes each segment with the existing per-chunk function (same provider
     call, retries and model config as every other path) and
  4. merges with the existing overlap-aware `transcribe.merge_transcripts`.

It is only used for files above a threshold (see should_segment), so jobs that
come through the existing paths keep their exact current behaviour.

Env knobs (read at call time):
  LARGE_FILE_SEGMENTING_ENABLED  default true   kill switch (false = old in-memory path)
  LARGE_FILE_THRESHOLD_BYTES     default 15 MiB  larger files are segmented
                                 (the iOS app chunk-uploads anything above 15 MB, so
                                 legacy regular jobs never reach this)
  LARGE_FILE_THRESHOLD_SECONDS   default 1800    longer (job-reported) audio is segmented
  LARGE_FILE_SEGMENT_SECONDS     default 300
  LARGE_FILE_OVERLAP_SECONDS     default 2
  LARGE_FILE_BITRATE_KBPS        default 48      ~1.8 MB per 5 min, far below the 25 MB API cap
  LARGE_FILE_FFMPEG_TIMEOUT_SECONDS default 1800
  MAX_CHUNK_WORKERS              (shared with jobs.py) parallel segment requests
  XAI_WORK_DIR                   scratch dir (shared with xai_single; real disk, not tmpfs)
"""
import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Callable, List, Optional, Tuple

import transcribe
import xai_single

DEFAULT_THRESHOLD_BYTES = 15 * 1024 * 1024
DEFAULT_THRESHOLD_SECONDS = 1800.0
DEFAULT_SEGMENT_SECONDS = 300.0
DEFAULT_OVERLAP_SECONDS = 2.0
DEFAULT_BITRATE_KBPS = 48
DEFAULT_FFMPEG_TIMEOUT_SECONDS = 1800.0

Progress = Callable[[int, str], None]  # (0-100 within this step, stage text)


class SegmentationError(RuntimeError):
    pass


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, ''))
        return value if value > 0 else default
    except ValueError:
        return default


def segmenting_enabled() -> bool:
    return os.getenv('LARGE_FILE_SEGMENTING_ENABLED', 'true').strip().lower() not in ('0', 'false', 'no', 'off')


def should_segment(size_bytes: int, duration_seconds: Optional[float] = None) -> bool:
    if not segmenting_enabled():
        return False
    if size_bytes > _env_float('LARGE_FILE_THRESHOLD_BYTES', DEFAULT_THRESHOLD_BYTES):
        return True
    return bool(duration_seconds) and duration_seconds > _env_float('LARGE_FILE_THRESHOLD_SECONDS', DEFAULT_THRESHOLD_SECONDS)


@contextmanager
def temp_workdir():
    """Private scratch dir shared with the xAI path (same prefix => same stale-dir sweep)."""
    xai_single.sweep_stale_workdirs()
    with tempfile.TemporaryDirectory(prefix=xai_single.WORKDIR_PREFIX, dir=os.getenv('XAI_WORK_DIR') or None) as path:
        yield path


def _run_ffmpeg(cmd: List[str]) -> None:
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=_env_float('LARGE_FILE_FFMPEG_TIMEOUT_SECONDS', DEFAULT_FFMPEG_TIMEOUT_SECONDS),
        )
    except FileNotFoundError:
        raise SegmentationError('ffmpeg is not installed') from None
    except subprocess.TimeoutExpired:
        raise SegmentationError('ffmpeg timed out') from None
    if proc.returncode != 0:
        raise SegmentationError(f'ffmpeg failed (exit {proc.returncode}): {(proc.stderr or "")[-300:].strip()}')


def _encode_args(bitrate: int) -> List[str]:
    return ['-vn', '-map_metadata', '-1', '-ac', '1', '-ar', '16000', '-c:a', 'libmp3lame', '-b:a', f'{bitrate}k']


def segment_audio(src: str, out_dir: str) -> List[str]:
    """Split `src` into ordered segment files in `out_dir`; returns their paths.

    Pass 1 is a single streaming `ffmpeg -f segment` run. Pass 2 (best effort, per
    segment) appends the head of the next segment as overlap.
    """
    seg_seconds = _env_float('LARGE_FILE_SEGMENT_SECONDS', DEFAULT_SEGMENT_SECONDS)
    overlap = _env_float('LARGE_FILE_OVERLAP_SECONDS', DEFAULT_OVERLAP_SECONDS)
    bitrate = int(_env_float('LARGE_FILE_BITRATE_KBPS', DEFAULT_BITRATE_KBPS))

    pattern = os.path.join(out_dir, 'seg_%04d.mp3')
    _run_ffmpeg(
        ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-i', src, '-map', '0:a:0']
        + _encode_args(bitrate)
        + ['-f', 'segment', '-segment_time', str(int(seg_seconds)), '-reset_timestamps', '1', pattern]
    )
    segments = sorted(
        os.path.join(out_dir, name) for name in os.listdir(out_dir)
        if name.startswith('seg_') and name.endswith('.mp3') and '_ov' not in name
    )
    segments = [p for p in segments if os.path.getsize(p) > 0]
    if not segments:
        raise SegmentationError('ffmpeg produced no audio segments')

    # A trailing piece shorter than the overlap is fully contained in the previous
    # segment's overlap tail; transcribing it alone would duplicate it (and providers
    # reject very short audio), so it feeds the overlap but is not transcribed itself.
    min_bytes = overlap * bitrate * 1000 / 8 * 0.9
    drop_last = len(segments) > 1 and overlap > 0 and os.path.getsize(segments[-1]) < min_bytes

    if overlap <= 0:
        return segments
    result: List[str] = []
    for index, path in enumerate(segments):
        if index == len(segments) - 1:
            result.append(path)
            continue
        extended = path[:-4] + '_ov.mp3'
        try:
            _run_ffmpeg(
                ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
                 '-i', path, '-t', str(overlap), '-i', segments[index + 1],
                 '-filter_complex', '[0:a][1:a]concat=n=2:v=0:a=1[out]', '-map', '[out]']
                + _encode_args(bitrate) + [extended]
            )
            os.remove(path)
            result.append(extended)
        except SegmentationError as error:
            print(f'   ⚠️ overlap for segment {index + 1} failed, using it without overlap: {error}')
            result.append(path)
    if drop_last:
        os.remove(result.pop())
    return result


def transcribe_segments(paths: List[str], language: Optional[str], provider: str,
                        progress: Optional[Progress] = None, workers: int = 2) -> str:
    """Transcribe ordered segment files (bounded parallelism) and merge with overlap handling."""
    total = len(paths)
    done = 0

    def run(item: Tuple[int, str]) -> str:
        index, path = item
        with open(path, 'rb') as f:
            data = f.read()  # ~2 MB
        print(f'   🎤 Transcribing segment {index + 1}/{total} ({len(data) / 1024 / 1024:.2f} MB)')
        return transcribe.transcribe_chunk_with_retry(data, f'segment_{index + 1}.mp3', language, provider)

    transcripts: List[str] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        # map() yields in input order, so the merge order is always the audio order.
        for text in executor.map(run, list(enumerate(paths))):
            transcripts.append(text)
            done += 1
            if progress:
                progress(int(done / total * 100), f'Transcribed {done}/{total} segments...')
    return transcribe.merge_transcripts(transcripts)


def transcribe_large_file(
    src: str, workdir: str, language: Optional[str], provider: str,
    progress: Optional[Progress] = None, duration_hint: Optional[float] = None, workers: int = 2,
) -> Tuple[str, float]:
    """Segment + transcribe + merge one big on-disk file. Returns (transcript, duration_seconds)."""
    def report(pct: int, stage: str) -> None:
        if progress:
            progress(pct, stage)

    report(0, 'Splitting audio...')
    seg_dir = os.path.join(workdir, 'segments')
    os.makedirs(seg_dir, exist_ok=True)
    segments = segment_audio(src, seg_dir)
    duration = xai_single.probe_duration(src) or duration_hint or 0.0
    print(f'   ✂️ Split into {len(segments)} segment(s), duration={duration:.0f}s')
    report(10, f'Transcribing {len(segments)} segments...')
    text = transcribe_segments(
        segments, language, provider, workers=workers,
        progress=lambda pct, stage: report(10 + int(pct * 0.9), stage),
    )
    report(100, 'Transcription complete')
    return text, duration or len(segments) * _env_float('LARGE_FILE_SEGMENT_SECONDS', DEFAULT_SEGMENT_SECONDS)
