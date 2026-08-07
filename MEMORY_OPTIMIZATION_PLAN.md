# Memory Optimization Plan: Sequential Processing

> **Status**: NOT IMPLEMENTED - Saved for future reference if RAM needs to be reduced from 8GB to 2GB.

## Problem Context

With 2GB RAM on Render, the transcription worker was crashing with:
```
Out of memory (used over 2Gi)
```

Root causes:
1. Concurrent job processing (3 jobs simultaneously via asyncio)
2. Parallel chunk processing (5 workers per job via ThreadPoolExecutor)
3. No explicit memory cleanup (`del`, `gc.collect()`)
4. Large AudioSegment objects held in memory

## Current Solution

Upgraded Render worker to **8GB RAM** - parallel processing works fast and reliably.

---

## Alternative: Sequential Processing (for 2GB RAM)

If you need to reduce RAM back to 2GB, implement these changes:

### Git History Reference
- **`fe9bce2`** (Oct 3, 2025): Original sequential implementation
- **`f3cce5d`** (Oct 4, 2025): Added parallel job processing
- **`685e248`** (Nov 28, 2025): Added ThreadPoolExecutor for chunks

---

## Files to Modify

### 1. `worker.py`

**Remove async and concurrency:**

```python
# BEFORE
import asyncio
MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "3"))

def run_once():
    print(f"Starting transcription worker (single run, max {MAX_CONCURRENT_JOBS} concurrent)...")
    asyncio.run(process_pending_jobs(max_concurrent=MAX_CONCURRENT_JOBS))
    print("Worker finished\n")

# AFTER
def run_once():
    print("Starting transcription worker (single run, sequential)...")
    process_pending_jobs()
    print("Worker finished\n")
```

### 2. `jobs.py`

**Add gc import:**
```python
import gc
```

**Remove parallel imports:**
```python
# REMOVE these:
import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
```

**Replace async process_pending_jobs with sequential:**
```python
# BEFORE
async def process_pending_jobs(max_concurrent: int = 3):
    pending_jobs = get_pending_jobs()
    if not pending_jobs:
        return
    for i in range(0, len(pending_jobs), max_concurrent):
        batch = pending_jobs[i:i + max_concurrent]
        tasks = [process_job_async(job) for job in batch]
        await asyncio.gather(*tasks)

# AFTER
def process_pending_jobs():
    pending_jobs = get_pending_jobs()
    if not pending_jobs:
        return
    print(f"Found {len(pending_jobs)} pending job(s), processing sequentially")
    for job in pending_jobs:
        job_id = job["id"]
        print(f"\nProcessing job {job_id}...")
        process_job(job)
        gc.collect()  # Memory cleanup after each job
```

**Delete process_job_async function entirely.**

**Replace parallel chunk processing in process_chunked_job():**
```python
# BEFORE (around line 448)
with ThreadPoolExecutor(max_workers=5) as executor:
    future_to_chunk = {...}
    for future in as_completed(future_to_chunk):
        ...

# AFTER
for i, chunk in enumerate(chunks):
    chunk_id = chunk["id"]
    chunk_index = chunk["chunk_index"]
    file_path = chunk["file_path"]

    chunk_data = download_chunk_from_storage(file_path)
    result = transcribe_audio(chunk_data, f"chunk_{chunk_index}.m4a", language=language)
    transcript = result["transcript"]

    update_chunk_transcript(chunk_id, transcript)
    chunk_results[chunk_index] = transcript

    current_progress = 5 + int(((i + 1) / len(chunks)) * 65)
    update_job_progress(job_id, current_progress, f"Transcribed {i + 1}/{len(chunks)} chunks...")
    update_chunks_processed(job_id, i + 1)

    del chunk_data
    gc.collect()
```

**Replace parallel AI generation with sequential (2 places):**
```python
# BEFORE (in process_chunked_job and process_job)
with ThreadPoolExecutor(max_workers=2) as executor:
    overview_future = executor.submit(generate_overview, summary)
    actions_future = executor.submit(extract_actions, summary)
    overview = overview_future.result()
    actions = actions_future.result()

# AFTER
overview = generate_overview(summary)
actions = extract_actions(summary)
```

**Add memory cleanup in process_job() after transcription:**
```python
transcript = result["transcript"]
duration = result["duration"]
del audio_data
gc.collect()
```

### 3. `transcribe.py`

**Add gc import:**
```python
import gc
```

**Add cleanup in chunk_audio() after while loop:**
```python
# After creating each chunk in the loop:
del chunk_audio
gc.collect()

# After the loop ends:
del audio
gc.collect()
return chunks
```

---

## Expected Results

| Metric | 8GB (current) | 2GB (sequential) |
|--------|---------------|------------------|
| Peak memory | ~2-3GB | ~300-500MB |
| Jobs at once | 3 parallel | 1 sequential |
| Chunks at once | 5 parallel | 1 sequential |
| Speed | Fast | Slower |
| Stability | Good | Good |

---

## Trade-offs

**8GB RAM (current)**:
- Pro: Fast parallel processing
- Pro: No code changes needed
- Con: Higher Render costs

**2GB RAM (sequential)**:
- Pro: Lower Render costs
- Pro: More predictable memory usage
- Con: Slower processing (jobs wait in queue)
- Con: Code changes required
