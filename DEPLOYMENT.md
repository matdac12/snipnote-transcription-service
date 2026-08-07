# Deployment Instructions

> **Primary host: the `omni` VPS** (`https://api.snipnote.app`).
> Render is kept running only during the client cutover overlap — see
> [Decommissioning Render](#decommissioning-render).

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
ln -sf /etc/nginx/sites-available/snipnote-api /etc/nginx/sites-enabled/snipnote-api
nginx -t && systemctl reload nginx

# TLS — requires api.snipnote.app to already resolve to this box
apt-get install -y certbot python3-certbot-nginx
certbot --nginx -d api.snipnote.app --non-interactive --agree-tos -m <your-email>
```

`certbot` installs a systemd timer that handles renewal automatically.

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

`TRANSCRIPTION_MODEL` in `/etc/snipnote-transcription/env` selects the OpenAI model
(default `gpt-transcribe`). To revert:

```bash
sed -i 's/^TRANSCRIPTION_MODEL=.*/TRANSCRIPTION_MODEL=gpt-4o-transcribe/' /etc/snipnote-transcription/env
systemctl restart snipnote-worker snipnote-api
```

### Memory notes

This box has 2 vCPU / 3.7 GB shared with the Omni assistant, Postgres and two Next.js
apps. `MAX_CONCURRENT_JOBS=1` and `MAX_CHUNK_WORKERS=2` (both below the code defaults of
3 and 5) keep the worker inside its `MemoryMax=1500M`. If you ever see the worker being
restarted by systemd under load, `MEMORY_OPTIMIZATION_PLAN.md` describes the fully
sequential rewrite as the next lever.

### Decommissioning Render

Once the App Store release pointing at `api.snipnote.app` has had a few weeks to roll out:

1. Delete the Render web service and cron job.
2. Delete `render.yaml` and the Render section below.
3. Delete the Supabase **Database → Webhooks** entry that pokes the Render worker
   on-demand — the continuous poller replaces it.

---

## 🗄️ Render Deployment (legacy — remove after cutover)

## ✅ Files Updated

All Python backend files have been updated with full AI processing:

- **main.py** - Added `overview`, `summary`, `actions` to `JobStatusResponse`
- **jobs.py** - Added GPT-4o functions and updated `process_job()` pipeline
- **supabase_client.py** - Added `update_job_with_results()` function
- **requirements.txt** - Already has `openai==1.54.0` ✅

## 🚀 Deployment Steps

### 1. Add Environment Variable

Before deploying, add this to your Render environment variables:

```
OPENAI_API_KEY=sk-your-actual-key-here
```

**How to add on Render:**
1. Go to your Render dashboard
2. Select the snipnote-transcription service
3. Go to "Environment" tab
4. Click "Add Environment Variable"
5. Key: `OPENAI_API_KEY`
6. Value: Your OpenAI API key
7. Save changes

### 2. Deploy Changes

If using Git integration:
```bash
cd /Users/mattia/Documents/Projects/Xcodestuff/SnipNote/snipnote-transcription-service
git add .
git commit -m "feat: add server-side AI processing (overview, summary, actions)"
git push origin main
```

Render will automatically detect the changes and redeploy.

If manual deployment:
1. Go to Render dashboard
2. Select your service
3. Click "Manual Deploy" → "Deploy latest commit"

### 3. Verify Deployment

After deployment, check the Render logs to see:
```
✅ Supabase client initialized
```

## 📋 Complete AI Pipeline

When a job is processed, the worker now:

1. **Download audio** from Supabase Storage
2. **Transcription API** (`gpt-transcribe`) → Transcribe audio
3. **GPT-4o** → Generate 1-sentence overview
4. **GPT-4o** → Generate comprehensive summary
5. **GPT-4o** → Extract action items
6. **Update database** with all results (status=completed)

## 🧪 Testing

After deployment, test with a real audio file:

### On iOS:
1. Share an audio file to SnipNote
2. Ensure "Server Transcription" toggle is ON
3. Tap "Analyze Meeting"
4. Watch it navigate to MeetingDetailView
5. Pull to refresh or wait 15 seconds for polling

### Expected Logs (Render):
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

- **Environment Variable**: Make sure `OPENAI_API_KEY` is set before deployment!
- **Actions Format**: Stored as JSONB in database, converted to iOS `Action` objects automatically
- **Error Handling**: If AI generation fails, the job will fail (not partial completion)
- **Cron Frequency**: Worker runs every 1-2 minutes (check `render.yaml`)

## ✅ Ready to Deploy!

1. Add `OPENAI_API_KEY` environment variable
2. Push changes to GitHub (or manual deploy)
3. Test with a real audio file
4. Monitor Render logs for successful AI processing

All done! 🎉
