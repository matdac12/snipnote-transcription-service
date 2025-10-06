import httpx
import io
import json
import os
import asyncio
from typing import List, Dict, Any
from openai import OpenAI
from supabase_client import (
    supabase,
    update_job_status,
    update_job_with_results,
    update_job_progress,
    get_audio_chunks,
    update_chunk_transcript,
    update_chunks_processed
)
from transcribe import transcribe_audio

# Initialize OpenAI client
openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))


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


def generate_overview(summary: str) -> str:
    """Generate 1-sentence meeting overview using GPT-5-mini from summary"""
    print(f"   📝 Generating overview from summary...")

    prompt = f"""Identify the language spoken and always respond in the same language as the input.
Summarize this meeting summary in exactly one short, clear sentence. Capture the main topic and key outcome or focus of the meeting.

Examples:
- "Team discussed Q4 goals and assigned project leads for upcoming initiatives."
- "Budget review meeting where department heads presented spending proposals."
- "Weekly standup covering project progress and addressing technical blockers."

Meeting Summary: {summary}"""

    response = openai_client.chat.completions.create(
        model="gpt-5-mini",
        messages=[
            {"role": "system", "content": "You create concise one-sentence meeting overviews. Always respond with exactly one clear, informative sentence in the same language as the input transcript."},
            {"role": "user", "content": prompt}
        ],
        reasoning={"effort": "minimal"},
        verbosity="low"
    )

    overview = response.choices[0].message.content.strip()
    print(f"   ✅ Overview generated: {overview[:80]}...")
    return overview


def generate_summary(transcript: str) -> str:
    """Generate comprehensive meeting summary using GPT-5-mini"""
    print(f"   📄 Generating summary...")

    prompt = f"""Identify the language spoken and always respond in the same language as the input transcript.
Please create a comprehensive meeting summary from this transcript. Structure your response with the following sections:

## Key Discussion Points
- Main topics discussed
- Important insights shared

## Decisions Made
- Key decisions reached during the meeting
- Who is responsible for what

## Action Items
- Tasks assigned with responsible parties
- Deadlines mentioned

## Next Steps
- Follow-up actions
- Future meetings or milestones

Meeting Transcript: {transcript}"""

    response = openai_client.chat.completions.create(
        model="gpt-5-mini",
        messages=[
            {"role": "system", "content": "You are a professional meeting summarizer. Create structured, comprehensive summaries that capture key decisions, action items, and next steps. Always respond in the same language as the input transcript."},
            {"role": "user", "content": prompt}
        ],
        reasoning={"effort": "minimal"}
    )

    summary = response.choices[0].message.content
    print(f"   ✅ Summary generated ({len(summary)} chars)")
    return summary


def extract_actions(summary: str) -> list:
    """Extract action items from summary using GPT-5-mini"""
    print(f"   ✅ Extracting actions from summary...")

    prompt = f"""Identify the language spoken and always respond in the same language as the input.
Extract actionable items from this meeting summary. For each action item, provide:
1. A clear, concise action description
2. Priority level (HIGH, MED, LOW)

Return ONLY a JSON array with this exact format:
[{{"action": "action description", "priority": "HIGH|MED|LOW"}}]

If no actionable items exist, return an empty array: []

Meeting Summary: {summary}"""

    response = openai_client.chat.completions.create(
        model="gpt-5-mini",
        messages=[
            {"role": "system", "content": "You extract actionable items from text and return them as JSON. Be precise and only return valid JSON. Always use the same language as the input transcript for action descriptions."},
            {"role": "user", "content": prompt}
        ],
        reasoning={"effort": "minimal"}
    )

    try:
        actions_text = response.choices[0].message.content.strip()

        # Extract JSON from markdown code blocks if present
        if "```json" in actions_text:
            # Extract content between ```json and ```
            start = actions_text.find("```json") + 7
            end = actions_text.find("```", start)
            actions_text = actions_text[start:end].strip()
        elif "```" in actions_text:
            # Extract content between ``` and ```
            start = actions_text.find("```") + 3
            end = actions_text.find("```", start)
            actions_text = actions_text[start:end].strip()

        # Try to parse the JSON
        actions = json.loads(actions_text)

        # Validate it's a list
        if not isinstance(actions, list):
            print(f"   ⚠️  GPT returned non-list JSON: {type(actions)}, returning empty array")
            return []

        print(f"   ✅ Actions extracted: {len(actions)} items")
        return actions

    except json.JSONDecodeError as e:
        print(f"   ⚠️  Failed to parse actions JSON: {e}")
        print(f"   📝 GPT response was: {response.choices[0].message.content[:200]}")
        return []
    except Exception as e:
        print(f"   ⚠️  Unexpected error extracting actions: {e}")
        return []


def download_chunk_from_storage(chunk_file_path: str) -> bytes:
    """
    Download a single chunk from Supabase Storage

    Args:
        chunk_file_path: File path in storage (e.g., "userId/meetingId_chunk_0.m4a")

    Returns:
        Audio chunk bytes

    Raises:
        Exception: If download fails
    """
    try:
        print(f"   📥 Downloading chunk: {chunk_file_path}")

        # Download from Supabase Storage using service key
        response = supabase.storage.from_("recordings").download(chunk_file_path)

        print(f"   ✅ Downloaded chunk: {len(response)} bytes")
        return response

    except Exception as e:
        print(f"   ❌ Failed to download chunk {chunk_file_path}: {e}")
        raise


def process_chunked_job(job: Dict[str, Any]):
    """
    Process a chunked transcription job

    Args:
        job: Job dictionary from Supabase
    """
    job_id = job["id"]
    meeting_id = job["meeting_id"]
    total_chunks = job.get("total_chunks", 0)

    try:
        # Step 1: Update status to 'processing'
        print(f"   📦 Processing chunked job ({total_chunks} chunks)...")
        update_job_status(job_id, "processing")
        update_job_progress(job_id, 0, "Starting chunked transcription...")

        # Step 2: Fetch all audio chunks from database
        print(f"   📋 Fetching audio chunks from database...")
        update_job_progress(job_id, 5, "Fetching audio chunks...")
        chunks = get_audio_chunks(meeting_id)

        if not chunks:
            raise Exception(f"No audio chunks found for meeting {meeting_id}")

        if len(chunks) != total_chunks:
            print(f"   ⚠️ Expected {total_chunks} chunks, found {len(chunks)}")

        # Step 3: Process each chunk (5-70% total progress)
        transcripts = []
        progress_per_chunk = 65 / len(chunks)  # 65% of total progress for transcription

        for i, chunk in enumerate(chunks):
            chunk_id = chunk["id"]
            chunk_index = chunk["chunk_index"]
            file_path = chunk["file_path"]

            # Update progress
            current_progress = 5 + int((i / len(chunks)) * 65)
            update_job_progress(
                job_id,
                current_progress,
                f"Transcribing chunk {chunk_index + 1}/{len(chunks)}..."
            )

            # Download chunk from storage
            chunk_data = download_chunk_from_storage(file_path)

            # Transcribe chunk
            print(f"   🎤 Transcribing chunk {chunk_index + 1}/{len(chunks)}...")
            result = transcribe_audio(chunk_data, f"chunk_{chunk_index}.m4a")
            transcript = result["transcript"]

            # Save transcript to chunk
            update_chunk_transcript(chunk_id, transcript)

            # Add to transcripts list
            transcripts.append(transcript)

            # Update chunks_processed count
            update_chunks_processed(job_id, i + 1)

            print(f"   ✅ Chunk {chunk_index + 1}/{len(chunks)} transcribed ({len(transcript)} chars)")

        # Step 4: Merge transcripts (70%)
        print(f"   🔗 Merging {len(transcripts)} chunk transcripts...")
        update_job_progress(job_id, 70, "Merging transcripts...")
        full_transcript = "\n".join(transcripts)
        print(f"   ✅ Merged transcript: {len(full_transcript)} chars")

        # Step 5: Generate AI content (70-90%)

        # 5a: Summary (70-80%) - needs full transcript
        update_job_progress(job_id, 70, "Generating summary...")
        summary = generate_summary(full_transcript)
        update_job_progress(job_id, 80, "Summary generated")

        # 5b: Overview (80-85%) - from summary
        update_job_progress(job_id, 80, "Generating overview...")
        overview = generate_overview(summary)
        update_job_progress(job_id, 85, "Overview generated")

        # 5c: Actions (85-90%) - from summary
        update_job_progress(job_id, 85, "Extracting actions...")
        actions = extract_actions(summary)
        update_job_progress(job_id, 90, "Actions extracted")

        # Step 6: Save results (90-100%)
        print(f"   💾 Saving all results to database...")
        update_job_progress(job_id, 95, "Saving results...")

        # Use duration from job if available, otherwise calculate from chunks
        duration = job.get("duration")
        if not duration:
            duration = sum(chunk.get("duration_seconds", 0) for chunk in chunks)

        update_job_with_results(
            job_id=job_id,
            transcript=full_transcript,
            overview=overview,
            summary=summary,
            actions=actions,
            duration=duration
        )

        print(f"✅ Chunked job {job_id} completed successfully!")
        print(f"   - Chunks processed: {len(chunks)}")
        print(f"   - Total transcript: {len(full_transcript)} chars")
        print(f"   - Overview: {overview[:80]}...")
        print(f"   - Summary: {len(summary)} chars")
        print(f"   - Actions: {len(actions)} items")

    except Exception as e:
        error_message = str(e)
        print(f"❌ Chunked job {job_id} failed: {error_message}")

        try:
            update_job_status(job_id=job_id, status="failed", error=error_message)
            update_job_progress(job_id, 0, f"Failed: {error_message[:50]}...")
            print(f"   💾 Error saved to database")
        except Exception as update_error:
            print(f"   ⚠️  Failed to update job status: {update_error}")


def process_job(job: Dict[str, Any]):
    """
    Process a single transcription job with full AI pipeline and progress tracking

    Routes to chunked or regular processing based on job type.

    Args:
        job: Job dictionary from Supabase
    """
    job_id = job["id"]
    is_chunked = job.get("is_chunked", False)

    # Route to appropriate handler
    if is_chunked:
        print(f"   🔀 Routing to chunked job processor...")
        process_chunked_job(job)
        return

    # Regular (non-chunked) job processing
    audio_url = job["audio_url"]

    try:
        # Step 1: Update status to 'processing' and set initial progress
        print(f"   ⚙️  Updating status to 'processing'...")
        update_job_status(job_id, "processing")
        update_job_progress(job_id, 0, "Starting job...")

        # Step 2: Download audio (0-10%)
        print(f"   📥 Downloading audio...")
        update_job_progress(job_id, 5, "Downloading audio...")
        audio_data = download_audio(audio_url)
        update_job_progress(job_id, 10, "Audio downloaded")

        # Step 3: Transcribe using OpenAI Whisper (10-60%)
        print(f"   🎤 Transcribing audio...")

        def transcription_progress(pct: float, stage: str):
            """Callback to report transcription progress (maps 0-100 to 10-60)"""
            adjusted_pct = 10 + int(pct * 0.5)  # Scale to 10-60% range
            update_job_progress(job_id, adjusted_pct, stage)

        result = transcribe_audio(
            audio_data,
            "audio.m4a",
            progress_callback=transcription_progress
        )

        transcript = result["transcript"]
        duration = result["duration"]

        print(f"   ✅ Transcription complete: {len(transcript)} chars, {duration:.1f}s")

        # Step 4: Generate AI content (60-90%)

        # 4a: Summary (60-75%) - needs full transcript
        update_job_progress(job_id, 60, "Generating summary...")
        summary = generate_summary(transcript)
        update_job_progress(job_id, 75, "Summary generated")

        # 4b: Overview (75-82%) - from summary
        update_job_progress(job_id, 75, "Generating overview...")
        overview = generate_overview(summary)
        update_job_progress(job_id, 82, "Overview generated")

        # 4c: Actions (82-90%) - from summary
        update_job_progress(job_id, 82, "Extracting actions...")
        actions = extract_actions(summary)
        update_job_progress(job_id, 90, "Actions extracted")

        # Step 5: Update job with all results and status='completed' (90-100%)
        print(f"   💾 Saving all results to database...")
        update_job_progress(job_id, 95, "Saving results...")

        update_job_with_results(
            job_id=job_id,
            transcript=transcript,
            overview=overview,
            summary=summary,
            actions=actions,
            duration=duration
        )
        # update_job_with_results automatically sets progress to 100% and stage to "Complete"

        print(f"✅ Job {job_id} completed successfully!")
        print(f"   - Transcript: {len(transcript)} chars")
        print(f"   - Overview: {overview[:80]}...")
        print(f"   - Summary: {len(summary)} chars")
        print(f"   - Actions: {len(actions)} items")

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
            update_job_progress(job_id, 0, f"Failed: {error_message[:50]}...")
            print(f"   💾 Error saved to database")
        except Exception as update_error:
            print(f"   ⚠️  Failed to update job status: {update_error}")


async def process_pending_jobs(max_concurrent: int = 3):
    """
    Main function to process all pending transcription jobs in parallel

    Args:
        max_concurrent: Maximum number of jobs to process concurrently (default 3)

    For each pending job:
    1. Update status to 'processing'
    2. Download audio from audio_url
    3. Transcribe using OpenAI Whisper
    4. Update job with transcript and status='completed'
    5. Handle errors by marking job as 'failed'

    Jobs are processed in parallel up to max_concurrent limit for better performance.
    """
    pending_jobs = get_pending_jobs()

    if not pending_jobs:
        return

    print(f"📊 Found {len(pending_jobs)} pending job(s), processing up to {max_concurrent} concurrently")

    # Process jobs in batches of max_concurrent
    for i in range(0, len(pending_jobs), max_concurrent):
        batch = pending_jobs[i:i + max_concurrent]

        print(f"\n🔄 Processing batch of {len(batch)} job(s)...")

        # Process batch in parallel using asyncio.gather
        tasks = [process_job_async(job) for job in batch]
        await asyncio.gather(*tasks)

        if i + max_concurrent < len(pending_jobs):
            print(f"✅ Batch complete, {len(pending_jobs) - (i + max_concurrent)} job(s) remaining")


async def process_job_async(job: Dict[str, Any]):
    """
    Async wrapper for process_job to enable parallel processing

    Args:
        job: Job dictionary from Supabase
    """
    job_id = job["id"]
    print(f"\n🔄 [Job {job_id[:8]}] Starting...")

    # Run the synchronous process_job in a thread pool to avoid blocking
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, process_job, job)


if __name__ == "__main__":
    print("🚀 Starting job processor...")
    process_pending_jobs()
    print("✅ Job processor finished")
