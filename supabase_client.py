import os
from supabase import create_client, Client
from typing import Optional, Dict, Any
from datetime import datetime

# Initialize Supabase client
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    raise ValueError(
        "Missing required environment variables: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set"
    )

# Create Supabase client with service role key (bypasses RLS)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

print(f"✅ Supabase client initialized for: {SUPABASE_URL}")


def create_job(user_id: str, meeting_id: str, audio_url: str) -> Dict[str, Any]:
    """
    Create a new transcription job with status='pending'

    Args:
        user_id: UUID of the user creating the job
        meeting_id: UUID of the meeting to transcribe
        audio_url: URL to the audio file (Supabase Storage or public URL)

    Returns:
        Dict containing the created job data including job_id

    Raises:
        Exception: If job creation fails
    """
    try:
        data = {
            "user_id": user_id,
            "meeting_id": meeting_id,
            "audio_url": audio_url,
            "status": "pending"
        }

        response = supabase.table("transcription_jobs").insert(data).execute()

        if response.data and len(response.data) > 0:
            job = response.data[0]
            print(f"✅ Created job {job['id']} for user {user_id}")
            return job
        else:
            raise Exception("Failed to create job: No data returned")

    except Exception as e:
        print(f"❌ Error creating job: {e}")
        raise


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    """
    Retrieve a transcription job by ID

    Args:
        job_id: UUID of the job to retrieve

    Returns:
        Dict containing job data or None if not found

    Raises:
        Exception: If query fails
    """
    try:
        response = supabase.table("transcription_jobs").select("*").eq("id", job_id).execute()

        if response.data and len(response.data) > 0:
            return response.data[0]
        else:
            print(f"⚠️ Job {job_id} not found")
            return None

    except Exception as e:
        print(f"❌ Error retrieving job {job_id}: {e}")
        raise


def update_job_status(
    job_id: str,
    status: str,
    transcript: Optional[str] = None,
    duration: Optional[float] = None,
    error: Optional[str] = None
) -> Dict[str, Any]:
    """
    Update job status and related fields

    Args:
        job_id: UUID of the job to update
        status: New status ('pending', 'processing', 'completed', 'failed')
        transcript: Transcription text (for completed jobs)
        duration: Audio duration in seconds (for completed jobs)
        error: Error message (for failed jobs)

    Returns:
        Dict containing updated job data

    Raises:
        Exception: If update fails
    """
    try:
        update_data: Dict[str, Any] = {"status": status}

        if transcript is not None:
            update_data["transcript"] = transcript

        if duration is not None:
            update_data["duration"] = duration

        if error is not None:
            update_data["error_message"] = error

        # Set completed_at timestamp when job completes
        if status == "completed":
            update_data["completed_at"] = datetime.utcnow().isoformat()

        response = supabase.table("transcription_jobs").update(update_data).eq("id", job_id).execute()

        if response.data and len(response.data) > 0:
            job = response.data[0]
            print(f"✅ Updated job {job_id} to status: {status}")
            return job
        else:
            raise Exception(f"Failed to update job {job_id}: No data returned")

    except Exception as e:
        print(f"❌ Error updating job {job_id}: {e}")
        raise


def update_job_progress(
    job_id: str,
    progress: int,
    stage: str
) -> Dict[str, Any]:
    """
    Update job progress and current stage

    Args:
        job_id: UUID of the job to update
        progress: Progress percentage (0-100)
        stage: Human-readable stage description

    Returns:
        Dict containing updated job data

    Raises:
        Exception: If update fails
    """
    try:
        update_data = {
            "progress_percentage": progress,
            "current_stage": stage
        }

        response = supabase.table("transcription_jobs").update(update_data).eq("id", job_id).execute()

        if response.data and len(response.data) > 0:
            return response.data[0]
        else:
            raise Exception(f"Failed to update job {job_id} progress: No data returned")

    except Exception as e:
        print(f"❌ Error updating job {job_id} progress: {e}")
        raise


def update_job_with_results(
    job_id: str,
    transcript: str,
    overview: str,
    summary: str,
    actions: list,
    duration: float
) -> Dict[str, Any]:
    """
    Update job with all AI-generated results

    Args:
        job_id: UUID of the job to update
        transcript: Full meeting transcript
        overview: 1-sentence overview
        summary: Comprehensive meeting summary
        actions: List of action items
        duration: Audio duration in seconds

    Returns:
        Dict containing updated job data

    Raises:
        Exception: If update fails
    """
    try:
        update_data = {
            "status": "completed",
            "transcript": transcript,
            "overview": overview,
            "summary": summary,
            "actions": actions,  # Supabase client handles JSONB conversion automatically
            "duration": duration,
            "progress_percentage": 100,  # Mark as 100% complete
            "current_stage": "Complete",
            "completed_at": datetime.utcnow().isoformat()
        }

        response = supabase.table("transcription_jobs").update(update_data).eq("id", job_id).execute()

        if response.data and len(response.data) > 0:
            job = response.data[0]
            print(f"✅ Updated job {job_id} with complete AI results")
            return job
        else:
            raise Exception(f"Failed to update job {job_id}: No data returned")

    except Exception as e:
        print(f"❌ Error updating job {job_id} with results: {e}")
        raise
