"""auth.py: remote verification via GET /auth/v1/user (60 s cache) and AUTH_MODE helpers. No network."""
import io
import os
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import httpx
import jwt

import support
from support import TEST_USER_ID
import auth

BASE = 'https://offline.invalid'


def token_for(sub=TEST_USER_ID, exp_delta=3600, **extra):
    return jwt.encode({'sub': sub, 'aud': 'authenticated', 'exp': int(time.time()) + exp_delta, **extra}, 'unrelated-secret-0123456789abcdef', algorithm='HS256')


class RemoteBase(unittest.TestCase):
    def setUp(self):
        auth.clear_remote_cache()
        env = patch.dict(os.environ, {'SUPABASE_URL': BASE, 'SUPABASE_SERVICE_KEY': 'service-key-xyz', 'SUPABASE_JWT_SECRET': ''})
        env.start()
        self.addCleanup(env.stop)
        for key in ('SUPABASE_JWT_JWKS_URL', 'SUPABASE_JWT_USE_JWKS', 'SUPABASE_ANON_KEY', 'SUPABASE_AUTH_REMOTE_VERIFY', 'AUTH_MODE'):
            os.environ.pop(key, None)
        self.requests = []
        self.respond = lambda request: httpx.Response(200, json={'id': TEST_USER_ID.upper(), 'role': 'authenticated', 'email': 'a@b.example'})

        def handler(request):
            self.requests.append(request)
            return self.respond(request)
        client = httpx.Client(transport=httpx.MockTransport(handler))
        patcher = patch.object(auth, '_remote_http', client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def verify(self, token=None):
        return auth.verify_supabase_jwt(token or token_for())


class RemoteVerifyTests(RemoteBase):
    def test_valid_token_is_verified_against_supabase_auth(self):
        token = token_for()
        user = self.verify(token)
        self.assertEqual((user.user_id, user.email), (TEST_USER_ID, 'a@b.example'))   # normalised from Supabase's answer
        req = self.requests[0]
        self.assertEqual(str(req.url), f'{BASE}/auth/v1/user')
        self.assertEqual(req.headers['authorization'], f'Bearer {token}')
        self.assertEqual(req.headers['apikey'], 'service-key-xyz')

    def test_anon_key_is_preferred_for_the_apikey_header(self):
        with patch.dict(os.environ, {'SUPABASE_ANON_KEY': 'anon-key-abc'}):
            self.verify()
        self.assertEqual(self.requests[0].headers['apikey'], 'anon-key-abc')

    def test_identity_comes_from_supabase_not_from_the_unverified_sub(self):
        self.respond = lambda r: httpx.Response(200, json={'id': '11111111-2222-4333-8444-555555555555', 'role': 'authenticated'})
        self.assertEqual(self.verify(token_for(sub=TEST_USER_ID)).user_id, '11111111-2222-4333-8444-555555555555')

    def test_result_is_cached_for_the_same_token_only(self):
        token = token_for()
        self.verify(token); self.verify(token); self.verify(token)
        self.assertEqual(len(self.requests), 1)
        self.verify(token_for(iat=1))   # different token
        self.assertEqual(len(self.requests), 2)

    def test_cache_expires_after_60_seconds(self):
        token = token_for()
        base = time.monotonic()
        with patch.object(auth.time, 'monotonic', return_value=base):
            self.verify(token)
        with patch.object(auth.time, 'monotonic', return_value=base + 59):
            self.verify(token)
        self.assertEqual(len(self.requests), 1)
        with patch.object(auth.time, 'monotonic', return_value=base + 61):
            self.verify(token)
        self.assertEqual(len(self.requests), 2)

    def test_cache_never_outlives_the_token(self):
        token = token_for(exp_delta=5)
        base = time.monotonic()
        with patch.object(auth.time, 'monotonic', return_value=base):
            self.verify(token)
        with patch.object(auth.time, 'monotonic', return_value=base + 10):
            self.verify(token)   # re-asked upstream (which would now say 401)
        self.assertEqual(len(self.requests), 2)

    def test_garbage_and_expired_tokens_cost_no_upstream_call(self):
        for bad in ('garbage', 'a.b.c', token_for(exp_delta=-5)):
            with self.assertRaises(auth.AuthError) as ctx:
                self.verify(bad)
            self.assertEqual(ctx.exception.status_code, 401)
        no_exp = jwt.encode({'sub': TEST_USER_ID}, 'k' * 32, algorithm='HS256')
        with self.assertRaises(auth.AuthError):
            self.verify(no_exp)
        self.assertEqual(self.requests, [])

    def test_rejected_by_supabase_is_401_and_not_cached(self):
        self.respond = lambda r: httpx.Response(401, json={'msg': 'invalid JWT'})
        token = token_for()
        for _ in range(2):
            with self.assertRaises(auth.AuthError) as ctx:
                self.verify(token)
            self.assertEqual((ctx.exception.status_code, ctx.exception.detail), (401, 'Invalid token'))
        self.assertEqual(len(self.requests), 2)

    def test_upstream_trouble_is_503_fail_closed_and_not_cached(self):
        token = token_for()
        for behaviour in (lambda r: httpx.Response(500), lambda r: httpx.Response(200, json={'nope': 1}),
                          lambda r: httpx.Response(200, text='not json'), lambda r: (_ for _ in ()).throw(httpx.ConnectError('boom'))):
            self.respond = behaviour
            with self.assertRaises(auth.AuthError) as ctx:
                self.verify(token)
            self.assertEqual(ctx.exception.status_code, 503, behaviour)
        self.assertEqual(len(self.requests), 4)

    def test_non_user_roles_are_rejected(self):
        self.respond = lambda r: httpx.Response(200, json={'id': TEST_USER_ID, 'role': 'service_role'})
        with self.assertRaises(auth.AuthError) as ctx:
            self.verify()
        self.assertEqual(ctx.exception.status_code, 401)

    def test_can_be_disabled_and_then_fails_closed(self):
        with patch.dict(os.environ, {'SUPABASE_AUTH_REMOTE_VERIFY': 'false'}):
            with self.assertRaises(auth.AuthError) as ctx:
                self.verify()
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(self.requests, [])

    def test_local_key_configuration_wins_and_never_calls_upstream(self):
        secret = 'a-local-secret-that-is-long-enough-123456'
        token = jwt.encode({'sub': TEST_USER_ID, 'aud': 'authenticated', 'iss': f'{BASE}/auth/v1', 'exp': int(time.time()) + 60}, secret, algorithm='HS256')
        with patch.dict(os.environ, {'SUPABASE_JWT_SECRET': secret}):
            self.assertEqual(auth.verify_supabase_jwt(token).user_id, TEST_USER_ID)
            with self.assertRaises(auth.AuthError):
                self.verify(token_for())   # signed with another secret: rejected locally, no fallback to remote
        self.assertEqual(self.requests, [])

    def test_cache_is_bounded(self):
        with patch.object(auth, 'REMOTE_CACHE_MAX_ENTRIES', 3):
            for i in range(6):
                self.verify(token_for(iat=i))
        self.assertLessEqual(len(auth._remote_cache), 3)

    def test_token_is_never_stored_in_the_cache_keys(self):
        token = token_for()
        self.verify(token)
        self.assertTrue(all(token not in key for key in auth._remote_cache))


class ModeTests(RemoteBase):
    def test_mode_default_and_parsing(self):
        self.assertEqual(auth.auth_mode(), 'log')
        for raw, expected in (('off', 'off'), (' ENFORCE ', 'enforce'), ('Log', 'log'), ('', 'log'), ('enforced', 'log')):
            with patch.dict(os.environ, {'AUTH_MODE': raw}):
                self.assertEqual(auth.auth_mode(), expected, raw)

    def test_resolve_user_never_raises(self):
        self.assertEqual(auth.resolve_user(None).problem, 'missing')
        self.assertEqual(auth.resolve_user('').problem, 'missing')
        self.assertEqual(auth.resolve_user('Basic abc').problem, 'missing')
        self.assertEqual(auth.resolve_user('Bearer garbage').problem, 'invalid')
        self.assertEqual(auth.resolve_user(f'Bearer {token_for(exp_delta=-9)}').problem, 'invalid')
        self.assertEqual(auth.resolve_user(f'Bearer {token_for()}').user.user_id, TEST_USER_ID)
        self.respond = lambda r: httpx.Response(502)
        self.assertEqual(auth.resolve_user(f'Bearer {token_for(iat=5)}').problem, 'unavailable')

    def test_counters_and_log_lines_have_no_pii(self):
        auth.auth_counters.clear()
        buf = io.StringIO()
        with redirect_stdout(buf):
            for _ in range(7):
                auth.note_request('GET /jobs', 'missing')
            auth.note_request('GET /jobs', 'authenticated')
        self.assertEqual(auth.auth_counters['GET /jobs:missing'], 7)
        self.assertEqual(auth.auth_counters['GET /jobs:authenticated'], 1)
        lines = buf.getvalue().strip().splitlines()
        self.assertEqual(len(lines), auth.LOG_FIRST)       # first 5 only, not every request
        self.assertIn('mode=log', lines[0])
        self.assertNotIn(TEST_USER_ID, buf.getvalue())


if __name__ == '__main__':
    unittest.main()
