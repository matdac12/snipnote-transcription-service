import os
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch
from fastapi.testclient import TestClient
import support
from support import TEST_USER_ID, TEST_MEETING_ID, TEST_AUDIO_URL, chunk_path_for
import main
import jobs
import supabase_client


class JobTests(unittest.TestCase):
    def setUp(self):
        self.api = TestClient(main.app)
        self.payload = {'user_id': TEST_USER_ID, 'meeting_id': TEST_MEETING_ID, 'audio_url': TEST_AUDIO_URL}
        self.inserted = []
        self.table_patch = patch.object(supabase_client.supabase, 'table')
        table = self.table_patch.start().return_value
        def insert(data):
            self.inserted.append(data)
            table.execute.return_value = SimpleNamespace(data=[{**data, 'id': 'job', 'created_at': '2026-09-30', 'updated_at': '2026-09-30'}])
            return table
        table.insert.side_effect = insert
        self.addCleanup(self.table_patch.stop)
        # These tests cover the chunked xAI path; single-request has its own tests.
        env = patch.dict(os.environ, {'XAI_SINGLE_REQUEST_ENABLED': 'false'})
        env.start()
        self.addCleanup(env.stop)

    def test_regular_job_persists_xai(self):
        response = self.api.post('/jobs', json={**self.payload, 'transcription_provider': 'xai'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.inserted[0].get('transcription_provider'), 'xai')

    def test_chunked_job_persists_xai(self):
        response = self.api.post('/jobs', json={**self.payload, 'audio_url': None, 'is_chunked': True, 'total_chunks': 2, 'transcription_provider': 'xai'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.inserted[0].get('transcription_provider'), 'xai')

    def test_legacy_job_defaults_openai(self):
        self.assertEqual(self.api.post('/jobs', json=self.payload).status_code, 200)
        self.assertEqual(self.inserted[0].get('transcription_provider'), 'openai')
        with patch.object(main, 'get_job', return_value={**self.payload, 'id': 'job', 'status': 'pending', 'created_at': '2026-09-30', 'updated_at': '2026-09-30'}):
            self.assertEqual(self.api.get('/jobs/job').json().get('transcription_provider'), 'openai')

    def test_explicit_invalid_provider_returns_422(self):
        for value in ['unknown', '', None]:
            self.assertEqual(self.api.post('/jobs', json={**self.payload, 'transcription_provider': value}).status_code, 422)
        self.assertEqual(self.inserted, [])

    def test_invalid_provider_rejected_before_database(self):
        with self.assertRaises(ValueError):
            supabase_client.create_job(TEST_USER_ID, TEST_MEETING_ID, transcription_provider='unknown')
        self.assertEqual(self.inserted, [])

    def run_worker(self, chunked=False, provider='xai'):
        job = {**self.payload, 'id': 'job', 'is_chunked': chunked, 'total_chunks': 2, 'duration': 30, 'language': 'it'}
        if provider is not None:
            job['transcription_provider'] = provider
        with ExitStack() as stack:
            mocks = {}
            for name, result in {'download_audio': b'audio', 'download_chunk_from_storage': b'audio', 'get_audio_chunks': [
                {'id': 'chunk1', 'chunk_index': 0, 'file_path': chunk_path_for(0)}, {'id': 'chunk2', 'chunk_index': 1, 'file_path': chunk_path_for(1)}],
                'update_job_status': None, 'update_job_progress': None, 'update_chunk_transcript': None,
                'update_chunks_processed': None, 'increment_retry_count': None, 'update_job_with_results': None,
                'transcribe_audio': {'transcript': 'transcript', 'duration': 30},
                'generate_summary': 'summary', 'generate_overview': 'overview', 'extract_actions': []}.items():
                mocks[name] = stack.enter_context(patch.object(jobs, name, return_value=result))
            jobs.process_job(job)
            calls = mocks['transcribe_audio'].call_args_list
            self.assertEqual(len(calls), 2 if chunked else 1)
            self.assertEqual([call.kwargs.get('provider') for call in calls], [provider or 'openai'] * len(calls))
            mocks['generate_summary'].assert_called_once_with('transcript\ntranscript' if chunked else 'transcript')
            mocks['generate_overview'].assert_called_once_with('summary')
            mocks['extract_actions'].assert_called_once_with('summary')
            mocks['increment_retry_count'].assert_not_called()
            mocks['update_job_with_results'].assert_called_once()

    def test_regular_worker_uses_stored_provider(self):
        self.run_worker()

    def test_parallel_chunks_use_stored_provider(self):
        self.run_worker(chunked=True)

    def test_old_workers_default_openai(self):
        self.run_worker(provider=None)
        self.run_worker(chunked=True, provider=None)

    def test_synchronous_endpoint_is_gone(self):
        # /transcribe was unauthenticated and ran transcription inside the API process.
        with patch.object(jobs, 'transcribe_audio') as transcribe:
            for method in ('post', 'get', 'put'):
                response = getattr(self.api, method)('/transcribe')
                self.assertEqual(response.status_code, 410, method)
            response = self.api.post('/transcribe', files={'file': ('meeting.m4a', b'audio', 'audio/mp4')}, data={'transcription_provider': 'xai'})
            self.assertEqual(response.status_code, 410)
            self.assertIn('POST /jobs', response.json()['detail'])
            transcribe.assert_not_called()
        self.assertFalse(hasattr(main, 'transcribe_audio'), 'main must not import the transcription code any more')

    def test_no_cors_by_default_and_wildcard_is_ignored(self):
        self.assertEqual([m for m in main.app.user_middleware if 'CORS' in m.cls.__name__], [])
        with patch.dict(os.environ, {'CORS_ALLOWED_ORIGINS': ''}):
            self.assertEqual(main.cors_allowed_origins(), [])
        with patch.dict(os.environ, {'CORS_ALLOWED_ORIGINS': ' https://a.example , *, https://b.example '}):
            self.assertEqual(main.cors_allowed_origins(), ['https://a.example', 'https://b.example'])
        response = self.api.get('/', headers={'Origin': 'https://evil.example'})
        self.assertNotIn('access-control-allow-origin', response.headers)
