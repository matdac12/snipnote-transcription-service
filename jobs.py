import httpx
import io
from typing import List, Dict, Any
from supabase_client import supabase, update_job_status
from transcribe import transcribe_audio


def get_pending_jobs() -> List[Dict[str, Any]]:
    """
    Query Supabase for all jobs with status='pending'

    Returns:
        List of pending job dictionaries
    """
    try:
        response = supabase.table("transcription_jobs").select("*").eq("status", "pending").execute()

        if response.data:
            print(f"📋 Found {len(response.data)} pending job(s)")
            return response.data
        else:
            print("✨ No pending jobs")
            return []
    except Exception as e:
        print(f"❌ Error fetching pending jobs: {e}")
        return []


def download_audio(audio_url: str) -> bytes:
    """
    Download audio file from URL

    Args:
        audio_url: URL to audio file (Supabase Storage or public URL)

    Returns:
        Audio file bytes

    Raises:
        Exception: If download fails
    """
    print(f"   📥 Downloading audio from {audio_url[:50]}...")

    response = httpx.get(audio_url, timeout=120.0, follow_redirects=True)
    response.raise_for_status()

    print(f"   ✅ Downloaded {len(response.content)} bytes")
    return response.content


def process_job(job: Dict[str, Any]):
    """
    Process a single transcription job

    Args:
        job: Job dictionary from Supabase
    """
    job_id = job["id"]
    audio_url = job["audio_url"]

    try:
        # Step 1: Update status to 'processing'
        print(f"   ⚙️  Updating status to 'processing'...")
        update_job_status(job_id, "processing")

        # Step 2: Download audio
        audio_data = download_audio(audio_url)

        # Step 3: Transcribe using OpenAI Whisper
        print(f"   🎤 Transcribing audio...")
        result = transcribe_audio(audio_data, "audio.m4a")

        transcript = result["transcript"]
        duration = result["duration"]

        print(f"   ✅ Transcription complete: {len(transcript)} chars, {duration:.1f}s")

        # Step 4: Update job with transcript and status='completed'
        print(f"   💾 Saving transcript to database...")
        update_job_status(
            job_id=job_id,
            status="completed",
            transcript=transcript,
            duration=duration
        )

        print(f"✅ Job {job_id} completed successfully!")

    except Exception as e:
        # Error handling: update job status to 'failed' with error message
        error_message = str(e)
        print(f"❌ Job {job_id} failed: {error_message}")

        try:
            update_job_status(
                job_id=job_id,
                status="failed",
                error=error_message
            )
            print(f"   💾 Error saved to database")
        except Exception as update_error:
            print(f"   ⚠️  Failed to update job status: {update_error}")


def process_pending_jobs():
    """
    Main function to process all pending transcription jobs

    For each pending job:
    1. Update status to 'processing'
    2. Download audio from audio_url
    3. Transcribe using OpenAI Whisper
    4. Update job with transcript and status='completed'
    5. Handle errors by marking job as 'failed'
    """
    pending_jobs = get_pending_jobs()

    if not pending_jobs:
        return

    for job in pending_jobs:
        job_id = job["id"]
        print(f"\n🔄 Processing job {job_id}...")
        process_job(job)


if __name__ == "__main__":
    print("🚀 Starting job processor...")
    process_pending_jobs()
    print("✅ Job processor finished")
