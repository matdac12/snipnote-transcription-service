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

Offline verification (no production credentials):

```bash
python -m unittest discover -s tests -v
python -m compileall -q main.py ai_config.py transcription_provider.py transcribe.py jobs.py supabase_client.py
```

## Background upload rollout (approval pending)

Keep the existing worker and its concurrency unchanged. New tables and service-only
RPCs are owned by the app repository's Supabase migrations; do not copy migrations
into this repository. Add `BACKGROUND_UPLOAD_ENABLED=false` and an empty
`BACKGROUND_UPLOAD_ALLOWED_USERS` to the existing environment. Install the separate
`deploy/snipnote-upload-reconciler.service` only after migration/deployment approval.
It scans at most 100 sessions every 20 seconds and never invokes transcription.
Expired sessions retain objects for retry; this process deletes no audio. Verified
and queued files remain referenced by ordinary chunk/job metadata.

Local rehearsal uses PostgreSQL 17 and `tests/upload_legacy_fixture.sql` (synthetic
legacy schema only), followed by the two app migrations, then:

```bash
python3.12 -m venv .venv-test
.venv-test/bin/pip install -r requirements-test.txt
UPLOAD_TEST_DATABASE_URL='<disposable-local-dsn>' .venv-test/bin/python -m unittest discover -s tests -v
```

Never set this test DSN to production. Migration scripts have 5-second lock and
30-second statement timeouts. Anonymous/authenticated roles have no upload table
privileges and cannot execute either RPC. The service verifies identity through
Supabase Auth and checks meeting/session ownership before signing or reading.
