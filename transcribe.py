import os
from openai import OpenAI
import io

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

def transcribe_audio(audio_data: bytes, filename: str) -> dict:
    """
    Transcribe audio using OpenAI Whisper
    """
    # Create file-like object
    audio_file = io.BytesIO(audio_data)
    audio_file.name = filename

    # Call OpenAI Whisper
    transcript = client.audio.transcriptions.create(
        model="whisper-1",
        file=audio_file
    )

    # Calculate duration (simplified for MVP)
    duration = len(audio_data) / 32000  # Rough estimate

    return {
        "transcript": transcript.text,
        "duration": duration
    }
