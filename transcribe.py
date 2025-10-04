import os
import io
from typing import Callable, Optional, List
from openai import OpenAI

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Constants matching iOS implementation
MAX_CHUNK_SIZE_MB = 1.5
MAX_CHUNK_SIZE_BYTES = int(MAX_CHUNK_SIZE_MB * 1024 * 1024)


def chunk_audio_simple(audio_bytes: bytes, progress_callback: Optional[Callable] = None) -> List[bytes]:
    """
    Split audio into chunks of MAX_CHUNK_SIZE_MB using simple binary splitting

    This is a simple approach that doesn't require audio processing libraries.
    Works well with Whisper API since it can handle partial audio segments.

    Args:
        audio_bytes: Raw audio file bytes
        progress_callback: Optional callback(progress_pct: float, stage: str)

    Returns:
        List of audio chunk bytes
    """
    total_size = len(audio_bytes)

    # If file is small enough, return as single chunk
    if total_size <= MAX_CHUNK_SIZE_BYTES:
        if progress_callback:
            progress_callback(100, "Audio ready")
        return [audio_bytes]

    # Calculate number of chunks needed
    num_chunks = (total_size + MAX_CHUNK_SIZE_BYTES - 1) // MAX_CHUNK_SIZE_BYTES

    if progress_callback:
        progress_callback(0, f"Splitting audio into {num_chunks} chunk(s)...")

    chunks = []
    for i in range(num_chunks):
        start_byte = i * MAX_CHUNK_SIZE_BYTES
        end_byte = min((i + 1) * MAX_CHUNK_SIZE_BYTES, total_size)

        chunk = audio_bytes[start_byte:end_byte]
        chunks.append(chunk)

        if progress_callback:
            progress_pct = ((i + 1) / num_chunks) * 100
            progress_callback(progress_pct, f"Created chunk {i + 1}/{num_chunks}")

    print(f"✅ Split {total_size / 1024 / 1024:.2f}MB audio into {len(chunks)} chunk(s)")
    return chunks


def merge_transcripts(transcripts: List[str]) -> str:
    """
    Merge chunk transcripts with simple concatenation

    Since we're using binary chunking, chunks may cut mid-word,
    but Whisper is robust enough to handle this and we just concatenate.

    Args:
        transcripts: List of transcript strings from chunks

    Returns:
        Combined transcript
    """
    if not transcripts:
        return ""

    # Filter out empty transcripts
    filtered = [t.strip() for t in transcripts if t.strip()]
    if not filtered:
        return ""

    # Simple concatenation with spaces
    merged = " ".join(filtered)

    return merged


def transcribe_audio(
    audio_data: bytes,
    filename: str,
    progress_callback: Optional[Callable] = None
) -> dict:
    """
    Transcribe audio using OpenAI Whisper with automatic chunking for large files

    Args:
        audio_data: Raw audio file bytes
        filename: Original filename (for format detection)
        progress_callback: Optional callback(progress_pct: float, stage: str)

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

        transcript_response = client.audio.transcriptions.create(
            model="gpt-4o-transcribe",
            file=audio_file
        )

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

        chunks = chunk_audio_simple(audio_data, chunk_progress)

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
            chunk_file.name = filename  # Keep original filename/extension

            # Transcribe chunk
            try:
                transcript_response = client.audio.transcriptions.create(
                    model="gpt-4o-transcribe",
                    file=chunk_file
                )
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
