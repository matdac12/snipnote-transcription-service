"""Offline boundaries: never initialize a client with production credentials."""
import os
from unittest.mock import MagicMock, patch

os.environ['OPENAI_API_KEY'] = 'offline-openai-key'
os.environ['SUPABASE_URL'] = 'https://offline.invalid'
os.environ['SUPABASE_SERVICE_KEY'] = 'offline-service-key'
# jobs.py calls apns.notify_stage; keep the Live Activity notifier (thread, Supabase writes) off in the job tests.
os.environ['LIVE_ACTIVITY_ENABLED'] = 'false'
with patch('supabase.create_client', return_value=MagicMock()):
    import supabase_client
