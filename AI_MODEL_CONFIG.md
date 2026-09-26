# AI Model Configuration (`ai_model_config`)

Server-side processing for meetings longer than 5 minutes no longer hardcodes models:
- Transcription (`transcribe.py`) goes through `ai_config.create_transcription()`.
- Summaries (`generate_overview`, `generate_summary`, `extract_actions` in `jobs.py`)
  go through `ai_config.create_response()`.

Both read the model (plus reasoning effort and verbosity for summaries) for each
task from the Supabase table `public.ai_model_config`.

The iOS app's `openai-proxy` Edge Function reads **the same rows**. Editing a row
switches the model for both short (on-device) and long (VPS) meetings.

| Column | Meaning |
|---|---|
| `task` | `transcription`, `overview`, `summary`, `actions` (used here); iOS also uses `title`, `text_summary`, `eve_chat`, `actions_report` |
| `model` | OpenAI model ID, e.g. `gpt-6-luna` |
| `reasoning_effort` | e.g. `none`, `low`, `medium`, `high`. NULL sends no `reasoning` parameter (for models without reasoning). Note: `minimal` is rejected by GPT-6 models. |
| `verbosity` | `low`, `medium` or `high`. NULL uses the code default (`low` for overview and summary, unset for actions). |
| `fallback_model` | Retried once if OpenAI returns 400/404 for `model`. NULL disables the retry. |

- The table is created by the migration `supabase/migrations/20260926_create_ai_model_config.sql`
  in the **SnipNote** repo.
- Config is cached for 60 seconds, so an edit applies within a minute. No restart is needed.
- If the table can't be read, the last known config is used. If nothing has been loaded yet,
  the defaults in `ai_config.DEFAULT_CONFIG` (`gpt-6-luna`, effort `low`) are used.
  Summaries never fail because of the config.
- The `transcription` row (seeded `gpt-transcribe`, fallback `gpt-4o-transcribe`) uses only
  `model` and `fallback_model`. `TRANSCRIPTION_MODEL` in `/etc/snipnote-transcription/env`
  is now only the default for when the table can't be read or has no `transcription` row.

---

## For the Claude Code agent on the VPS: deploying this change

You're on the `omni` VPS helping Mattia deploy this. Layout, units and env
file are described in `DEPLOYMENT.md`. Confirm each step with Mattia before
running it. Do not print or echo secrets from `/etc/snipnote-transcription/env`.

### Prerequisite
The `ai_model_config` table must exist in Supabase. Mattia runs the SnipNote
migration from their Mac (see `OPENAI_PROXY_SETUP.md` in the SnipNote repo).
Ask Mattia to confirm this. If the table is missing, the worker still runs on the
built-in defaults, but the Table Editor won't control anything.

Quick check from the VPS, using the service key already in the env file
(the key is not echoed):
```bash
set -a; . /etc/snipnote-transcription/env; set +a
curl -s "$SUPABASE_URL/rest/v1/ai_model_config?select=task,model,reasoning_effort,verbosity,fallback_model" \
  -H "apikey: $SUPABASE_SERVICE_KEY" -H "Authorization: Bearer $SUPABASE_SERVICE_KEY"
```
This should return the seeded rows as JSON. `{"code":"42P01"...}` or
`relation does not exist` means the migration hasn't been run yet.

### Deploy
```bash
cd /opt/snipnote-transcription
git fetch origin
git checkout <branch or main, whichever Mattia says>
git pull
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m py_compile jobs.py ai_config.py
systemctl restart snipnote-api snipnote-worker
systemctl status snipnote-api snipnote-worker --no-pager
```

### Verify
1. Ask Mattia to process a meeting longer than 5 minutes from the app.
2. `journalctl -u snipnote-worker -f` should show one line per AI step
   (transcription has no log line of its own unless it falls back):
   ```
      🤖 summary: model=gpt-6-luna effort=low
      🤖 overview: model=gpt-6-luna effort=low
      🤖 actions: model=gpt-6-luna effort=low
   ```
3. A line like `⚠️ summary: model X rejected (...); retrying with gpt-6-luna` means the
   configured model or a parameter was refused. Look at the quoted OpenAI error and fix
   the row in Supabase. No redeploy is needed. The same applies to
   `⚠️ transcription: model X rejected (...); retrying with gpt-4o-transcribe`.
4. `⚠️ Failed to load ai_model_config` means the worker can't read the table (missing
   table, or a network or key issue). It keeps running on the defaults. Rerun the check above.

### Roll back
- **The model is the problem:** edit the row in Supabase. It applies within 60 seconds, no deploy.
- **The code is the problem:** `git checkout <previous commit>` then restart both units.
  The previous code hardcodes `gpt-5-mini` with `reasoning.effort: "minimal"` for summaries,
  and reads transcription from `TRANSCRIPTION_MODEL` again.
