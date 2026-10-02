import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch
from fastapi.testclient import TestClient
import support
import main
import jobs
import supabase_client


class JobTests(unittest.TestCase):
    def setUp(self):
        self.api = TestClient(main.app)
        self.payload = {'user_id': 'user', 'meeting_id': 'meeting', 'audio_url': 'https://offline.invalid/audio'}
        self.inserted = []
        self.table_patch = patch.object(supabase_client.supabase, 'table')
        table = self.table_patch.start().return_value
        def insert(data):
            self.inserted.append(data)
            table.execute.return_value = SimpleNamespace(data=[{**data, 'id': 'job', 'created_at': '2026-09-30', 'updated_at': '2026-09-30'}])
            return table
        table.insert.side_effect = insert
        self.addCleanup(self.table_patch.stop)

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
            supabase_client.create_job('user', 'meeting', transcription_provider='unknown')
        self.assertEqual(self.inserted, [])

    def run_worker(self, chunked=False, provider='xai'):
        job = {**self.payload, 'id': 'job', 'is_chunked': chunked, 'total_chunks': 2, 'duration': 30, 'language': 'it'}
        if provider is not None:
            job['transcription_provider'] = provider
        with ExitStack() as stack:
            mocks = {}
            for name, result in {'download_audio': b'audio', 'download_chunk_from_storage': b'audio', 'get_audio_chunks': [
                {'id': 'chunk1', 'chunk_index': 0, 'file_path': 'one'}, {'id': 'chunk2', 'chunk_index': 1, 'file_path': 'two'}],
                'get_job': {'status': 'processing'}, 'reset_chunk_transcripts': None, 'update_job_status': None, 'update_job_progress': None, 'update_chunk_transcript': None,
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

    def test_synchronous_endpoint_routes_provider(self):
        with patch.object(main, 'transcribe_audio', return_value={'transcript': 'text', 'duration': 1}) as transcribe:
            response = self.api.post('/transcribe', files={'file': ('meeting.m4a', b'audio', 'audio/mp4')}, data={'transcription_provider': 'xai', 'language': 'it'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(transcribe.call_args.kwargs.get('provider'), 'xai')
            response = self.api.post('/transcribe', files={'file': ('meeting.m4a', b'audio')}, data={'transcription_provider': 'unknown'})
            self.assertEqual(response.status_code, 422)

    def test_synchronous_empty_provider_is_rejected(self):
        with patch.object(main, 'transcribe_audio') as transcribe:
            response = self.api.post('/transcribe', files={'file': ('meeting.m4a', b'audio')}, data={'transcription_provider': ''})
            self.assertEqual(response.status_code, 422)
            transcribe.assert_not_called()


class ReliabilityTests(unittest.TestCase):
    def test_newer_job_stops_old_retry_before_using_shared_cache(self):
        current = {'id': 'older', 'meeting_id': 'meeting', 'status': 'pending', 'retry_count': 1}
        with patch.object(jobs, 'get_job', return_value=current), patch.object(jobs, 'get_latest_job_id', return_value='newer'), patch.object(jobs, 'update_job_status') as status:
            with self.assertRaises(jobs.TranscriptionCancelled):
                jobs.ensure_job_active('older')
            self.assertEqual(status.call_args.args[:2], ('older', 'failed'))

    def test_deleted_job_does_not_start_or_requeue(self):
        with patch.object(jobs, 'get_job', return_value=None), patch.object(jobs, 'update_job_status') as status, patch.object(jobs, 'increment_retry_count') as retry, patch.object(jobs, 'download_audio') as download:
            jobs.process_job({'id': 'deleted', 'meeting_id': 'meeting', 'audio_url': 'url'})
            status.assert_not_called()
            retry.assert_not_called()
            download.assert_not_called()

    def test_cancelled_job_does_not_start_chunks(self):
        with patch.object(jobs, 'get_job', return_value={'status': 'failed'}), patch.object(jobs, 'update_job_status') as status, patch.object(jobs, 'increment_retry_count') as retry:
            jobs.process_chunked_job({'id': 'cancelled', 'meeting_id': 'meeting'})
            status.assert_not_called()
            retry.assert_not_called()

    def test_retry_reuses_completed_chunk_without_downloading(self):
        chunk = {'id': 'chunk', 'chunk_index': 0, 'file_path': 'one', 'transcribed': True, 'transcript': 'saved Italian transcript'}
        with patch.object(jobs, 'get_job', return_value={'status': 'processing'}), patch.object(jobs, 'download_chunk_from_storage') as download:
            result = jobs.process_single_chunk(chunk, 2, 'it', 'xai', job_id='job', reuse_completed=True)
            self.assertEqual(result['transcript'], 'saved Italian transcript')
            download.assert_not_called()

    def test_failure_still_saves_other_successful_chunks(self):
        chunks = [{'id': 'bad', 'chunk_index': 0}, {'id': 'good', 'chunk_index': 1}]
        def process(chunk, *args, **kwargs):
            if chunk['id'] == 'bad': raise RuntimeError('HTTP 503')
            return {'chunk_id': 'good', 'chunk_index': 1, 'transcript': 'saved'}
        with ExitStack() as stack:
            for name, value in {'get_job': {'status': 'processing'}, 'reset_chunk_transcripts': None, 'get_audio_chunks': chunks, 'update_job_status': None, 'update_job_progress': None, 'update_chunks_processed': None}.items():
                stack.enter_context(patch.object(jobs, name, return_value=value))
            stack.enter_context(patch.object(jobs, 'process_single_chunk', side_effect=process))
            saved = stack.enter_context(patch.object(jobs, 'update_chunk_transcript'))
            retry = stack.enter_context(patch.object(jobs, 'increment_retry_count'))
            jobs.process_chunked_job({'id': 'job', 'meeting_id': 'meeting', 'total_chunks': 2})
            saved.assert_called_once_with('good', 'saved')
            retry.assert_called_once()

    def test_new_job_discards_previous_job_chunk_cache(self):
        chunk = {'id': 'chunk', 'chunk_index': 0, 'file_path': 'one', 'transcribed': True, 'transcript': 'old provider text'}
        def reset(meeting_id):
            self.assertEqual(meeting_id, 'meeting')
            chunk.update(transcribed=False, transcript=None)
        with ExitStack() as stack:
            for name, value in {'get_job': {'status': 'processing'}, 'get_audio_chunks': [chunk], 'update_job_status': None, 'update_job_progress': None, 'update_chunk_transcript': None, 'update_chunks_processed': None, 'increment_retry_count': None}.items():
                stack.enter_context(patch.object(jobs, name, return_value=value))
            stack.enter_context(patch.object(jobs, 'reset_chunk_transcripts', side_effect=reset))
            stack.enter_context(patch.object(jobs, 'download_chunk_from_storage', return_value=b'audio'))
            def transcribe(*args, **kwargs):
                self.assertFalse(chunk['transcribed'])
                raise RuntimeError('HTTP 503')
            create = stack.enter_context(patch.object(jobs, 'transcribe_audio', side_effect=transcribe))
            jobs.process_chunked_job({'id': 'new-job', 'meeting_id': 'meeting', 'total_chunks': 1, 'transcription_provider': 'xai'})
            self.assertEqual(create.call_count, 1)
            self.assertIsNone(chunk['transcript'])

    def test_all_empty_meeting_fails_without_generating_summary(self):
        with ExitStack() as stack:
            for name, value in {'get_job': {'status': 'processing'}, 'download_audio': b'audio', 'update_job_status': None, 'update_job_progress': None, 'transcribe_audio': {'transcript': '', 'duration': 3}}.items():
                stack.enter_context(patch.object(jobs, name, return_value=value))
            summary = stack.enter_context(patch.object(jobs, 'generate_summary'))
            retry = stack.enter_context(patch.object(jobs, 'increment_retry_count'))
            jobs.process_job({'id': 'job', 'meeting_id': 'meeting', 'audio_url': 'url'})
            summary.assert_not_called()
            retry.assert_not_called()


class CancellationRaceTests(unittest.TestCase):
    def test_cancellation_cannot_be_overwritten_by_worker_updates(self):
        # The row becomes terminal after the worker's last read, before UPDATE.
        class Query:
            def __init__(self):
                self.statuses = None
            def update(self, data):
                self.data = data
                return self
            def eq(self, *args): return self
            def in_(self, column, values):
                if column == 'status': self.statuses = values
                return self
            def execute(self):
                if self.statuses is None or 'failed' in self.statuses:
                    return SimpleNamespace(data=[{'id': 'job', **self.data}])
                return SimpleNamespace(data=[])
        operations = [
            lambda: supabase_client.update_job_status('job', 'processing'),
            lambda: supabase_client.update_job_progress('job', 50, 'working'),
            lambda: supabase_client.update_job_with_results('job', 'text', 'overview', 'summary', [], 3),
            lambda: supabase_client.increment_retry_count('job', 'HTTP 503'),
        ]
        for operation in operations:
            with self.subTest(operation=operation), patch.object(supabase_client.supabase, 'table', return_value=Query()), patch.object(supabase_client, 'get_job', return_value={'status': 'processing', 'retry_count': 1}):
                with self.assertRaises(Exception) as error:
                    operation()
                self.assertFalse(jobs.is_retryable_error(error.exception))
