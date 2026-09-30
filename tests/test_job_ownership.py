"""GET /jobs/{id}: AUTH_MODE=off|log|enforce, ownership, audio_url withholding. No network."""
import os
import time
import unittest
from unittest.mock import patch

import jwt
from fastapi.testclient import TestClient

import support
from support import TEST_USER_ID, TEST_MEETING_ID, TEST_AUDIO_URL
import auth
import main

SECRET = 'ownership-test-secret-long-enough-1234567890'
OTHER_USER = '5d4c3b2a-1f0e-4d9c-8b7a-6f5e4d3c2b1a'
JOB_ID = '3f2e1d0c-9b8a-4c7d-8e6f-5a4b3c2d1e0f'
# Fields the iOS JobStatusResponse decodes without a default (TranscriptionJobModels.swift):
IOS_REQUIRED = ['id', 'user_id', 'meeting_id', 'status', 'created_at', 'updated_at']


def token(sub=TEST_USER_ID, exp_delta=3600, secret=SECRET):
    return jwt.encode({'sub': sub, 'aud': 'authenticated', 'iss': 'https://offline.invalid/auth/v1', 'exp': int(time.time()) + exp_delta},
                      secret, algorithm='HS256')


def bearer(tok=None):
    return {'Authorization': f'Bearer {tok or token()}'}


def job_row(**extra):
    return {'id': JOB_ID, 'user_id': TEST_USER_ID, 'meeting_id': TEST_MEETING_ID, 'audio_url': TEST_AUDIO_URL, 'status': 'completed',
            'transcript': 'secret words', 'overview': 'o', 'summary': 's', 'actions': [], 'duration': 61.5, 'progress_percentage': 100,
            'current_stage': 'Complete', 'created_at': '2026-09-30T10:00:00+00:00', 'updated_at': '2026-09-30T10:05:00+00:00',
            'completed_at': '2026-09-30T10:05:00+00:00', 'transcription_provider': 'openai', **extra}


class OwnershipBase(unittest.TestCase):
    mode = None

    def setUp(self):
        env = {'SUPABASE_JWT_SECRET': SECRET, 'SUPABASE_URL': 'https://offline.invalid'}
        if self.mode:
            env['AUTH_MODE'] = self.mode
        patcher = patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        if not self.mode:
            os.environ.pop('AUTH_MODE', None)
        for key in ('SUPABASE_JWT_JWKS_URL', 'SUPABASE_JWT_USE_JWKS', 'SUPABASE_JWT_ISSUER', 'API_KEY'):
            os.environ.pop(key, None)
        self.row = job_row()
        p = patch.object(main, 'get_job', side_effect=lambda job_id: self.row if job_id == JOB_ID else None)
        self.get_job = p.start()
        self.addCleanup(p.stop)
        self.api = TestClient(main.app)
        auth.auth_counters.clear()

    def get(self, headers=None, job_id=JOB_ID):
        return self.api.get(f'/jobs/{job_id}', headers=headers or {})


class LogModeTests(OwnershipBase):
    """The default. Old app builds (no Authorization header) must keep working."""

    def test_default_mode_is_log(self):
        self.assertEqual(auth.auth_mode(), 'log')

    def test_anonymous_old_client_still_gets_everything_it_decodes(self):
        r = self.get()
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        for key in IOS_REQUIRED:
            self.assertIsNotNone(body.get(key), key)
        self.assertEqual((body['user_id'], body['status'], body['transcript'], body['overview'], body['summary']),
                         (TEST_USER_ID, 'completed', 'secret words', 'o', 's'))
        self.assertEqual((body['duration'], body['progress_percentage'], body['current_stage']), (61.5, 100, 'Complete'))
        self.assertIn('audio_url', body)             # key present (Swift field is optional) ...
        self.assertIsNone(body['audio_url'])          # ... but the storage URL is withheld
        self.assertNotIn('storage_path', body)

    def test_anonymous_requests_are_counted(self):
        self.get(); self.get()
        self.assertEqual(auth.auth_counters['GET /jobs:missing'], 2)

    def test_owner_with_token_sees_audio_url_and_is_counted_as_adopted(self):
        body = self.get(bearer()).json()
        self.assertEqual(body['audio_url'], TEST_AUDIO_URL)
        self.assertEqual(auth.auth_counters['GET /jobs:authenticated'], 1)

    def test_valid_token_of_another_user_is_404_even_in_log_mode(self):
        r = self.get(bearer(token(sub=OTHER_USER)))
        self.assertEqual(r.status_code, 404)
        self.assertNotIn('secret words', r.text)

    def test_user_id_case_does_not_matter(self):
        self.row = job_row(user_id=TEST_USER_ID.upper())
        self.assertEqual(self.get(bearer()).json()['audio_url'], TEST_AUDIO_URL)

    def test_bad_or_expired_token_is_treated_as_anonymous(self):
        for headers in (bearer('garbage'), bearer(token(exp_delta=-100)), bearer(token(secret='x' * 40)), {'Authorization': 'Basic abc'}):
            r = self.get(headers)
            self.assertEqual(r.status_code, 200)
            self.assertIsNone(r.json()['audio_url'])
        self.assertEqual(auth.auth_counters['GET /jobs:invalid'], 3)

    def test_auth_backend_outage_does_not_break_polling(self):
        with patch.dict(os.environ, {'SUPABASE_JWT_SECRET': '', 'SUPABASE_AUTH_REMOTE_VERIFY': 'true'}), \
                patch.object(auth, 'verify_remote', side_effect=auth.AuthError(503, 'down')):
            r = self.get(bearer())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(auth.auth_counters['GET /jobs:unavailable'], 1)

    def test_unknown_job_and_malformed_id_are_404_without_touching_the_db(self):
        self.assertEqual(self.get(job_id='00000000-0000-4000-8000-000000000000').status_code, 404)
        self.get_job.reset_mock()
        for bad in ('job', "x'%20or%201=1", '123'):
            self.assertEqual(self.get(job_id=bad).status_code, 404)
        self.get_job.assert_not_called()

    def test_database_errors_are_not_echoed(self):
        self.get_job.side_effect = Exception('connection to db.internal.example:5432 refused; password=hunter2')
        r = self.get()
        self.assertEqual(r.status_code, 500)
        self.assertNotIn('hunter2', r.text)
        self.assertEqual(r.json(), {'detail': 'Failed to retrieve job'})

    def test_upload_mode_job_fields_still_present(self):
        self.row = job_row(status='awaiting_upload', audio_url=None, expected_bytes=1000, upload_deadline='2026-09-30T16:00:00+00:00')
        body = self.get().json()
        self.assertEqual((body['status'], body['expected_bytes']), ('awaiting_upload', 1000))


class EnforceModeTests(OwnershipBase):
    mode = 'enforce'

    def test_no_token_is_401(self):
        r = self.get()
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.headers.get('www-authenticate'), 'Bearer')
        self.assertNotIn('secret words', r.text)
        self.get_job.assert_not_called()    # not even looked up

    def test_invalid_or_expired_token_is_401(self):
        for headers in (bearer('garbage'), bearer(token(exp_delta=-100)), bearer(token(secret='x' * 40))):
            self.assertEqual(self.get(headers).status_code, 401)

    def test_owner_gets_the_job_including_audio_url(self):
        r = self.get(bearer())
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.json()['user_id'], r.json()['audio_url']), (TEST_USER_ID, TEST_AUDIO_URL))

    def test_other_user_gets_404_identical_to_a_missing_job(self):
        other = self.get(bearer(token(sub=OTHER_USER)))
        missing = self.get(bearer(), job_id='00000000-0000-4000-8000-000000000000')
        self.assertEqual((other.status_code, other.json()), (404, missing.json()))

    def test_auth_backend_outage_fails_closed_503(self):
        with patch.dict(os.environ, {'SUPABASE_JWT_SECRET': ''}), patch.object(auth, 'verify_remote', side_effect=auth.AuthError(503, 'down')):
            self.assertEqual(self.get(bearer()).status_code, 503)


class OffModeTests(OwnershipBase):
    mode = 'off'

    def test_tokens_are_ignored_but_audio_url_is_still_withheld(self):
        for headers in ({}, bearer(), bearer(token(sub=OTHER_USER)), bearer('garbage')):
            r = self.get(headers)
            self.assertEqual(r.status_code, 200)
            self.assertIsNone(r.json()['audio_url'])
            self.assertEqual(r.json()['user_id'], TEST_USER_ID)
        self.assertEqual(dict(auth.auth_counters), {})


if __name__ == '__main__':
    unittest.main()
