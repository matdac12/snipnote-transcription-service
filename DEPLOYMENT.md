# Server-Side AI Processing - Deployment Instructions

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
2. **Whisper API** → Transcribe audio
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

Per 1-minute audio file:
- Whisper: $0.006
- GPT-4o Overview: ~$0.0001
- GPT-4o Summary: ~$0.001
- GPT-4o Actions: ~$0.0005
- **Total: ~$0.008**

Per 10-minute audio file:
- Whisper: $0.060
- GPT-4o (all): ~$0.002
- **Total: ~$0.062**

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
