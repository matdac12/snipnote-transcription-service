"""Background upload: JWT auth, job creation, promotion/expiry, large-file segmenting. No network."""
import base64
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import jwt
from fastapi.testclient import TestClient

import support
import auth
import background_upload as bu
import jobs
import large_audio
import main
import supabase_client as sc

SECRET = 'test-jwt-secret-with-enough-length-for-hs256'
USER = str(uuid.uuid4())
MEETING = str(uuid.uuid4())
SIGNED_TOKEN_SECRET = 'storage-signing-secret-not-the-user-secret'
BASE = 'https://offline.invalid'


def make_token(sub=USER, secret=SECRET, exp_delta=3600, aud='authenticated', iss=f'{BASE}/auth/v1', alg='HS256', **extra):
    claims = {'sub': sub, 'aud': aud, 'iss': iss, 'exp': int(time.time()) + exp_delta, 'role': 'authenticated', **extra}
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, secret, algorithm=alg)


class FakeStorageAndDB:
    """In-memory stand-in for the supabase_client functions background_upload uses.

    transition/update are real compare-and-set operations guarded by a lock, so the
    race tests exercise the same contract the SQL `UPDATE ... WHERE status=` gives.
    """
    def __init__(self):
        self.rows = {}
        self.objects = {}  # path -> size (None = upload in progress)
        self.deleted = []
        self.sign_calls = []
        self.lock = threading.Lock()
        self.fail_storage = False
        self.fail_sign = False

    # --- functions patched onto supabase_client ---
    def find_upload_job(self, user_id, meeting_id):
        rows = [r for r in self.rows.values() if r['user_id'] == user_id and r['meeting_id'] == meeting_id
                and r.get('storage_path') and r['status'] != 'failed']
        return dict(sorted(rows, key=lambda r: r['created_at'])[-1]) if rows else None

    def insert_upload_job(self, data):
        with self.lock:
            if self.find_upload_job(data['user_id'], data['meeting_id']):
                raise Exception('duplicate key value violates unique constraint (23505)')
            row = {**data, 'id': str(uuid.uuid4()), 'created_at': datetime.now(timezone.utc).isoformat()}
            self.rows[row['id']] = row
            return dict(row)

    def transition_job_status(self, job_id, from_status, to_status, **fields):
        with self.lock:
            row = self.rows.get(job_id)
            if not row or row['status'] != from_status:
                return None
            row.update(fields, status=to_status)
            return dict(row)

    def update_awaiting_job(self, job_id, **fields):
        with self.lock:
            row = self.rows.get(job_id)
            if not row or row['status'] != 'awaiting_upload':
                return None
            row.update(fields)
            return dict(row)

    def list_awaiting_upload_jobs(self, limit=200):
        with self.lock:
            return [dict(r) for r in sorted(self.rows.values(), key=lambda r: r['created_at']) if r['status'] == 'awaiting_upload']

    def create_signed_upload_url(self, path, upsert=True):
        if self.fail_sign:
            raise sc.StorageError('signed upload URL request returned HTTP 500')
        self.sign_calls.append(path)
        return {'upload_url': f'{BASE}/storage/v1/object/upload/sign/recordings/{path}?token=SECRET-TOKEN-{len(self.sign_calls)}',
                'expires_at': time.time() + 7200}

    def get_storage_object_size(self, path):
        if self.fail_storage:
            raise sc.StorageError('storage list returned HTTP 500')
        return self.objects.get(path)

    def delete_storage_object(self, path):
        self.deleted.append(path)
        self.objects.pop(path, None)

    def patch(self, stack):
        for name in ['find_upload_job', 'insert_upload_job', 'transition_job_status', 'update_awaiting_job',
                     'list_awaiting_upload_jobs', 'create_signed_upload_url', 'get_storage_object_size', 'delete_storage_object']:
            stack.enter_context(patch.object(sc, name, getattr(self, name)))


class EnvBase(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {'SUPABASE_JWT_SECRET': SECRET, 'SUPABASE_URL': BASE})
        env.start()
        self.addCleanup(env.stop)
        for key in ['SUPABASE_JWT_JWKS_URL', 'SUPABASE_JWT_USE_JWKS', 'SUPABASE_JWT_ISSUER', 'API_KEY']:
            os.environ.pop(key, None)


class JwtTests(EnvBase):
    def test_valid_token(self):
        user = auth.verify_supabase_jwt(make_token(email='a@b.c'))
        self.assertEqual((user.user_id, user.email), (USER, 'a@b.c'))

    def assertRejected(self, token, status=401):
        with self.assertRaises(auth.AuthError) as ctx:
            auth.verify_supabase_jwt(token)
        self.assertEqual(ctx.exception.status_code, status)
        self.assertTrue(not token or token not in ctx.exception.detail)

    def test_expired(self):
        self.assertRejected(make_token(exp_delta=-3600))

    def test_wrong_secret(self):
        self.assertRejected(make_token(secret='some-other-secret-that-is-long-enough-xx'))

    def test_wrong_audience_and_issuer(self):
        self.assertRejected(make_token(aud='anon'))
        self.assertRejected(make_token(iss='https://evil.invalid/auth/v1'))

    def test_issuer_check_can_be_disabled(self):
        with patch.dict(os.environ, {'SUPABASE_JWT_ISSUER': ''}):
            self.assertEqual(auth.verify_supabase_jwt(make_token(iss='https://x.invalid')).user_id, USER)

    def test_missing_claims_and_bad_sub(self):
        self.assertRejected(make_token(sub='not-a-uuid'))
        self.assertRejected(make_token(sub=None))
        self.assertRejected(make_token(role='service_role'))

    def test_alg_none_and_garbage(self):
        none_token = jwt.encode({'sub': USER, 'aud': 'authenticated', 'exp': int(time.time()) + 60}, None, algorithm='none')
        self.assertRejected(none_token)
        self.assertRejected('garbage')
        self.assertRejected('')

    def test_hs_token_rejected_when_only_jwks_configured(self):
        with patch.dict(os.environ, {'SUPABASE_JWT_SECRET': '', 'SUPABASE_JWT_JWKS_URL': f'{BASE}/jwks'}):
            self.assertRejected(make_token())

    def test_fails_closed_when_unconfigured(self):
        with patch.dict(os.environ, {'SUPABASE_JWT_SECRET': '', 'SUPABASE_AUTH_REMOTE_VERIFY': 'false'}):
            self.assertRejected(make_token(), status=503)

    def test_bearer_header_parsing(self):
        for header in [None, '', 'Basic abc', 'Bearer', 'Bearer  ']:
            with self.assertRaises(auth.AuthError):
                auth.authenticate_header(header)
        self.assertEqual(auth.authenticate_header(f'bearer {make_token()}').user_id, USER)

    def test_es256_via_jwks(self):
        from cryptography.hazmat.primitives.asymmetric import ec
        private = ec.generate_private_key(ec.SECP256R1())
        token = jwt.encode({'sub': USER, 'aud': 'authenticated', 'iss': f'{BASE}/auth/v1', 'exp': int(time.time()) + 60},
                           private, algorithm='ES256', headers={'kid': 'k1'})
        stub = MagicMock()
        stub.get_signing_key_from_jwt.return_value = SimpleNamespace(key=private.public_key())
        with patch.dict(os.environ, {'SUPABASE_JWT_JWKS_URL': f'{BASE}/jwks'}), patch.object(auth, '_jwks_client', return_value=stub):
            self.assertEqual(auth.verify_supabase_jwt(token).user_id, USER)
            # HS256 still works with the secret while both are configured
            self.assertEqual(auth.verify_supabase_jwt(make_token()).user_id, USER)
            # ...and an ES256 token signed by a different key is rejected
            other = ec.generate_private_key(ec.SECP256R1())
            bad = jwt.encode({'sub': USER, 'aud': 'authenticated', 'iss': f'{BASE}/auth/v1', 'exp': int(time.time()) + 60}, other, algorithm='ES256')
            self.assertRejected(bad)


class CreateJobApiTests(EnvBase):
    def setUp(self):
        super().setUp()
        self.db = FakeStorageAndDB()
        stack = ExitStack()
        self.db.patch(stack)
        self.addCleanup(stack.close)
        self.api = TestClient(main.app)
        self.body = {'upload_pending': True, 'meeting_id': MEETING, 'expected_bytes': 150_000_000,
                     'duration': 5400.0, 'language': 'it', 'provider': 'xai', 'file_extension': 'm4a', 'content_type': 'audio/m4a'}

    def post(self, body=None, token=None, headers=None):
        h = {'Authorization': f'Bearer {token or make_token()}', **(headers or {})}
        return self.api.post('/jobs', json=body or self.body, headers=h)

    def test_returns_signed_url_and_creates_awaiting_job(self):
        r = self.post()
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data['status'], 'awaiting_upload')
        self.assertIn('/object/upload/sign/recordings/', data['upload_url'])
        self.assertEqual(data['upload_method'], 'PUT')
        self.assertEqual(data['upload_headers']['Content-Type'], 'audio/m4a')
        self.assertEqual(data['storage_path'], f'{USER}/{MEETING}.m4a')
        self.assertEqual(data['expected_bytes'], 150_000_000)
        self.assertTrue(data['expires_at'] and data['upload_deadline'])
        row = self.db.rows[data['job_id']]
        self.assertEqual((row['user_id'], row['transcription_provider'], row['language'], row['duration']), (USER, 'xai', 'it', 5400.0))
        self.assertEqual(row['status'], 'awaiting_upload')
        self.assertIsNone(row.get('audio_url'))

    def test_user_id_comes_from_token_not_body(self):
        other = str(uuid.uuid4())
        r = self.post({**self.body, 'user_id': other})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.db.rows, {})
        r = self.post({**self.body, 'user_id': USER.upper()})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(list(self.db.rows.values())[0]['user_id'], USER)

    def test_auth_required(self):
        for headers in [{}, {'Authorization': 'Bearer nope'}, {'Authorization': f'Bearer {make_token(exp_delta=-10)}'},
                        {'Authorization': f'Bearer {make_token(secret="x" * 40)}'}]:
            r = self.api.post('/jobs', json=self.body, headers=headers)
            self.assertEqual(r.status_code, 401, headers)
            self.assertEqual(r.headers.get('www-authenticate'), 'Bearer')
        self.assertEqual(self.db.rows, {})

    def test_idempotent_per_meeting_returns_fresh_url(self):
        first = self.post().json()
        second = self.post().json()
        self.assertEqual(first['job_id'], second['job_id'])
        self.assertEqual(len(self.db.rows), 1)
        self.assertNotEqual(first['upload_url'], second['upload_url'])
        self.assertEqual(len(self.db.sign_calls), 2)

    def test_retry_extends_deadline_and_updates_size(self):
        first = self.post().json()
        second = self.post({**self.body, 'expected_bytes': 151_000_000}).json()
        self.assertEqual(second['expected_bytes'], 151_000_000)
        self.assertGreaterEqual(second['upload_deadline'], first['upload_deadline'])

    def test_retry_after_promotion_does_not_reupload(self):
        first = self.post().json()
        self.db.objects[first['storage_path']] = 150_000_000
        bu.promote_uploaded_jobs()
        again = self.post().json()
        self.assertEqual((again['job_id'], again['status']), (first['job_id'], 'pending'))
        self.assertNotIn('upload_url', again)
        self.assertEqual(len(self.db.sign_calls), 1)

    def test_failed_job_does_not_block_new_one(self):
        first = self.post().json()
        self.db.rows[first['job_id']]['status'] = 'failed'
        second = self.post().json()
        self.assertNotEqual(first['job_id'], second['job_id'])

    def test_concurrent_create_converges_on_one_job(self):
        results, errors = [], []
        def call():
            try:
                results.append(bu.create_upload_job(user_id=USER, meeting_id=MEETING, expected_bytes=10))
            except Exception as e:  # pragma: no cover
                errors.append(e)
        threads = [threading.Thread(target=call) for _ in range(8)]
        [t.start() for t in threads]; [t.join() for t in threads]
        self.assertEqual(errors, [])
        self.assertEqual(len(self.db.rows), 1)
        self.assertEqual({r['job_id'] for r in results}, set(self.db.rows))

    def test_validation(self):
        bad = [{'meeting_id': '../../etc/passwd'}, {'expected_bytes': 0}, {'expected_bytes': None},
               {'expected_bytes': 10 ** 12}, {'file_extension': 'exe'}, {'file_extension': '../x'}, {'content_type': 'text/html'},
               {'provider': 'nope'}]
        for patch_ in bad:
            body = {**self.body, **patch_}
            r = self.post(body)
            self.assertIn(r.status_code, (400, 413, 422), patch_)
        self.assertEqual(self.db.rows, {})
        self.assertEqual(self.db.sign_calls, [])

    def test_signing_failure_creates_no_job_and_leaks_nothing(self):
        self.db.fail_sign = True
        r = self.post()
        self.assertEqual(r.status_code, 502)
        self.assertEqual(self.db.rows, {})
        self.assertNotIn('token', r.text.lower())

    def test_upload_url_is_never_printed(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            data = self.post().json()
            self.post()
            bu.promote_uploaded_jobs()
        self.assertNotIn('SECRET-TOKEN', buf.getvalue())
        self.assertNotIn(data['upload_url'], buf.getvalue())

    def test_legacy_mode_unchanged(self):
        inserted = []
        table = MagicMock()
        def insert(data):
            inserted.append(data)
            table.execute.return_value = SimpleNamespace(data=[{**data, 'id': 'job', 'created_at': 'now'}])
            return table
        table.insert.side_effect = insert
        with patch.object(sc.supabase, 'table', return_value=table):
            r = self.api.post('/jobs', json={'user_id': USER, 'meeting_id': MEETING, 'audio_url': f'{BASE}/storage/v1/object/public/recordings/{USER}/{MEETING}.m4a'})  # no JWT needed
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {'job_id': 'job', 'status': 'pending', 'created_at': 'now'})
        self.assertEqual(inserted[0]['status'], 'pending')
        self.assertNotIn('storage_path', inserted[0])
        self.assertEqual(self.api.post('/jobs', json={'meeting_id': MEETING}).status_code, 422)  # legacy still needs user_id


class StorageClientTests(unittest.TestCase):
    def fake_token(self, exp):
        body = base64.urlsafe_b64encode(json.dumps({'exp': exp}).encode()).decode().rstrip('=')
        return f'aaa.{body}.ccc'

    def test_signed_url_request_shape_and_expiry(self):
        exp = int(time.time()) + 7200
        token = self.fake_token(exp)
        response = httpx.Response(200, json={'url': f'/object/upload/sign/recordings/{USER}/{MEETING}.m4a?token={token}'})
        with patch.object(sc.storage_http, 'post', return_value=response) as post:
            out = sc.create_signed_upload_url(f'{USER}/{MEETING}.m4a')
        url = post.call_args.args[0]
        self.assertEqual(url, f'{sc.STORAGE_BASE_URL}/object/upload/sign/recordings/{USER}/{MEETING}.m4a')
        self.assertEqual(post.call_args.kwargs['headers']['x-upsert'], 'true')
        self.assertTrue(out['upload_url'].startswith(f'{sc.STORAGE_BASE_URL}/object/upload/sign/recordings/'))
        self.assertIn(f'token={token}', out['upload_url'])
        self.assertEqual(out['expires_at'], exp)

    def test_signed_url_errors_are_sanitized(self):
        with patch.object(sc.storage_http, 'post', return_value=httpx.Response(403, text='secret-body')):
            with self.assertRaises(sc.StorageError) as ctx:
                sc.create_signed_upload_url('a/b.m4a')
        self.assertNotIn('secret-body', str(ctx.exception))
        with patch.object(sc.storage_http, 'post', return_value=httpx.Response(200, json={'url': '/no-token'})):
            with self.assertRaises(sc.StorageError):
                sc.create_signed_upload_url('a/b.m4a')
        with patch.object(sc.storage_http, 'post', side_effect=httpx.ConnectError('boom https://x?token=abc')):
            with self.assertRaises(sc.StorageError) as ctx:
                sc.create_signed_upload_url('a/b.m4a')
        self.assertNotIn('abc', str(ctx.exception))

    def test_object_size_exact_name_and_incomplete_metadata(self):
        entries = [{'name': 'x.m4a.bak', 'metadata': {'size': 1}}, {'name': 'x.m4a', 'metadata': {'size': 42}}]
        with patch.object(sc.storage_http, 'post', return_value=httpx.Response(200, json=entries)) as post:
            self.assertEqual(sc.get_storage_object_size('u/x.m4a'), 42)
        self.assertEqual(post.call_args.kwargs['json']['prefix'], 'u')
        for payload in ([{'name': 'x.m4a', 'metadata': None}], [{'name': 'x.m4a', 'metadata': {}}], [], [{'name': 'other'}]):
            with patch.object(sc.storage_http, 'post', return_value=httpx.Response(200, json=payload)):
                self.assertIsNone(sc.get_storage_object_size('u/x.m4a'))
        with patch.object(sc.storage_http, 'post', return_value=httpx.Response(500)):
            with self.assertRaises(sc.StorageError):
                sc.get_storage_object_size('u/x.m4a')

    def test_transition_is_compare_and_set_on_status(self):
        table = MagicMock()
        table.update.return_value = table
        table.eq.return_value = table
        table.execute.return_value = SimpleNamespace(data=[])
        with patch.object(sc.supabase, 'table', return_value=table):
            self.assertIsNone(sc.transition_job_status('j1', 'awaiting_upload', 'pending', current_stage='x'))
        table.update.assert_called_once_with({'status': 'pending', 'current_stage': 'x'})
        self.assertEqual([c.args for c in table.eq.call_args_list], [('id', 'j1'), ('status', 'awaiting_upload')])


class PromotionTests(EnvBase):
    def setUp(self):
        super().setUp()
        self.db = FakeStorageAndDB()
        stack = ExitStack()
        self.db.patch(stack)
        self.addCleanup(stack.close)
        self.now = datetime.now(timezone.utc)
        self.job = bu.create_upload_job(user_id=USER, meeting_id=MEETING, expected_bytes=1000, now=self.now)
        self.path = self.job['storage_path']

    def status(self):
        return self.db.rows[self.job['job_id']]['status']

    def test_happy_path_promotes(self):
        self.db.objects[self.path] = 1000
        stats = bu.promote_uploaded_jobs(self.now)
        self.assertEqual((stats['promoted'], self.status()), (1, 'pending'))
        self.assertEqual(self.db.deleted, [])

    def test_absent_object_stays_awaiting(self):
        self.assertEqual(bu.promote_uploaded_jobs(self.now)['waiting'], 1)
        self.assertEqual(self.status(), 'awaiting_upload')

    def test_size_mismatch_or_incomplete_stays_awaiting(self):
        for size in (999, 1001, 0, None):
            self.db.objects[self.path] = size
            bu.promote_uploaded_jobs(self.now)
            self.assertEqual(self.status(), 'awaiting_upload', size)

    def test_tolerance_is_opt_in(self):
        self.db.objects[self.path] = 1004
        with patch.dict(os.environ, {'UPLOAD_SIZE_TOLERANCE_BYTES': '8'}):
            bu.promote_uploaded_jobs(self.now)
        self.assertEqual(self.status(), 'pending')

    def test_ttl_expiry_fails_job_and_deletes_partial_object(self):
        self.db.objects[self.path] = 400  # wrong-size leftover
        later = self.now + timedelta(seconds=bu.ttl_seconds() + 5)
        stats = bu.promote_uploaded_jobs(later)
        row = self.db.rows[self.job['job_id']]
        self.assertEqual((stats['expired'], row['status'], row['error_message']), (1, 'failed', 'upload_expired'))
        self.assertEqual(self.db.deleted, [self.path])

    def test_expiry_of_never_started_upload(self):
        later = self.now + timedelta(hours=7)
        bu.promote_uploaded_jobs(later)
        self.assertEqual((self.status(), self.db.rows[self.job['job_id']]['error_message']), ('failed', 'upload_expired'))
        self.assertEqual(self.db.deleted, [])

    def test_ttl_is_configurable_and_default_six_hours(self):
        self.assertEqual(bu.ttl_seconds(), 6 * 3600)
        # the deadline is stamped on the row when the URL is issued
        with patch.dict(os.environ, {'UPLOAD_PENDING_TTL_SECONDS': '60'}):
            short = bu.create_upload_job(user_id=USER, meeting_id=str(uuid.uuid4()), expected_bytes=5, now=self.now)
            bu.promote_uploaded_jobs(self.now + timedelta(seconds=30))
            self.assertEqual(self.db.rows[short['job_id']]['status'], 'awaiting_upload')
            bu.promote_uploaded_jobs(self.now + timedelta(seconds=61))
            self.assertEqual(self.db.rows[short['job_id']]['status'], 'failed')
            self.assertEqual(self.status(), 'awaiting_upload')  # the 6 h job is untouched

    def test_upload_finishing_at_deadline_is_promoted_not_expired(self):
        self.db.objects[self.path] = 1000
        bu.promote_uploaded_jobs(self.now + timedelta(days=1))
        self.assertEqual(self.status(), 'pending')
        self.assertEqual(self.db.deleted, [])

    def test_row_without_path_still_expires(self):
        self.db.rows[self.job['job_id']]['storage_path'] = None
        bu.promote_uploaded_jobs(self.now + timedelta(days=1))
        self.assertEqual(self.status(), 'failed')

    def test_double_promotion_race_promotes_once(self):
        self.db.objects[self.path] = 1000
        real_size = self.db.get_storage_object_size
        barrier = threading.Barrier(6)
        def slow_size(path):  # make every thread observe 'awaiting' before any transitions
            value = real_size(path)
            try:
                barrier.wait(timeout=2)
            except threading.BrokenBarrierError:
                pass
            return value
        results = []
        with patch.object(sc, 'get_storage_object_size', slow_size):
            threads = [threading.Thread(target=lambda: results.append(bu.promote_uploaded_jobs(self.now))) for _ in range(6)]
            [t.start() for t in threads]; [t.join() for t in threads]
        self.assertEqual(sum(r['promoted'] for r in results), 1)
        self.assertEqual(self.status(), 'pending')

    def test_promote_vs_expire_race_never_deletes_promoted_file(self):
        self.db.objects[self.path] = 1000
        later = self.now + timedelta(days=1)
        # Expirer saw a stale "size mismatch" snapshot, but the upload completed and was promoted meanwhile.
        stale = dict(self.db.rows[self.job['job_id']])
        with patch.object(sc, 'list_awaiting_upload_jobs', return_value=[stale]), \
                patch.object(sc, 'get_storage_object_size', return_value=1):
            self.db.transition_job_status(stale['id'], 'awaiting_upload', 'pending')
            stats = bu.promote_uploaded_jobs(later)
        self.assertEqual((stats['expired'], self.status(), self.db.deleted), (0, 'pending', []))

    def test_storage_outage_does_not_expire_and_aborts_pass(self):
        for i in range(5):
            bu.create_upload_job(user_id=USER, meeting_id=str(uuid.uuid4()), expected_bytes=5, now=self.now)
        self.db.fail_storage = True
        calls = []
        real = self.db.get_storage_object_size
        with patch.object(sc, 'get_storage_object_size', lambda p: (calls.append(p), real(p))[1]):
            stats = bu.promote_uploaded_jobs(self.now + timedelta(days=1))
        self.assertEqual(len(calls), bu.MAX_CONSECUTIVE_STORAGE_FAILURES)
        self.assertEqual(stats['expired'], 0)
        self.assertTrue(all(r['status'] == 'awaiting_upload' for r in self.db.rows.values()))

    def test_never_raises_when_listing_fails(self):
        with patch.object(sc, 'list_awaiting_upload_jobs', side_effect=Exception('db down')):
            self.assertEqual(bu.promote_uploaded_jobs(self.now)['errors'], 1)


class WorkerLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_promotion_runs_before_pending_query(self):
        order = []
        with patch.object(jobs.background_upload, 'promote_uploaded_jobs', side_effect=lambda: order.append('promote')), \
                patch.object(jobs, 'get_pending_jobs', side_effect=lambda: (order.append('pending'), [])[1]):
            await jobs.process_pending_jobs(1)
        self.assertEqual(order, ['promote', 'pending'])

    async def test_promotion_failure_does_not_block_jobs(self):
        with patch.object(jobs.background_upload, 'promote_uploaded_jobs', side_effect=RuntimeError('x')), \
                patch.object(jobs, 'get_pending_jobs', return_value=[{'id': 'job-1234567'}]) as pending, \
                patch.object(jobs, 'process_job') as process:
            await jobs.process_pending_jobs(1)
        pending.assert_called_once()
        process.assert_called_once()


def fake_segments(names_to_text):
    """segment_audio stand-in that writes one small file per segment."""
    def segment(src, out_dir):
        paths = []
        for i in range(len(names_to_text)):
            p = os.path.join(out_dir, f'seg_{i:04d}.mp3')
            with open(p, 'wb') as f:
                f.write(f'seg{i}'.encode())
            paths.append(p)
        return paths
    return segment


class LargeFileWorkerTests(unittest.TestCase):
    """Upload-mode job through process_job: OpenAI large-file segmenting vs the unchanged small path."""
    def setUp(self):
        self.workroot = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.workroot, True)
        env = patch.dict(os.environ, {'XAI_WORK_DIR': self.workroot, 'XAI_SINGLE_REQUEST_ENABLED': 'true'})
        env.start()
        self.addCleanup(env.stop)

    def run_job(self, size, provider='openai', duration=None, segment_texts=('alpha beta', 'beta gamma'), xai_single=None, call_fetchers=False, probe=None, **env):
        job = {'id': 'job', 'meeting_id': 'm', 'audio_url': None, 'storage_path': f'{USER}/{MEETING}.m4a', 'expected_bytes': size,
               'is_chunked': False, 'language': 'it', 'transcription_provider': provider, 'duration': duration}

        def download(path, dest):
            with open(dest, 'wb') as f:
                f.write(b'x' * size)

        names = ['update_job_status', 'update_job_progress', 'increment_retry_count', 'update_job_with_results']
        m = {n: MagicMock() for n in names}
        texts = list(segment_texts)  # looked up by segment name: worker threads run in any order
        fetched = []

        def single_request(fetchers, *a, **k):
            if call_fetchers:
                with patch.object(jobs, 'download_storage_object_to_file', side_effect=lambda p, d: fetched.append((p, d))):
                    fetchers[0]('/tmp/dest')
            return xai_single

        with patch.dict(os.environ, env), patch.multiple(jobs, **m), \
                patch.object(jobs, 'download_storage_object_to_file', side_effect=download) as dl, \
                patch.object(jobs, 'download_audio') as legacy_dl, \
                patch.object(jobs, 'transcribe_audio', return_value={'transcript': 'small', 'duration': 12}) as ta, \
                patch.object(large_audio, 'segment_audio', side_effect=fake_segments(segment_texts)) as seg, \
                patch.object(large_audio.transcribe, 'transcribe_chunk_with_retry', side_effect=lambda data, name, *a, **k: texts[int(name.split('_')[1].split('.')[0]) - 1]) as chunk, \
                patch.object(large_audio.xai_single, 'probe_duration', return_value=probe), \
                patch.object(jobs.xai_single, 'run_single_request', side_effect=single_request) as rs, \
                patch.object(jobs, 'generate_summary', return_value='s'), patch.object(jobs, 'generate_overview', return_value='o'), \
                patch.object(jobs, 'extract_actions', return_value=[]):
            jobs.process_job(job)
        return SimpleNamespace(fetched=fetched, m=m, dl=dl, legacy_dl=legacy_dl, ta=ta, seg=seg, chunk=chunk, rs=rs)

    def test_openai_large_file_is_segmented_not_decoded(self):
        r = self.run_job(20 * 1024 * 1024, probe=5400.0, segment_texts=['one two three four five six seven', 'six seven one more tail words here'])
        r.seg.assert_called_once()
        r.ta.assert_not_called()          # no whole-file pydub path
        r.legacy_dl.assert_not_called()   # no in-memory download
        r.rs.assert_not_called()          # OpenAI never uses the xAI single request
        self.assertEqual(r.chunk.call_count, 2)
        self.assertEqual([c.args[3] for c in r.chunk.call_args_list], ['openai', 'openai'])
        self.assertEqual(r.chunk.call_args_list[0].args[2], 'it')
        kw = r.m['update_job_with_results'].call_args.kwargs
        self.assertIn('seven', kw['transcript'])
        self.assertEqual(kw['duration'], 5400.0)
        r.m['increment_retry_count'].assert_not_called()
        self.assertEqual(os.listdir(self.workroot), [])  # scratch dir removed

    def test_segments_are_merged_in_audio_order_with_overlap_removed(self):
        a = 'the quick brown fox jumps over the lazy dog and keeps running far away'
        b = 'keeps running far away into the forest where nobody'
        r = self.run_job(20 * 1024 * 1024, segment_texts=[a, b])
        text = r.m['update_job_with_results'].call_args.kwargs['transcript']
        self.assertEqual(text.count('keeps running far away'), 1)
        self.assertTrue(text.startswith('the quick brown fox') and text.endswith('nobody'))

    def test_small_upload_keeps_existing_transcribe_audio_path(self):
        r = self.run_job(1024 * 1024)
        r.ta.assert_called_once()
        self.assertEqual(r.ta.call_args.kwargs['provider'], 'openai')
        self.assertEqual(r.ta.call_args.args[1], 'audio.m4a')
        r.seg.assert_not_called(); r.chunk.assert_not_called()

    def test_ffprobe_duration_triggers_segmenting_for_small_but_long_file(self):
        r = self.run_job(2 * 1024 * 1024, probe=10800.0)
        r.seg.assert_called_once(); r.ta.assert_not_called()

    def test_reported_long_duration_triggers_segmenting_even_if_small(self):
        r = self.run_job(2 * 1024 * 1024, duration=7200)
        r.seg.assert_called_once(); r.ta.assert_not_called()

    def test_threshold_and_kill_switch_are_configurable(self):
        r = self.run_job(2048, LARGE_FILE_THRESHOLD_BYTES='1024')
        r.seg.assert_called_once()
        r = self.run_job(20 * 1024 * 1024, LARGE_FILE_SEGMENTING_ENABLED='false')
        r.seg.assert_not_called(); r.ta.assert_called_once()

    def test_downloaded_size_mismatch_is_retried_not_transcribed(self):
        job_size = 4096
        with patch.object(jobs, 'download_storage_object_to_file', side_effect=lambda p, d: open(d, 'wb').write(b'x' * 10)), \
                patch.object(jobs, 'update_job_progress'), patch.object(jobs, 'update_job_status'), \
                patch.object(jobs, 'increment_retry_count') as retry, patch.object(jobs, 'transcribe_audio') as ta:
            jobs.process_job({'id': 'j', 'meeting_id': 'm', 'storage_path': 'u/m.m4a', 'expected_bytes': job_size,
                              'transcription_provider': 'openai'})
        ta.assert_not_called(); retry.assert_called_once()

    def test_xai_upload_job_uses_storage_fetcher_in_single_request(self):
        import transcription_provider as tp
        result = tp.TranscriptionResult(text='whole', duration=9.0, provider='xai')
        r = self.run_job(20 * 1024 * 1024, provider='xai', xai_single=result, call_fetchers=True)
        r.seg.assert_not_called()
        self.assertEqual(r.fetched, [(f'{USER}/{MEETING}.m4a', '/tmp/dest')])

    def test_xai_fallback_for_big_upload_segments_with_xai_provider(self):
        r = self.run_job(20 * 1024 * 1024, provider='xai', xai_single=None)
        r.seg.assert_called_once()
        self.assertEqual([c.args[3] for c in r.chunk.call_args_list], ['xai', 'xai'])


class SegmentAudioCommandTests(unittest.TestCase):
    def test_ffmpeg_segment_muxer_and_overlap_commands(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if '-f' in cmd and 'segment' in cmd:
                for i, size in enumerate([20000, 20000, 3000]):  # last one shorter than the 2 s overlap
                    with open(os.path.join(d, f'seg_{i:04d}.mp3'), 'wb') as f:
                        f.write(b'0' * size)
            else:
                open(cmd[-1], 'wb').write(b'0' * 100)
            return SimpleNamespace(returncode=0, stderr='')

        with patch.object(large_audio.subprocess, 'run', side_effect=fake_run):
            paths = large_audio.segment_audio('/in/source.m4a', d)
        first = calls[0]
        self.assertEqual(first[:1], ['ffmpeg'])
        self.assertIn('segment', first); self.assertEqual(first[first.index('-segment_time') + 1], '300')
        self.assertEqual(first[first.index('-ac') + 1], '1'); self.assertEqual(first[first.index('-ar') + 1], '16000')
        self.assertEqual(len(calls), 1 + 2)  # 1 segment pass + 2 overlap passes (seg0<-seg1, seg1<-seg2)
        self.assertIn('-t', calls[1])
        self.assertEqual(len(paths), 2)  # tiny trailing piece lives only inside the previous overlap
        self.assertTrue(all(p.endswith('_ov.mp3') for p in paths[:1]))
        self.assertFalse(os.path.exists(os.path.join(d, 'seg_0002.mp3')))

    def test_missing_ffmpeg_and_failure_raise(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        with patch.object(large_audio.subprocess, 'run', side_effect=FileNotFoundError):
            with self.assertRaises(large_audio.SegmentationError):
                large_audio.segment_audio('/in/x.m4a', d)
        with patch.object(large_audio.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stderr='bad')):
            with self.assertRaises(large_audio.SegmentationError):
                large_audio.segment_audio('/in/x.m4a', d)


@unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg not installed')
class RealFfmpegTests(unittest.TestCase):
    def test_real_split_with_overlap(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        src = os.path.join(d, 'tone.wav')
        subprocess.run(['ffmpeg', '-nostdin', '-loglevel', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=13',
                        '-ac', '2', '-ar', '44100', src], check=True)
        out = os.path.join(d, 'segs')
        os.makedirs(out)
        with patch.dict(os.environ, {'LARGE_FILE_SEGMENT_SECONDS': '5', 'LARGE_FILE_OVERLAP_SECONDS': '1'}):
            paths = large_audio.segment_audio(src, out)
        self.assertGreaterEqual(len(paths), 2)
        self.assertTrue(all(os.path.getsize(p) > 0 for p in paths))


if __name__ == '__main__':
    unittest.main()
