# Security hardening and server-side minutes: rollout guide

Audit of 2026-09-30 (HANDOFF_2026-09-30.md section 3). Branch `claude/security-minutes-fixes`.
Nothing here was deployed or run against production; everything was tested offline with mocks, plus the
SQL migrations on a scratch PostgreSQL 16 with a *mock* Supabase and a *mock* minutes ledger.

## Environment variables

| variable | default | effect |
|---|---|---|
| `AUTH_MODE` | `log` | `off` / `log` / `enforce` for GET `/jobs/{id}` and legacy POST `/jobs` (see DEPLOYMENT.md table) |
| `SUPABASE_AUTH_REMOTE_VERIFY` | `true` | no local JWT key configured -> verify via `GET {SUPABASE_URL}/auth/v1/user` (60 s cache). `false` = fail closed (503) |
| `SUPABASE_ANON_KEY` | unset | `apikey` header for that call (service key is used if unset) |
| `SUPABASE_JWT_SECRET` / `SUPABASE_JWT_USE_JWKS` / `SUPABASE_JWT_JWKS_URL` | unset | local verification instead of the remote call (from the background-upload branch) |
| `CORS_ALLOWED_ORIGINS` | empty | empty = no CORS headers at all; exact origins only, `*` ignored |
| `MAX_DOWNLOAD_BYTES` | 314572800 | hard cap per storage download |
| `AUDIO_URL_ALLOWED_HOSTS` | empty | extra hosts accepted in legacy `audio_url` (custom storage domain) |
| `MAX_CHUNKS_PER_JOB` | 200 | chunk rows / `total_chunks` bound |
| `MAX_ACTIVE_JOBS_PER_USER` | 20 | concurrent queued/running jobs per user (429); 0 = off |
| `MAX_JOB_DURATION_SECONDS` | 43200 | longest accepted `duration` (422) |
| `SERVER_MINUTES_DEBIT_ENABLED` | `false` | worker debit + create-time 402 (needs migration 011) |
| `SERVER_MINUTES_DEBIT_SWEEP_INTERVAL_SECONDS` / `_LOOKBACK_HOURS` / `_BATCH` | 300 / 72 / 25 | sweep for completed-but-undebited jobs |

`RECORDINGS_BUCKET` (default `recordings`) also feeds the URL validation. With every default the service behaves as
before for old app builds: no token required, `user_id` still returned, `audio_url` now `null`.

## Migrations

Order: 006 -> **commit** -> 007 -> 008 -> 009 -> 010 -> 011 (optional, see below) -> 012 (optional).
006 and 007 must be separate runs (`ALTER TYPE ... ADD VALUE`). All are additive/idempotent.

| # | what | needs |
|---|---|---|
| 006/007 | background upload (enum value, columns, indexes) | before the new worker |
| 008/009 | Live Activity tokens (+RLS), `transcription_jobs.stage` | 008 fixed here: its hex CHECK used `{32,512}`, which PostgreSQL rejects at runtime (max repetition 255) |
| 010 | RLS: clients may only write `status='completed'` result columns | **iOS check first** (below); review the WARNINGs it prints |
| 011 | server minutes debit | the real ledger functions; read its header; `TODO(owner)` items; aborts if they are missing |
| 012 | RLS on `audio_chunks` | confirm the app's chunk `file_path` is always `<lowercase uid>/...` (it is, per `uploadAudioChunk`) |

## What the owner must do, in order

1. **nginx, no deploy**: add `location = /transcribe { return 410; }` (or install `deploy/nginx-snipnote-api.conf`,
   which also adds `limit_req` on POST `/jobs`); `nginx -t && systemctl reload nginx`.
2. **Supabase**: run 006, then 007 (if not done yet). Run 010 and read its output; run the verification queries below.
   Optionally 012. Before 010, build/run the current iOS app once against a staging project: on-device transcription must
   still save its result (INSERT and UPDATE of `transcription_jobs`). If any other client code writes that table, extend
   the column lists in 010 first.
3. **VPS**: `git pull && .venv/bin/pip install -r requirements.txt`, merge the env block from `deploy/env.example`
   (nothing is required; defaults are backward compatible), restart `snipnote-api` and `snipnote-worker`.
   Check `journalctl -u snipnote-worker | grep "invalid audio location"` stays empty for real traffic (if not:
   `SUPABASE_URL` differs from the host the app uses -> set `AUDIO_URL_ALLOWED_HOSTS`).
4. Watch `[auth]` lines in the API journal. Ship the iOS build (follow-ups below). When almost all requests are
   `authenticated`, set `AUTH_MODE=enforce` and restart the API. Pair it with a force-update: old builds then poll
   without seeing results (they get 401, which their code logs as a polling error and ignores).
5. **Minutes** (only after reading migration 011): run the four verification steps in its header on staging, apply 011,
   ship the iOS build that handles 402, then `SERVER_MINUTES_DEBIT_ENABLED=true` and restart both units. Roll back with
   `false` (no data is lost; the sweep simply stops).
6. Later (not done here): make the `recordings` bucket private (the worker already downloads by path with the service
   key; playback uses signed URLs), run the services as non-root.

Verification queries (Supabase SQL editor):

```sql
select policyname, cmd, roles, permissive from pg_policies where tablename in ('transcription_jobs','audio_chunks');
select grantee, privilege_type, column_name from information_schema.column_privileges
 where table_name = 'transcription_jobs' and grantee in ('anon','authenticated') order by 1,3;   -- only result columns for INSERT/UPDATE
select relrowsecurity from pg_class where oid in ('public.transcription_jobs'::regclass, 'public.audio_chunks'::regclass);
select id, public, file_size_limit from storage.buckets where id = 'recordings';      -- is the bucket public?
```

## iOS follow-ups (not done; the iOS repo was not touched)

1. `RenderTranscriptionService.swift`: add `request.setValue("Bearer \(try await SupabaseManager.shared.client.auth.session.accessToken)", forHTTPHeaderField: "Authorization")`
   to the three requests: `createJob` (~line 46), `createChunkedJob` (~106) and `getJobStatus` (~165). (`transcribe()` has no callers: delete it.)
   Until this ships, `AUTH_MODE` must stay `log`.
2. **401 is not retryable**: in `getJobStatus` throw a new `TranscriptionError.unauthorized` on 401 without touching
   `retryAttempts`; refresh the session once and retry; if it still fails, stop polling and tell the user to sign in again.
   (Today a non-200 answer throws `serverError` and `pollJobStatus` just logs it and keeps polling, so enforcing too early
   looks like a job that never finishes.)
3. **Fallback only on a confirmed failure** (`MeetingDetailView.pollJobStatus` ~1140 / `attemptOnDeviceFallback` ~1682):
   `maxRetriesExceeded` comes from three *network* errors in `getJobStatus`, not from a failed job. Keep polling with back-off
   and an "offline" hint; start the on-device fallback only when the server returned `failed` (or the job is unknown, 404).
   Otherwise the server job still completes (and costs money) while the user pays for a second, on-device run.
4. **Do not debit server jobs in the app**: the worker debits by `meeting_id` when it finishes. After a server job completes
   call `MinutesManager.refreshBalance()` so the UI shows the new balance. The fallback's `debit_minutes(p_meeting_id)`
   then fails as a duplicate, which `isDuplicateDebitError` already treats as success (see the risk below).
5. **402 handling** (only once `SERVER_MINUTES_DEBIT_ENABLED=true`): `createJob`/`createChunkedJob`/the upload-pending call answer
   `402` with `{"detail":{"error":"insufficient_minutes","required_minutes":N,"reserved_minutes":R,"balance_minutes":B}}`;
   show the purchase sheet instead of a generic failure, and do not start an on-device run that would fail to debit too.
6. `JobStatusResponse.audioUrl` is now always `null` for the app (`grep` shows the app never reads it). `userId` stays.
7. Background-upload clients: send the token on `GET /jobs/{id}` as well once `AUTH_MODE=enforce`.

## Unverified / known gaps

* The real minutes ledger (tables, `debit_minutes` internals, whether the balance may go negative, whether it really rejects
  a second debit for the same `p_meeting_id`). If it does not dedupe by meeting, a client fallback can still double-charge.
* Live RLS policies/grants, bucket visibility, nginx config and the VPS env. `transcription_jobs.retry_count` is written by
  `increment_retry_count` but no migration in either repo creates it (presumably added by hand).
* Whether `SUPABASE_URL` on the VPS equals the host in the app's stored `audio_url`s (old rows are irrelevant; new jobs are).
* `GET /auth/v1/user` with the service key as `apikey` (set `SUPABASE_ANON_KEY` to be safe) was not run against Supabase.
* Upload-pending creation has no per-user active-job cap (it needs a token and is idempotent per meeting).
* The per-IP nginx rate limit is per `$remote_addr`; behind CGNAT many users can share an address (60/min, burst 20).
