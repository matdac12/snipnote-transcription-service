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

# Realistic ids: the API and the worker now validate them (UUIDs, storage paths under the owner's folder).
TEST_USER_ID = '8a1c2f3e-4b5d-4e6f-8a7b-9c0d1e2f3a4b'
TEST_MEETING_ID = '0b9e8d7c-6a5f-4e3d-9c2b-1a0f9e8d7c6b'
TEST_AUDIO_URL = f'https://offline.invalid/storage/v1/object/public/recordings/{TEST_USER_ID}/{TEST_MEETING_ID}.m4a'


def chunk_path_for(index: int) -> str:
    return f'{TEST_USER_ID}/{TEST_MEETING_ID}_chunk_{index}.m4a'
