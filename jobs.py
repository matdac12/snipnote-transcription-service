import httpx
import io
import json
import os
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Callable
from functools import wraps
from openai import OpenAI
from supabase_client import (
    supabase,
    update_job_status,
    update_job_with_results,
    update_job_progress,
    get_audio_chunks,
    update_chunk_transcript,
    update_chunks_processed,
    increment_retry_count
)
from transcribe import transcribe_audio


# Maximum retry attempts before permanent failure
MAX_RETRY_ATTEMPTS = 5


def is_retryable_error(error: Exception) -> bool:
    """
    Determine if an error is retryable (transient) or permanent.

    Retryable errors include:
    - Rate limits (429)
    - Server errors (500, 502, 503, 504)
    - Timeouts
    - Connection errors

    Permanent errors include:
    - Invalid audio format
    - Authentication failures (401, 403)
    - Bad requests (400)
    - File not found (404)

    Args:
        error: The exception to classify

    Returns:
        True if the error is retryable, False if permanent
    """
    error_str = str(error).lower()

    # Retryable patterns - transient issues that may resolve on retry
    retryable_patterns = [
        "rate limit",
        "rate_limit",
        "429",
        "too many requests",
        "timeout",
        "timed out",
        "connection error",
        "connection refused",
        "connection reset",
        "server error",
        "internal server error",
        "500",
        "502",
        "503",
        "504",
        "bad gateway",
        "service unavailable",
        "gateway timeout",
        "temporarily unavailable",
        "overloaded",
        "capacity",
        "try again",
        "retry",
        "network",
        "socket",
        "eof",
        "broken pipe",
    ]

    # Permanent patterns - errors that won't be fixed by retrying
    permanent_patterns = [
        "invalid audio",
        "invalid file",
        "unsupported format",
        "could not decode",
        "authentication",
        "unauthorized",
        "401",
        "forbidden",
        "403",
        "not found",
        "404",
        "bad request",
        "400",
        "invalid_api_key",
        "api key",
        "permission denied",
        "access denied",
        "file too large",
        "exceeds maximum",
    ]

    # Check for permanent errors first (they take priority)
    for pattern in permanent_patterns:
        if pattern in error_str:
            return False

    # Check for retryable errors
    for pattern in retryable_patterns:
        if pattern in error_str:
            return True

    # Default: treat unknown errors as retryable (safer)
    # This ensures we don't permanently fail on unexpected transient issues
    return True

# Initialize OpenAI client
openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# Reusable HTTP client for connection pooling
http_client = httpx.Client(timeout=120.0)


def retry_with_backoff(max_retries: int = 3, base_delay: float = 1.0):
    """
    Decorator for retrying functions with exponential backoff

    Args:
        max_retries: Maximum number of retry attempts
        base_delay: Base delay in seconds (doubles each retry)
    """
    def decorator(func: Callable):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        delay = base_delay * (2 ** attempt)
                        print(f"   ⚠️ Retry {attempt + 1}/{max_retries} for {func.__name__} after {delay:.1f}s: {e}")
                        time.sleep(delay)
                    else:
                        print(f"   ❌ {func.__name__} failed after {max_retries} attempts: {e}")
            raise last_exception
        return wrapper
    return decorator


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
    Download audio file from URL using reusable HTTP client for connection pooling

    Args:
        audio_url: URL to audio file (Supabase Storage or public URL)

    Returns:
        Audio file bytes

    Raises:
        Exception: If download fails
    """
    print(f"   📥 Downloading audio from {audio_url[:50]}...")

    response = http_client.get(audio_url, follow_redirects=True)
    response.raise_for_status()

    print(f"   ✅ Downloaded {len(response.content)} bytes")
    return response.content


@retry_with_backoff(max_retries=3, base_delay=1.0)
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

    response = openai_client.responses.create(
        model="gpt-5-mini",
        input=[
            {"role": "system", "content": "You create concise one-sentence meeting overviews. Always respond with exactly one clear, informative sentence in the same language as the input transcript."},
            {"role": "user", "content": prompt}
        ],
        reasoning={"effort": "minimal"},
        text={"verbosity": "low"}
    )

    # Find the message output (reasoning output doesn't have content)
    message_output = next((item for item in response.output if item.type == "message"), None)
    if not message_output or not message_output.content:
        raise Exception("No message content in response")

    overview = message_output.content[0].text.strip()
    print(f"   ✅ Overview generated: {overview[:80]}...")
    return overview


@retry_with_backoff(max_retries=3, base_delay=1.0)
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

    response = openai_client.responses.create(
        model="gpt-5-mini",
        input=[
            {"role": "system", "content": "You are a professional meeting summarizer. Create structured, comprehensive summaries that capture key decisions, action items, and next steps. Always respond in the same language as the input transcript."},
            {"role": "user", "content": prompt}
        ],
        reasoning={"effort": "minimal"},
        text={"verbosity": "low"}
    )

    # Find the message output (reasoning output doesn't have content)
    message_output = next((item for item in response.output if item.type == "message"), None)
    if not message_output or not message_output.content:
        raise Exception("No message content in response")

    summary = message_output.content[0].text
    print(f"   ✅ Summary generated ({len(summary)} chars)")
    return summary


@retry_with_backoff(max_retries=3, base_delay=1.0)
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

    response = openai_client.responses.create(
        model="gpt-5-mini",
        input=[
            {"role": "system", "content": "You extract actionable items from text and return them as JSON. Be precise and only return valid JSON. Always use the same language as the input transcript for action descriptions."},
            {"role": "user", "content": prompt}
        ],
        reasoning={"effort": "minimal"}
    )

    try:
        # Find the message output (reasoning output doesn't have content)
        message_output = next((item for item in response.output if item.type == "message"), None)
        if not message_output or not message_output.content:
            raise Exception("No message content in response")

        actions_text = message_output.content[0].text.strip()

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


def process_single_chunk(chunk: Dict[str, Any], total_chunks: int, language: str = None) -> Dict[str, Any]:
    """
    Process a single audio chunk: download and transcribe.
    Used by ThreadPoolExecutor for parallel processing.

    Args:
        chunk: Chunk dictionary with id, chunk_index, file_path
        total_chunks: Total number of chunks (for logging)
        language: Optional language code for transcription

    Returns:
        Dict with chunk_id, chunk_index, transcript
    """
    chunk_id = chunk["id"]
    chunk_index = chunk["chunk_index"]
    file_path = chunk["file_path"]

    try:
        # Download chunk from storage
        chunk_data = download_chunk_from_storage(file_path)

        # Transcribe chunk
        print(f"   🎤 Transcribing chunk {chunk_index + 1}/{total_chunks}...")
        result = transcribe_audio(chunk_data, f"chunk_{chunk_index}.m4a", language=language)
        transcript = result["transcript"]

        print(f"   ✅ Chunk {chunk_index + 1}/{total_chunks} transcribed ({len(transcript)} chars)")

        return {
            "chunk_id": chunk_id,
            "chunk_index": chunk_index,
            "transcript": transcript
        }
    except Exception as e:
        print(f"   ❌ Chunk {chunk_index + 1}/{total_chunks} failed: {e}")
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
    language = job.get("language")  # None if not specified (auto-detect)

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

        # Step 3: Process chunks in PARALLEL (5-70% total progress)
        # Using ThreadPoolExecutor for 5-10x speedup on chunked jobs
        print(f"   🚀 Processing {len(chunks)} chunks in parallel (max 5 workers)...")
        update_job_progress(job_id, 10, f"Transcribing {len(chunks)} chunks in parallel...")

        # Track results by chunk_index to maintain order
        chunk_results: Dict[int, str] = {}
        completed_count = 0

        with ThreadPoolExecutor(max_workers=5) as executor:
            # Submit all chunks for parallel processing
            future_to_chunk = {
                executor.submit(process_single_chunk, chunk, len(chunks), language): chunk
                for chunk in chunks
            }

            # Process completed chunks as they finish
            for future in as_completed(future_to_chunk):
                chunk = future_to_chunk[future]
                try:
                    result = future.result()
                    chunk_id = result["chunk_id"]
                    chunk_index = result["chunk_index"]
                    transcript = result["transcript"]

                    # Save transcript to database
                    update_chunk_transcript(chunk_id, transcript)

                    # Store result by index for ordered merging later
                    chunk_results[chunk_index] = transcript

                    # Update progress
                    completed_count += 1
                    current_progress = 5 + int((completed_count / len(chunks)) * 65)
                    update_job_progress(
                        job_id,
                        current_progress,
                        f"Transcribed {completed_count}/{len(chunks)} chunks..."
                    )
                    update_chunks_processed(job_id, completed_count)

                except Exception as e:
                    chunk_index = chunk.get("chunk_index", "?")
                    print(f"   ❌ Chunk {chunk_index} failed: {e}")
                    raise

        # Build ordered transcripts list from results
        transcripts = [chunk_results[i] for i in sorted(chunk_results.keys())]
        print(f"   ✅ All {len(transcripts)} chunks transcribed in parallel")

        # Step 4: Merge transcripts (70%)
        print(f"   🔗 Merging {len(transcripts)} chunk transcripts...")
        update_job_progress(job_id, 70, "Merging transcripts...")
        full_transcript = "\n".join(transcripts)
        print(f"   ✅ Merged transcript: {len(full_transcript)} chars")

        # Step 5: Generate AI content (70-90%)

        # 5a: Summary (70-80%) - needs full transcript, must run first
        update_job_progress(job_id, 70, "Generating summary...")
        summary = generate_summary(full_transcript)
        update_job_progress(job_id, 80, "Summary generated")

        # 5b: Overview + Actions in PARALLEL (80-90%) - both only need summary
        print(f"   🚀 Generating overview and actions in parallel...")
        update_job_progress(job_id, 80, "Generating overview and extracting actions...")

        with ThreadPoolExecutor(max_workers=2) as executor:
            overview_future = executor.submit(generate_overview, summary)
            actions_future = executor.submit(extract_actions, summary)

            overview = overview_future.result()
            actions = actions_future.result()

        update_job_progress(job_id, 90, "AI content generated")

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
        # Error handling: classify error and decide whether to retry or fail permanently
        error_message = str(e)
        retry_count = job.get("retry_count", 0) or 0

        try:
            if is_retryable_error(e) and retry_count < MAX_RETRY_ATTEMPTS:
                # Retryable error - queue for retry
                print(f"🔄 Chunked job {job_id} failed with retryable error (attempt {retry_count + 1}/{MAX_RETRY_ATTEMPTS}): {error_message}")
                increment_retry_count(job_id, error_message)
                print(f"   📋 Job queued for retry on next cron run")
            else:
                # Permanent error or max retries exceeded
                if retry_count >= MAX_RETRY_ATTEMPTS:
                    print(f"❌ Chunked job {job_id} failed permanently after {MAX_RETRY_ATTEMPTS} attempts: {error_message}")
                    error_message = f"Max retries ({MAX_RETRY_ATTEMPTS}) exceeded. Last error: {error_message}"
                else:
                    print(f"❌ Chunked job {job_id} failed with permanent error: {error_message}")

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
    language = job.get("language")  # None if not specified (auto-detect)

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
            progress_callback=transcription_progress,
            language=language
        )

        transcript = result["transcript"]
        duration = result["duration"]

        print(f"   ✅ Transcription complete: {len(transcript)} chars, {duration:.1f}s")

        # Step 4: Generate AI content (60-90%)

        # 4a: Summary (60-75%) - needs full transcript, must run first
        update_job_progress(job_id, 60, "Generating summary...")
        summary = generate_summary(transcript)
        update_job_progress(job_id, 75, "Summary generated")

        # 4b: Overview + Actions in PARALLEL (75-90%) - both only need summary
        print(f"   🚀 Generating overview and actions in parallel...")
        update_job_progress(job_id, 75, "Generating overview and extracting actions...")

        with ThreadPoolExecutor(max_workers=2) as executor:
            overview_future = executor.submit(generate_overview, summary)
            actions_future = executor.submit(extract_actions, summary)

            overview = overview_future.result()
            actions = actions_future.result()

        update_job_progress(job_id, 90, "AI content generated")

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
        # Error handling: classify error and decide whether to retry or fail permanently
        error_message = str(e)
        retry_count = job.get("retry_count", 0) or 0

        try:
            if is_retryable_error(e) and retry_count < MAX_RETRY_ATTEMPTS:
                # Retryable error - queue for retry
                print(f"🔄 Job {job_id} failed with retryable error (attempt {retry_count + 1}/{MAX_RETRY_ATTEMPTS}): {error_message}")
                increment_retry_count(job_id, error_message)
                print(f"   📋 Job queued for retry on next cron run")
            else:
                # Permanent error or max retries exceeded
                if retry_count >= MAX_RETRY_ATTEMPTS:
                    print(f"❌ Job {job_id} failed permanently after {MAX_RETRY_ATTEMPTS} attempts: {error_message}")
                    error_message = f"Max retries ({MAX_RETRY_ATTEMPTS}) exceeded. Last error: {error_message}"
                else:
                    print(f"❌ Job {job_id} failed with permanent error: {error_message}")

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
