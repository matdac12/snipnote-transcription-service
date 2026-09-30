"""Legacy POST /jobs: identity from JWT, shape checks, ownership rows, duplicates, per-user cap. No network."""
import os
import time
import unittest
from unittest.mock import patch

import jwt
from fastapi.testclient import TestClient

import support
from support import TEST_USER_ID, TEST_MEETING_ID, TEST_AUDIO_URL, chunk_path_for
import auth
import main
import supabase_client as sc

SECRET = 'policy-test-secret-long-enough-1234567890'
OTHER_USER = '5d4c3b2a-1f0e-4d9c-8b7a-6f5e4d3c2b1a'
STORAGE_PATH = f'{TEST_USER_ID}/{TEST_MEETING_ID}.m4a'


def bearer(sub=TEST_USER_ID, exp_delta=3600):
    tok = jwt.encode({'sub': sub, 'aud': 'authenticated', 'iss': 'https://offline.invalid/auth/v1', 'exp': int(time.time()) + exp_delta},
                     SECRET, algorithm='HS256')
    return {'Authorization': f'Bearer {tok}'}


class PolicyBase(unittest.TestCase):
    mode = None

    def setUp(self):
        patcher = patch.dict(os.environ, {'SUPABASE_JWT_SECRET': SECRET, 'SUPABASE_URL': 'https://offline.invalid'})
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ('AUTH_MODE', 'SUPABASE_JWT_JWKS_URL', 'SUPABASE_JWT_USE_JWKS', 'SUPABASE_JWT_ISSUER', 'API_KEY', 'MAX_ACTIVE_JOBS_PER_USER',
                    'MAX_JOB_DURATION_SECONDS', 'SERVER_MINUTES_DEBIT_ENABLED'):
            os.environ.pop(key, None)
        if self.mode:
            os.environ['AUTH_MODE'] = self.mode
        self.created = []
        self.recording = {'file_path': STORAGE_PATH, 'duration': 120, 'file_size': 1000}
        self.chunks = [{'chunk_index': 0, 'duration_seconds': 60, 'file_size': 10}, {'chunk_index': 1, 'duration_seconds': 60, 'file_size': 10}]
        self.active = []
        self.lookups = []

        def create_job(**kw):
            self.created.append(kw)
            return {'id': 'new-job', 'status': 'pending', 'created_at': '2026-09-30T10:00:00+00:00'}

        def recording(user, meeting):
            self.lookups.append(('recordings', user, meeting))
            if isinstance(self.recording, Exception):
                raise self.recording
            return self.recording

        def chunks(user, meeting):
            self.lookups.append(('audio_chunks', user, meeting))
            return self.chunks

        for target, name, fn in ((main, 'create_job', create_job), (sc, 'get_recording_row', recording), (sc, 'get_chunk_rows', chunks),
                                 (sc, 'list_active_jobs', lambda user, limit=200: self.active)):
            p = patch.object(target, name, side_effect=fn)
            p.start()
            self.addCleanup(p.stop)
        self.api = TestClient(main.app)
        auth.auth_counters.clear()
        self.body = {'user_id': TEST_USER_ID.upper(), 'meeting_id': TEST_MEETING_ID.upper(), 'audio_url': TEST_AUDIO_URL, 'language': 'it'}

    def post(self, body=None, headers=None):
        return self.api.post('/jobs', json=self.body if body is None else body, headers=headers or {})

    def chunked(self, **extra):
        return {'user_id': TEST_USER_ID, 'meeting_id': TEST_MEETING_ID, 'is_chunked': True, 'total_chunks': 2, 'duration': 120.0, **extra}


class LegacyClientCompatTests(PolicyBase):
    """Old builds: no Authorization header, uppercase uuidString ids, upload already done."""

    def test_regular_job_response_shape_and_stored_ids(self):
        r = self.post()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {'job_id': 'new-job', 'status': 'pending', 'created_at': '2026-09-30T10:00:00+00:00'})
        kw = self.created[0]
        self.assertEqual((kw['user_id'], kw['meeting_id'], kw['audio_url']), (TEST_USER_ID, TEST_MEETING_ID, TEST_AUDIO_URL))
        self.assertEqual(kw['transcription_provider'], 'openai')

    def test_chunked_job(self):
        r = self.post(self.chunked(transcription_provider='xai'))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((self.created[0]['is_chunked'], self.created[0]['total_chunks'], self.created[0]['transcription_provider']), (True, 2, 'xai'))

    def test_anonymous_request_is_counted(self):
        self.post()
        self.assertEqual(auth.auth_counters['POST /jobs:missing'], 1)


class IdentityTests(PolicyBase):
    def test_token_user_is_used_and_body_user_id_is_optional(self):
        body = {k: v for k, v in self.body.items() if k != 'user_id'}
        r = self.post(body, bearer())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.created[0]['user_id'], TEST_USER_ID)

    def test_body_user_id_mismatching_a_valid_token_is_403(self):
        r = self.post({**self.body, 'user_id': OTHER_USER}, bearer())
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.created, [])

    def test_token_of_another_user_cannot_create_jobs_for_the_body_user(self):
        self.assertEqual(self.post(self.body, bearer(OTHER_USER)).status_code, 403)

    def test_no_token_and_no_user_id_is_422(self):
        self.assertEqual(self.post({k: v for k, v in self.body.items() if k != 'user_id'}).status_code, 422)

    def test_invalid_token_in_log_mode_falls_back_to_body_identity(self):
        r = self.post(self.body, {'Authorization': 'Bearer garbage'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(auth.auth_counters['POST /jobs:invalid'], 1)

    def test_enforce_requires_a_token(self):
        os.environ['AUTH_MODE'] = 'enforce'
        r = self.post()
        self.assertEqual((r.status_code, r.headers.get('www-authenticate')), (401, 'Bearer'))
        self.assertEqual(self.created, [])
        self.assertEqual(self.post(self.body, bearer()).status_code, 200)
        self.assertEqual(self.post(self.body, {'Authorization': 'Bearer garbage'}).status_code, 401)

    def test_off_mode_ignores_tokens(self):
        os.environ['AUTH_MODE'] = 'off'
        self.assertEqual(self.post(self.body, bearer(OTHER_USER)).status_code, 200)
        self.assertEqual(dict(auth.auth_counters), {})


class ShapeTests(PolicyBase):
    def test_always_on_validation(self):
        bad = [
            {**self.body, 'user_id': 'user'}, {**self.body, 'meeting_id': 'meeting'},
            {**self.chunked(), 'total_chunks': 0}, {**self.chunked(), 'total_chunks': 100000},
            {**self.chunked(), 'duration': -5}, {**self.chunked(), 'duration': 0}, {**self.chunked(), 'duration': 13 * 3600},
        ]
        for body in bad:
            self.assertEqual(self.post(body).status_code, 422, body)
        self.assertEqual(self.created, [])

    def test_regular_job_needs_a_valid_audio_url(self):
        for url in (None, '', 'http://169.254.169.254/latest', f'https://evil.example/storage/v1/object/public/recordings/{TEST_USER_ID}/a.m4a',
                    f'https://offline.invalid/storage/v1/object/public/recordings/{OTHER_USER}/a.m4a'):
            r = self.post({**self.body, 'audio_url': url})
            self.assertEqual(r.status_code, 400, url)
        self.assertEqual(self.created, [])

    def test_limits_are_configurable(self):
        with patch.dict(os.environ, {'MAX_JOB_DURATION_SECONDS': '100'}):
            self.assertEqual(self.post(self.chunked(duration=101)).status_code, 422)
            self.assertEqual(self.post(self.chunked(duration=100)).status_code, 200)

    def test_db_errors_are_not_echoed(self):
        with patch.object(main, 'create_job', side_effect=Exception('password=hunter2 host=db.internal')):
            r = self.post()
        self.assertEqual((r.status_code, r.json()), (500, {'detail': 'Failed to create job'}))


class OwnershipRowTests(PolicyBase):
    def test_log_mode_only_logs_violations(self):
        for label, rec in (('no row', None), ('mismatch', {'file_path': f'{TEST_USER_ID}/other.m4a', 'duration': 5})):
            self.recording = rec
            with patch.object(main.job_policy, '_log') as log:
                self.assertEqual(self.post().status_code, 200, label)
            self.assertIn('would reject', log.call_args.args[0])
        self.assertEqual(len(self.created), 2)

    def test_log_mode_is_silent_when_everything_matches(self):
        with patch.object(main.job_policy, '_log') as log:
            self.assertEqual(self.post().status_code, 200)
            self.assertEqual(self.post(self.chunked()).status_code, 200)
        log.assert_not_called()

    def test_enforce_rejects_missing_or_mismatching_rows(self):
        os.environ['AUTH_MODE'] = 'enforce'
        self.recording = None
        self.assertEqual(self.post(self.body, bearer()).status_code, 404)
        self.recording = {'file_path': f'{TEST_USER_ID}/other.m4a', 'duration': 5}
        self.assertEqual(self.post(self.body, bearer()).status_code, 404)
        self.chunks = []
        self.assertEqual(self.post(self.chunked(), bearer()).status_code, 404)
        self.chunks = [{'chunk_index': 0, 'duration_seconds': 1}]          # 1 of 2 uploaded
        self.assertEqual(self.post(self.chunked(), bearer()).status_code, 404)
        self.assertEqual(self.created, [])

    def test_enforce_accepts_matching_rows(self):
        os.environ['AUTH_MODE'] = 'enforce'
        self.assertEqual(self.post(self.body, bearer()).status_code, 200)
        self.assertEqual(self.post(self.chunked(), bearer()).status_code, 200)
        self.assertEqual(self.lookups, [('recordings', TEST_USER_ID, TEST_MEETING_ID), ('audio_chunks', TEST_USER_ID, TEST_MEETING_ID)])

    def test_path_comparison_ignores_case(self):
        os.environ['AUTH_MODE'] = 'enforce'
        self.recording = {'file_path': STORAGE_PATH.upper(), 'duration': 1}
        self.assertEqual(self.post(self.body, bearer()).status_code, 200)

    def test_off_mode_does_not_look_at_the_tables(self):
        os.environ['AUTH_MODE'] = 'off'
        self.recording = None
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(self.lookups, [])

    def test_failed_lookup_never_blocks_even_in_enforce(self):
        os.environ['AUTH_MODE'] = 'enforce'
        self.recording = Exception('relation "recordings" does not exist')
        self.assertEqual(self.post(self.body, bearer()).status_code, 200)


class DuplicateAndCapTests(PolicyBase):
    def test_existing_active_job_of_the_same_meeting_is_returned(self):
        self.active = [{'id': 'existing', 'meeting_id': TEST_MEETING_ID, 'status': 'processing', 'created_at': '2026-09-30T09:00:00+00:00'}]
        r = self.post()
        self.assertEqual(r.json(), {'job_id': 'existing', 'status': 'processing', 'created_at': '2026-09-30T09:00:00+00:00'})
        self.assertEqual(self.created, [])

    def test_awaiting_upload_job_is_not_handed_to_a_legacy_create(self):
        self.active = [{'id': 'upl', 'meeting_id': TEST_MEETING_ID, 'status': 'awaiting_upload', 'created_at': 'x'}]
        self.assertEqual(self.post().json()['job_id'], 'new-job')

    def test_per_user_cap(self):
        self.active = [{'id': f'j{i}', 'meeting_id': f'{i:08d}-0000-4000-8000-000000000000', 'status': 'pending', 'created_at': 'x'} for i in range(20)]
        r = self.post()
        self.assertEqual((r.status_code, r.headers.get('retry-after')), (429, '60'))
        self.assertEqual(self.created, [])
        with patch.dict(os.environ, {'MAX_ACTIVE_JOBS_PER_USER': '0'}):
            self.assertEqual(self.post().status_code, 200)
        with patch.dict(os.environ, {'MAX_ACTIVE_JOBS_PER_USER': '21'}):
            self.assertEqual(self.post().status_code, 200)
        self.active = self.active[:19]
        self.assertEqual(self.post().status_code, 200)

    def test_cap_is_per_user_query(self):
        seen = []
        with patch.object(sc, 'list_active_jobs', side_effect=lambda user, limit=200: seen.append(user) or []):
            self.post()
        self.assertEqual(seen, [TEST_USER_ID])

    def test_failed_active_lookup_never_blocks(self):
        with patch.object(sc, 'list_active_jobs', side_effect=Exception('boom')):
            self.assertEqual(self.post().status_code, 200)


if __name__ == '__main__':
    unittest.main()
