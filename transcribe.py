import os
import io
from typing import Callable, Optional, List
from openai import OpenAI
from pydub import AudioSegment

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Constants matching iOS implementation
MAX_CHUNK_SIZE_MB = 1.5
MAX_CHUNK_SIZE_BYTES = int(MAX_CHUNK_SIZE_MB * 1024 * 1024)
OVERLAP_SECONDS = 2000  # 2 seconds in milliseconds for pydub


def chunk_audio(audio_bytes: bytes, filename: str, progress_callback: Optional[Callable] = None) -> List[bytes]:
    """
    Split audio into chunks of MAX_CHUNK_SIZE_MB with overlap using PyDub

    This properly splits audio at frame boundaries preserving file structure.

    Args:
        audio_bytes: Raw audio file bytes
        filename: Original filename (for format detection)
        progress_callback: Optional callback(progress_pct: float, stage: str)

    Returns:
        List of audio chunk bytes
    """
    # Load audio file
    audio_file = io.BytesIO(audio_bytes)

    # Detect format from filename extension
    file_format = filename.split('.')[-1].lower()
    if file_format not in ['mp3', 'm4a', 'wav', 'ogg', 'flac']:
        file_format = 'm4a'  # Default to m4a

    if progress_callback:
        progress_callback(0, "Loading audio file...")

    # Load audio using pydub
    audio = AudioSegment.from_file(audio_file, format=file_format)

    # Calculate total duration and bitrate
    duration_ms = len(audio)
    file_size_bytes = len(audio_bytes)

    # Estimate chunk duration based on file size and duration
    # We want chunks around MAX_CHUNK_SIZE_BYTES
    avg_bytes_per_ms = file_size_bytes / duration_ms
    target_chunk_duration_ms = int(MAX_CHUNK_SIZE_BYTES / avg_bytes_per_ms)

    # Ensure minimum chunk duration of 60 seconds (like iOS)
    MIN_CHUNK_DURATION_MS = 60 * 1000
    chunk_duration_ms = max(target_chunk_duration_ms, MIN_CHUNK_DURATION_MS)

    chunks = []
    current_pos_ms = 0
    chunk_index = 0

    # Estimate total chunks for progress tracking
    estimated_chunks = max(1, int(duration_ms / chunk_duration_ms))

    if progress_callback:
        progress_callback(5, f"Splitting audio into {estimated_chunks} chunk(s)...")

    while current_pos_ms < duration_ms:
        # Calculate chunk boundaries
        end_pos_ms = min(current_pos_ms + chunk_duration_ms, duration_ms)

        # Add overlap to the end (except for the last chunk)
        chunk_end_with_overlap = min(end_pos_ms + OVERLAP_SECONDS, duration_ms)

        # Extract chunk
        chunk_audio = audio[current_pos_ms:chunk_end_with_overlap]

        # Export chunk to bytes
        chunk_buffer = io.BytesIO()
        chunk_audio.export(chunk_buffer, format="mp3", bitrate="64k")  # Compress to reduce size
        chunk_bytes = chunk_buffer.getvalue()

        chunks.append(chunk_bytes)

        # Report progress
        if progress_callback:
            progress_pct = ((chunk_index + 1) / estimated_chunks) * 100
            progress_callback(
                min(progress_pct, 100),
                f"Created chunk {chunk_index + 1}/{estimated_chunks}"
            )

        # Move to next chunk (without overlap to avoid duplication)
        current_pos_ms = end_pos_ms
        chunk_index += 1

    print(f"✅ Split audio into {len(chunks)} chunk(s)")
    return chunks


def merge_transcripts(transcripts: List[str]) -> str:
    """
    Merge chunk transcripts with overlap handling

    Args:
        transcripts: List of transcript strings from chunks

    Returns:
        Combined transcript
    """
    if not transcripts:
        return ""

    # Find first non-empty transcript
    filtered = [t.strip() for t in transcripts if t.strip()]
    if not filtered:
        return ""

    merged = filtered[0]

    # Merge remaining transcripts with overlap detection
    for i in range(1, len(filtered)):
        next_transcript = filtered[i]

        # Try to find overlap (simple approach: check last 200 chars)
        overlap_found = False
        max_overlap_chars = min(200, len(merged), len(next_transcript))

        for overlap_len in range(max_overlap_chars, 20, -1):
            suffix = merged[-overlap_len:].lower()
            prefix = next_transcript[:overlap_len].lower()

            if suffix == prefix:
                # Found overlap, merge without duplication
                merged += next_transcript[overlap_len:]
                overlap_found = True
                print(f"   ✂️  Detected {overlap_len} char overlap between chunks {i} and {i+1}")
                break

        if not overlap_found:
            # No overlap found, just append with space
            merged += " " + next_transcript

    return merged


def transcribe_audio(
    audio_data: bytes,
    filename: str,
    progress_callback: Optional[Callable] = None,
    language: Optional[str] = None
) -> dict:
    """
    Transcribe audio using OpenAI gpt-4o-transcribe with automatic chunking for large files

    Args:
        audio_data: Raw audio file bytes
        filename: Original filename
        progress_callback: Optional callback(progress_pct: float, stage: str)
        language: ISO-639-1 language code (e.g., "en", "it"). None for auto-detect

    Returns:
        Dict with 'transcript' and 'duration' keys
    """
    file_size_bytes = len(audio_data)

    # Check if chunking is needed
    if file_size_bytes <= MAX_CHUNK_SIZE_BYTES:
        # Small file - direct transcription (fast path)
        print(f"📄 File size: {file_size_bytes / 1024 / 1024:.2f} MB - using direct transcription")

        if progress_callback:
            progress_callback(0, "Transcribing audio...")

        audio_file = io.BytesIO(audio_data)
        audio_file.name = filename

        # Build API kwargs - only include language if specified
        api_kwargs = {
            "model": "gpt-4o-transcribe",
            "file": audio_file
        }
        if language:
            api_kwargs["language"] = language

        transcript_response = client.audio.transcriptions.create(**api_kwargs)

        # Calculate duration (rough estimate)
        duration = len(audio_data) / 32000

        if progress_callback:
            progress_callback(100, "Transcription complete")

        return {
            "transcript": transcript_response.text,
            "duration": duration
        }

    else:
        # Large file - use chunking
        print(f"📦 File size: {file_size_bytes / 1024 / 1024:.2f} MB - using chunked transcription")

        # Split into chunks (progress: 0-10%)
        def chunk_progress(pct, stage):
            if progress_callback:
                progress_callback(pct * 0.1, stage)

        chunks = chunk_audio(audio_data, filename, chunk_progress)

        # Transcribe each chunk (progress: 10-90%)
        transcripts = []
        total_chunks = len(chunks)

        for i, chunk_bytes in enumerate(chunks):
            chunk_num = i + 1

            if progress_callback:
                base_progress = 10 + ((i / total_chunks) * 80)
                progress_callback(base_progress, f"Transcribing chunk {chunk_num}/{total_chunks}...")

            print(f"   🎤 Transcribing chunk {chunk_num}/{total_chunks} ({len(chunk_bytes) / 1024 / 1024:.2f} MB)")

            # Create file-like object for this chunk
            chunk_file = io.BytesIO(chunk_bytes)
            chunk_file.name = f"chunk_{chunk_num}.mp3"

            # Transcribe chunk
            try:
                # Build API kwargs - only include language if specified
                api_kwargs = {
                    "model": "gpt-4o-transcribe",
                    "file": chunk_file
                }
                if language:
                    api_kwargs["language"] = language

                transcript_response = client.audio.transcriptions.create(**api_kwargs)
                transcripts.append(transcript_response.text)
                print(f"   ✅ Chunk {chunk_num} transcribed: {len(transcript_response.text)} chars")
            except Exception as e:
                print(f"   ❌ Chunk {chunk_num} failed: {e}")
                raise  # Fail completely if any chunk fails

        # Merge transcripts (progress: 90-100%)
        if progress_callback:
            progress_callback(90, "Combining transcripts...")

        full_transcript = merge_transcripts(transcripts)

        # Calculate duration estimate
        duration = len(audio_data) / 32000

        if progress_callback:
            progress_callback(100, "Transcription complete")

        print(f"✅ Full transcript: {len(full_transcript)} chars from {total_chunks} chunks")

        return {
            "transcript": full_transcript,
            "duration": duration
        }
