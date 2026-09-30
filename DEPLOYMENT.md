# Deployment Instructions

> **Host: the `omni` VPS** (`https://api.snipnote.app`).
> Migrated off Render in Aug 2026; the Render services have been suspended and
> `render.yaml` removed. Git history has the old Blueprint if it is ever needed.

## 🖥️ VPS Deployment (primary)

### Layout

| Path | Purpose |
|------|---------|
| `/opt/snipnote-transcription/` | Git checkout — deploys are `git pull` + restart |
| `/opt/snipnote-transcription/.venv/` | Python 3.12 virtualenv |
| `/etc/snipnote-transcription/env` | Secrets + tuning, `chmod 600`, root-owned |
| `/etc/systemd/system/snipnote-api.service` | uvicorn on `127.0.0.1:8100` |
| `/etc/systemd/system/snipnote-worker.service` | `worker.py --continuous` |
| `/etc/nginx/sites-enabled/snipnote-api` | TLS termination + reverse proxy |

The unit files, nginx vhost and an annotated env template live in [`deploy/`](deploy/).

### First-time provisioning

```bash
ssh omni

apt-get update && apt-get install -y python3-venv ffmpeg

git clone https://github.com/matdac12/snipnote-transcription-service.git /opt/snipnote-transcription
cd /opt/snipnote-transcription
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

mkdir -p /etc/snipnote-transcription
cp deploy/env.example /etc/snipnote-transcription/env
chmod 600 /etc/snipnote-transcription/env
$EDITOR /etc/snipnote-transcription/env   # fill in the real secrets

cp deploy/snipnote-api.service deploy/snipnote-worker.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now snipnote-api snipnote-worker

cp deploy/nginx-snipnote-api.conf /etc/nginx/sites-available/snipnote-api
cp deploy/00-default-deny.conf /etc/nginx/sites-available/00-default-deny
ln -sf /etc/nginx/sites-available/snipnote-api /etc/nginx/sites-enabled/snipnote-api
ln -sf /etc/nginx/sites-available/00-default-deny /etc/nginx/sites-enabled/00-default-deny
nginx -t && systemctl reload nginx

# Open the web ports (this box otherwise keeps 80/443 closed — see below)
ufw allow 80/tcp && ufw allow 443/tcp

# TLS — requires api.snipnote.app to already resolve to this box
apt-get install -y certbot python3-certbot-nginx
certbot --nginx -d api.snipnote.app --non-interactive --agree-tos -m <your-email> --redirect
```

`certbot` installs a systemd timer that handles renewal automatically.

### Two traps specific to this box

**1. nginx cannot bind `0.0.0.0:443`.** `tailscaled` already listens on `:443` on the
Tailscale addresses, so certbot's default `listen 443 ssl;` fails with
`bind() to 0.0.0.0:443 failed (98: Address already in use)` — and nginx silently keeps
serving only port 80. After the first certbot run, edit
`/etc/nginx/sites-available/snipnote-api`: delete the `listen [::]:443` line and change
`listen 443 ssl;` to `listen 62.238.24.246:443 ssl;`, then `systemctl restart nginx`
(a reload is not enough to pick up new listen sockets after a failed bind). Verify with
`ss -tlnp | grep nginx`.

**2. Opening 80/443 exposes every other vhost.** Before this migration, ufw denied both
ports and everything was reached through the Cloudflare Tunnel. The `whatsapp-omni` vhost
was `default_server`, so opening the ports would have published the Omni assistant to the
internet. This is now prevented by `00-default-deny.conf` plus explicit
`server_name` + `allow 127.0.0.1; allow ::1; deny all;` on the tunnel-only vhosts. If you
add a new vhost here, keep that pattern. Check it still holds with:

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://<public-ip>/                                  # want 000
curl -s -o /dev/null -w "%{http_code}\n" -H "Host: api.bedigital-omni.com" http://<public-ip>/ # want 403
```

### Deploying a change

```bash
ssh omni 'cd /opt/snipnote-transcription \
  && git pull \
  && .venv/bin/pip install -r requirements.txt \
  && systemctl restart snipnote-api snipnote-worker'
```

### Operating

```bash
systemctl status snipnote-api snipnote-worker
journalctl -u snipnote-worker -f          # live job processing
journalctl -u snipnote-api -n 100
curl -s https://api.snipnote.app/          # {"status":"healthy",...}
```

### Rolling back the transcription model

The transcription model is the `transcription` row of the Supabase `ai_model_config`
table (seeded `gpt-transcribe`). To revert, set its `model` to `gpt-4o-transcribe` in the
Table Editor. It applies within 60 seconds with no restart, and changes the iOS app's
on-device path too. `TRANSCRIPTION_MODEL` in `/etc/snipnote-transcription/env` is only
the default used when the table can't be read.

### Changing the AI summary model

Transcription, overview, summary and action extraction use the model configured per task in the
Supabase table `ai_model_config`. Edit the row in the Supabase Table Editor; it applies
within 60 seconds, with no restart needed. See [`AI_MODEL_CONFIG.md`](AI_MODEL_CONFIG.md), which
also has step-by-step instructions for deploying this change on the VPS.

### Memory notes

This box has 2 vCPU / 3.7 GB shared with the Omni assistant, Postgres and two Next.js
apps. `MAX_CONCURRENT_JOBS=1` and `MAX_CHUNK_WORKERS=2` (both below the code defaults of
3 and 5) keep the worker inside its `MemoryMax=1500M`. If you ever see the worker being
restarted by systemd under load, `MEMORY_OPTIMIZATION_PLAN.md` describes the fully
sequential rewrite as the next lever.

### Leftover from the Render era

The Supabase **Database → Webhooks** entry that used to poke the Render cron worker
on job insert is now dead — the continuous poller replaces it. Delete it from the
Supabase dashboard.

---

## 📋 Complete AI Pipeline

When a job is processed, the worker now:

1. **Download audio** from Supabase Storage
2. **Transcription API** (`gpt-transcribe`) → Transcribe audio
3. **`gpt-5-mini`** → Generate comprehensive summary
4. **`gpt-5-mini`** → Generate 1-sentence overview and extract action items (in parallel)
6. **Update database** with all results (status=completed)

## 🧪 Testing

After deployment, test with a real audio file:

### On iOS:
1. Share an audio file to SnipNote
2. Ensure "Server Transcription" toggle is ON
3. Tap "Analyze Meeting"
4. Watch it navigate to MeetingDetailView
5. Pull to refresh or wait 15 seconds for polling

### Expected Logs (`journalctl -u snipnote-worker -f`):
```
🔄 Processing job abc-123...
   ⚙️  Updating status to 'processing'...
   📥 Downloading audio...
   ✅ Downloaded 96635 bytes
   🎤 Transcribing audio...
   ✅ Transcription complete: 450 chars, 11.2s
   📝 Generating overview...
   ✅ Overview generated: Team discussed Q4 goals...
   📄 Generating summary...
   ✅ Summary generated (1200 chars)
   ✅ Extracting actions...
   ✅ Actions extracted: 3 items
   💾 Saving all results to database...
✅ Job abc-123 completed successfully!
   - Transcript: 450 chars
   - Overview: Team discussed Q4 goals and assigned project leads...
   - Summary: 1200 chars
   - Actions: 3 items
```

### Expected Logs (iOS Console):
```
📊 Job status: Processing
📊 Job status: Completed
✅ [MeetingDetail] Overview: Team discussed Q4 goals...
✅ [MeetingDetail] Summary: 1200 chars
✅ [MeetingDetail] Created 3 action items
✅ [MeetingDetail] Async job completed with full AI processing
```

## 💰 Cost Estimates

Transcription uses `gpt-transcribe` at $0.0045/min (was `gpt-4o-transcribe` at
$0.006/min — a 25% reduction). Summary, overview and actions use `gpt-5-mini`.

Per 1-minute audio file:
- Transcription: $0.0045
- Overview: ~$0.0001
- Summary: ~$0.001
- Actions: ~$0.0005
- **Total: ~$0.0061**

Per 10-minute audio file:
- Transcription: $0.045
- Text generation (all): ~$0.002
- **Total: ~$0.047**

## 🌍 Language Support

All prompts include automatic language detection:
> "Identify the language spoken and always respond in the same language as the input transcript."

This ensures the overview, summary, and actions are generated in the same language as the meeting.

## ⚠️ Important Notes

- **Environment Variable**: Make sure `OPENAI_API_KEY` is set in `/etc/snipnote-transcription/env`
- **Actions Format**: Stored as JSONB in database, converted to iOS `Action` objects automatically
- **Error Handling**: If AI generation fails, the job will fail (not partial completion)
- **Poll Frequency**: Worker checks for pending jobs every `WORKER_INTERVAL_SECONDS` (20s)

## ✅ Ready to Deploy!

1. Add `OPENAI_API_KEY` environment variable
2. Push changes to GitHub (or manual deploy)
3. Test with a real audio file
4. Monitor `journalctl -u snipnote-worker -f` for successful AI processing

All done! 🎉

## Saved transcription provider release

This release is prepared locally; deployment, credentials, restarts and paid
smoke tests require separate authorization. Local main includes four unpushed
configuration commits ending at `5176d6b`; deploy these together with the feature.

1. Inspect Supabase schema/history and apply only the app's additive migration
   `20260930105323_add_transcription_provider.sql`. Do not replay historical
   migrations with blanket `supabase db push`.
2. Set `XAI_API_KEY` in Supabase Edge Function secrets and separately in the
   VPS `/etc/snipnote-transcription/env` (root-owned, mode 600). Add it on the
   VPS now, before deploying provider-capable API/worker code; Supabase's secret
   alone does not configure the VPS. Preserve OpenAI credentials for text tasks.
3. Deploy the reviewed proxy with JWT verification, then this service API and
   worker. Restart **both** `snipnote-api.service` and `snipnote-worker.service`
   in the approved window. Verify health and sanitized logs without printing keys.
4. Run staging smoke tests, then release iOS last.

`POST /jobs` accepts `transcription_provider: openai|xai` for regular and chunked
jobs; the DB stores the value and GET status returns it. `/transcribe` accepts
that same multipart field. Missing fields/legacy jobs default to OpenAI; explicit
unknown values return 422. Workers retain the stored choice across parallel
chunks, internal chunking and retries; no provider failure changes provider.
Model rows `transcription`/`transcription_xai` use the existing 60-second cache.
Text generation, audio processing, minutes and notifications are unchanged.

Before authorized staging release, test both providers on short proxy audio and
long regular/chunked jobs, each with English, Italian and auto language. Change
Settings during delayed upload and after a transient failure: all attempts must
retain the captured/stored provider and the next operation must use the new
choice. Check transcript quality, progress, summaries/actions, notifications and
minutes. Simulate missing xAI key and 401/403/429/5xx on staging only; confirm no
OpenAI fallback and no secrets/content in logs. These paid tests were not run.

Rollback app and backends together; keep the additive DB column/config row.
Stop new xAI submissions and drain or explicitly fail pending xAI jobs before
rolling workers back to versions that ignore provider, or they could transcribe
those jobs using OpenAI. Keep xAI credentials until queued jobs are handled.

### Migration order (all optional features)

Apply in numeric order; 006 must be **committed** before 007 is run (`ALTER TYPE ... ADD VALUE`):

| # | file | feature |
|---|------|---------|
| 006 | `006_add_awaiting_upload_status.sql` | background upload (enum value / CHECK) |
| 007 | `007_background_upload_columns.sql` | background upload (columns, indexes; needs 006 committed) |
| 008 | `008_create_live_activity_tokens_table.sql` | Live Activity tokens (+ RLS) |
| 009 | `009_add_stage_to_transcription_jobs.sql` | `transcription_jobs.stage` |

The APNs migrations were originally numbered 006/007 on their own branch; they were renumbered to
008/009 when the two branches were integrated. If 006/007 of the *old* APNs numbering were
already applied on some environment, nothing needs re-running (both are idempotent), only the
file names differ.

### Live Activity (APNs) pushes

Optional. With all four `APNS_*` variables unset the worker behaves exactly as before.
Contract with the iOS app: [`docs/LIVE_ACTIVITY_CONTRACT.md`](docs/LIVE_ACTIVITY_CONTRACT.md).

1. **Apple key** (Apple Developer, Account owner/admin): Certificates, Identifiers & Profiles,
   Keys, `+`. Name it, tick **Apple Push Notifications service (APNs)**, Continue, Register,
   **Download** the `AuthKey_<KEYID>.p8` (only downloadable once). Note the **Key ID** (10 chars)
   and your **Team ID** (Membership details). One key works for sandbox and production and for
   every app of the team. The app's bundle ID must have Push Notifications enabled (the iOS
   side also adds `NSSupportsLiveActivities` and the push capability, later).
2. **Put the key on the VPS** (outside the git checkout):
   ```bash
   scp AuthKey_ABC123DEFG.p8 omni:/etc/snipnote-transcription/
   ssh omni 'chown root:root /etc/snipnote-transcription/AuthKey_*.p8 && chmod 600 /etc/snipnote-transcription/AuthKey_*.p8'
   ```
   Then add to `/etc/snipnote-transcription/env` (see `deploy/env.example`):
   `APNS_KEY_P8=/etc/snipnote-transcription/AuthKey_ABC123DEFG.p8`, `APNS_KEY_ID`,
   `APNS_TEAM_ID`, `APNS_BUNDLE_ID`. The worker unit runs as root like the env file, so root-only
   `600` is readable; if you ever run it as another user, `chown` the key to that user. Never
   commit the `.p8`; never paste it into logs or chat.
3. **Migrations** (Supabase SQL editor, in order, before deploying): `migrations/008_create_live_activity_tokens_table.sql`,
   `migrations/009_add_stage_to_transcription_jobs.sql` (numbered after the background-upload
   migrations 006/007; 008 references `transcription_jobs` only through its id, so it does not
   need 006/007 to exist, but apply 006 -> 007 -> 008 -> 009 in order on a fresh project). The worker tolerates them being missing
   (logs once, jobs unaffected), so order is not critical for safety.
4. `git pull && .venv/bin/pip install -r requirements.txt && systemctl restart snipnote-worker`
   (new deps: `httpx[http2]` -> `h2`, `PyJWT[crypto]`). Startup log says
   `APNs: live activity pushes enabled (topic ...)` or `disabled (APNS_* not set)`.
5. Verify: start a job from a debug build, watch `journalctl -u snipnote-worker -f | grep APNs`.
   Only failures are logged (`job 1a2b3c4d token ...abcdef transcribing: 400 BadDeviceToken`).
   Common reasons: `BadDeviceToken` (sandbox token on production or vice versa; the row is
   deleted), `TopicDisallowed`/`DeviceTokenNotForTopic` (wrong `APNS_BUNDLE_ID`),
   `InvalidProviderToken` (wrong key id / team id / key), `TooManyProviderTokenUpdates`.
6. Roll back instantly: unset the `APNS_*` variables (or `LIVE_ACTIVITY_ENABLED=false`, which also
   stops the `stage` column writes) and restart the worker.

Behaviour: the notifier runs on one background thread; job threads only enqueue, so APNs or
Supabase slowness never delays a job and any error is swallowed and logged by class name.
Stage changes are always pushed, in-stage progress at most once per
`APNS_PROGRESS_INTERVAL_SECONDS` (20 s). Done/failed send an `end` event with an alert (this
also delivers "ready/failed" when the app is closed) and delete the job's tokens. The provider
JWT is cached and re-minted every 50 minutes. A worker killed with SIGKILL can lose queued pushes;
a clean exit flushes them (8 s cap).

### xAI single-request transcription

For `transcription_provider: xai` the worker no longer uses the two-level chunking:
it streams the audio (one file, or the iOS upload chunks in order) to a temp dir,
joins/re-encodes it with ffmpeg to one mono 16 kHz ~48 kbps MP3 (the concat *filter*
is used because the iOS chunks are separately exported files, so byte/demuxer
concatenation is unsafe), and sends ONE `POST /v1/stt` whose multipart body is
streamed from disk. Summary/overview/actions (OpenAI), result saving and the
job `duration` are unchanged (`duration` is now measured with ffprobe instead of
the `bytes/32000` estimate for regular jobs; chunked jobs still prefer the job's
own duration). OpenAI jobs and `/transcribe` are untouched.

Fallback to the previous chunked xAI path (no job failure) happens when: the kill
switch is off, ffmpeg fails/is missing, the prepared file exceeds
`XAI_SINGLE_REQUEST_MAX_BYTES`, or the request still fails after
`XAI_SINGLE_REQUEST_MAX_ATTEMPTS` (timeouts, connection errors, 408/425/429/5xx) or
with any other non-auth error (e.g. 413). 401/403 and a missing `XAI_API_KEY` fail
the job (chunking cannot help). Caveat: a read timeout after xAI already finished
would be billed twice (single request + chunked fallback).

Requirements: `ffmpeg` and `ffprobe` on the worker's PATH (the apt `ffmpeg`
package provides both; `install_ffmpeg.sh` already installs it) and free disk of
about 2x the audio size in `XAI_WORK_DIR` (default system temp; keep it off tmpfs).
Knobs are documented in `deploy/env.example` (`XAI_SINGLE_REQUEST_ENABLED`,
`XAI_SINGLE_REQUEST_MAX_BYTES`, `XAI_STT_TIMEOUT_SECONDS`,
`XAI_SINGLE_REQUEST_MAX_ATTEMPTS`, `XAI_AUDIO_BITRATE_KBPS`,
`XAI_FFMPEG_TIMEOUT_SECONDS`, `XAI_WORK_DIR`). The 500MB xAI file limit and any
duration limit are unverified; the 100MB default is conservative.

Manual check on the VPS after `systemctl restart snipnote-worker`: submit a short
(<1 min) xAI job and a 1h+ xAI job (regular and chunked upload), watch
`journalctl -u snipnote-worker -f` for `xAI single request:` lines, confirm stages
"Downloading audio / Preparing audio / Transcribing audio", check `ls
${XAI_WORK_DIR:-/tmp}/snipnote-xai-*` is empty afterwards and peak RSS
(`systemd-cgtop`) stays well under 1500M. Roll back instantly with
`XAI_SINGLE_REQUEST_ENABLED=false` + worker restart.

### Background upload (`upload_pending`)

Server side of "hand one original file to a background URLSession". Full app-facing
contract: [`docs/BACKGROUND_UPLOAD_CONTRACT.md`](docs/BACKGROUND_UPLOAD_CONTRACT.md).

How it works: `POST /jobs` with `upload_pending=true` (Supabase user JWT required)
creates a job with status `awaiting_upload` and returns a Supabase Storage signed upload
URL for `recordings/<user_id>/<meeting_id>.<ext>`. Each worker loop first runs
`promote_uploaded_jobs()`: an `awaiting_upload` job whose object exists with exactly
`expected_bytes` becomes `pending` (compare-and-set on status); jobs older than their
`upload_deadline` (`UPLOAD_PENDING_TTL_SECONDS`, 6 h) become `failed` / `upload_expired`
and the partial object is deleted. The existing `pending` flow then runs; the worker
streams the file from storage to disk. Files above `LARGE_FILE_THRESHOLD_BYTES` (15 MiB)
or `LARGE_FILE_THRESHOLD_SECONDS` (30 min) are split by ffmpeg into ~5 min segments
and transcribed per segment with the existing overlap merge (both providers; xAI still
tries its single request first). The legacy `upload_pending=false` path is untouched
(no JWT requirement added there).

Release order (additive, nothing breaks if the app is not yet updated):

1. **Migrations, in this order, as two separate runs**: `migrations/006_add_awaiting_upload_status.sql`
   (adds the enum value / extends the CHECK), wait until it has committed, then
   `migrations/007_background_upload_columns.sql` (columns, unique and promotion
   indexes). They must not share one transaction (`ALTER TYPE ... ADD VALUE` limit, see
   the comments in 006). Both are idempotent. Apply them BEFORE the new worker: a worker
   that queries `status = 'awaiting_upload'` on an enum without the value logs an error
   every loop (it is caught, other jobs keep running).
2. Set `SUPABASE_JWT_SECRET` (HS256 projects) and/or `SUPABASE_JWT_USE_JWKS=true`
   (projects using asymmetric signing keys) in `/etc/snipnote-transcription/env`. Check
   which one your project uses in Dashboard -> Project Settings -> JWT Keys. Without
   either, the new mode returns 503 and the legacy path keeps working.
3. In the Supabase dashboard raise the Storage **global file size limit** (Storage ->
   Settings) and, if set, the `recordings` bucket limit to at least ~150 MB
   (`MAX_UPLOAD_BYTES` default is 300 MiB). Otherwise large PUTs fail and jobs expire.
4. Deploy API + worker (`git pull`, `pip install -r requirements.txt` for PyJWT, restart
   both units).
5. Smoke test with a real user token: POST `/jobs` (upload_pending), `curl -X PUT
   --data-binary @file -H 'Content-Type: audio/m4a' -H 'x-upsert: true' "<upload_url>"`,
   watch `journalctl -u snipnote-worker -f` for `promoted to pending`, then the job
   status. Also test an oversize/wrong-size file and an expired job (set a short
   `UPLOAD_PENDING_TTL_SECONDS` on staging).

Operating notes: the worker loop is single-threaded, so while a long job runs, uploads
that finished in the meantime are promoted only after it (status reads `awaiting_upload`
a little longer; processing order is unchanged because jobs queue anyway). Signed URLs
are credentials: they are never logged by this service, but keep them out of any proxy or
app logs you add. Scratch space for big files is `XAI_WORK_DIR` (real disk, about 2x the
file size; stale dirs are swept after 6 h).

Offline verification (no production credentials):

```bash
python -m unittest discover -s tests -v
python -m compileall -q main.py ai_config.py transcription_provider.py transcribe.py jobs.py supabase_client.py xai_single.py auth.py background_upload.py large_audio.py apns.py
```
