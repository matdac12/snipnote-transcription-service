# Background upload: API contract (server <-> iOS)

The app hands ONE original, unchunked audio file to a background `URLSession`
(`uploadTask(with:fromFile:)`, raw `PUT`) and may be killed or suspended. Nobody can
run app code when the upload ends, so the **server** notices the file and starts the
job (the worker promotes it within about one poll interval, see "Timing").

```
app                      api.snipnote.app            Supabase Storage        worker
 | POST /jobs upload_pending   |                              |                 |
 |---------------------------->| create job awaiting_upload   |                 |
 |                             |--- sign upload URL --------->|                 |
 |<-- job_id, upload_url ------|                              |                 |
 | background PUT (raw bytes) ------------------------------->|                 |
 |   (app may be killed)       |                              |<-- size check --| every loop
 |                             |                              |   awaiting_upload -> pending
 | GET /jobs/{id}  (whenever the app is next alive)           |   pending -> processing -> completed
```

## 1. Create the job

`POST https://api.snipnote.app/jobs`

Headers: `Authorization: Bearer <Supabase access token of the signed-in user>`,
`Content-Type: application/json`. (`X-API-Key` only if the server has `API_KEY` set; it
does not today.) The user is taken **from the token**; the body's `user_id` is ignored
(if sent it must equal the token's user, else `403`).

```json
{
  "upload_pending": true,
  "meeting_id": "0b1c...-uuid",          // required, UUID
  "expected_bytes": 148234567,            // required, EXACT size of the file on disk
  "duration": 5423.5,                     // optional, seconds (used to choose the processing strategy)
  "language": "it",                       // optional ISO-639-1, omit for auto-detect
  "provider": "openai",                   // "openai" | "xai" (alias: "transcription_provider"), default openai
  "file_extension": "m4a",                // optional: m4a (default) mp4 aac mp3 wav caf flac ogg webm
  "content_type": "audio/m4a"             // optional, default derived from the extension
}
```

Response `200`:

```json
{
  "job_id": "e4a1...",
  "status": "awaiting_upload",
  "created_at": "2026-09-30T10:00:00+00:00",
  "upload_url": "https://<ref>.supabase.co/storage/v1/object/upload/sign/recordings/<user>/<meeting>.m4a?token=...",
  "upload_method": "PUT",
  "upload_headers": { "Content-Type": "audio/m4a", "x-upsert": "true" },
  "expires_at": "2026-09-30T12:00:00+00:00",     // when the signed URL stops working (~2 h)
  "upload_deadline": "2026-09-30T16:00:00+00:00", // when the server gives up on the job (6 h)
  "storage_path": "<user>/<meeting>.m4a",         // same convention as uploadAudioRecording
  "expected_bytes": 148234567
}
```

* **Idempotent per `meeting_id`.** Calling again (lost response, relaunch, expired URL)
  returns the same `job_id` with a **fresh** `upload_url` and a renewed
  `upload_deadline`; a changed `expected_bytes` is accepted while still awaiting.
* If the job is already past upload (`pending`, `processing`, `completed`) the response
  has that `status` and **no** `upload_url`: do not upload again.
* A `failed` job (for example `upload_expired`) does not block: the next call creates a
  new job.
* Treat `upload_url` as a secret (it is a credential): do not log it or show it.

Errors: `400` bad meeting_id / extension / content_type / expected_bytes, `401` missing,
invalid or expired token (refresh the session and retry), `403` user_id mismatch, `413`
expected_bytes above the server limit (`MAX_UPLOAD_BYTES`, default 300 MiB), `422`
malformed body, `502` could not mint the URL (retry in a few seconds), `503` server auth
not configured, `500` other.

## 2. Upload (background URLSession)

```
PUT <upload_url>
Content-Type: <upload_headers["Content-Type"]>
x-upsert: true
<raw file bytes as the body; no multipart, no Authorization header needed>
```

Use `URLSessionConfiguration.background`, `uploadTask(with: request, fromFile: url)`
and keep the file on disk until the task completes. A `200` means the object exists.
If the task fails with `401`/`403` the URL expired (or is invalid): call
`POST /jobs` again for the same meeting to get a fresh URL and start a new upload
(uploads are not resumable, they restart from byte 0; `x-upsert: true` lets the new
upload replace a partial/previous object).

Persist `job_id`, `storage_path` and `expires_at` locally before the upload starts. Insert
the `recordings` row for `storage_path` from the app (before the hand-off, or on next
launch): the server does not create it.

## 3. Poll

`GET https://api.snipnote.app/jobs/{job_id}` as today. Status flow:

| status            | meaning | app action |
|-------------------|---------|------------|
| `awaiting_upload` | job exists, file not (yet) seen complete by the server | if the PUT already returned 200, show "queued"; otherwise "uploading" |
| `pending`         | upload verified, waiting for the worker | "queued" |
| `processing`      | transcribing / summarising | progress via `progress_percentage`, `current_stage` |
| `completed`       | `transcript`, `overview`, `summary`, `actions` set | done |
| `failed`          | `error_message` set | `upload_expired`: upload never completed in time (or had the wrong size) -> create a new job with `POST /jobs` and upload again; anything else: the existing failure handling |

The GET response additionally carries `expected_bytes` and `upload_deadline` (null for
non-upload jobs); everything else is unchanged.

### Timing

The worker checks uploads once per loop (`WORKER_INTERVAL_SECONDS`, 20 s), so a finished
PUT turns into `pending` within ~20 s. While the worker is busy with a long job the loop
does not run, so `awaiting_upload` can linger after the PUT finished (the job would be
queued behind the running one anyway). **Trust the PUT's completion, not `awaiting_upload`,
to decide that the file is uploaded.**

The server promotes only when the stored object's size equals `expected_bytes` exactly.
A different size (wrong `expected_bytes`, truncated or different file) never promotes and
ends as `failed` / `upload_expired` at the deadline; fix by calling `POST /jobs` with the
correct `expected_bytes` (same job) and uploading again before the deadline.

## 4. Why old app builds cannot break

`awaiting_upload` is a new value of the `status` field, which old builds decode as a
closed enum (`pending|processing|completed|failed`). Old builds never send
`upload_pending`, so they never own a job that can be in that state, and legacy
`POST /jobs` still creates `pending` jobs exactly as before. The only way an old build
could see the value is by polling a job id created by a new build (for example a
meeting shared between devices); the fallback there is the existing "unknown response ->
retry, then on-device fallback" path. New builds must decode `status` tolerantly
(unknown string -> treat as "in progress") so future states do not repeat this problem.
Anything else that enumerates `transcription_jobs.status` (dashboards, Edge Functions)
must tolerate the value too.

## 5. Server limits and operator notes

* Supabase Storage **global file size limit** (Dashboard -> Storage -> Settings) and the
  `recordings` bucket's own limit must both allow the largest file (set >= 150 MB; the API
  caps `expected_bytes` at `MAX_UPLOAD_BYTES`, default 300 MiB). Above the limit the PUT
  fails with `413` and the job expires.
* Signed upload URLs are valid for a fixed ~2 h (Supabase setting); the job deadline is 6 h
  (`UPLOAD_PENDING_TTL_SECONDS`) and is renewed each time a URL is issued.
* After `upload_expired` the server deletes the partial/incorrect object at `storage_path`.
