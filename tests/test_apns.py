"""Live Activity / APNs: offline tests (httpx.MockTransport, fake store, no network)."""
import asyncio
import json
import os
import threading
import unittest
from unittest.mock import patch

import httpx
from contextlib import ExitStack
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
import jwt

import support  # noqa: F401  (offline env before any project import)
import apns
import jobs

TOKEN = 'ab' * 32
PEM = ec.generate_private_key(ec.SECP256R1()).private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
CONFIG = apns.ApnsConfig(PEM, 'KEYID12345', 'TEAMID1234', 'com.example.snipnote')
ENV = {'APNS_KEY_P8': PEM, 'APNS_KEY_ID': 'KEYID12345', 'APNS_TEAM_ID': 'TEAMID1234', 'APNS_BUNDLE_ID': 'com.example.snipnote'}


class FakeStore:
    def __init__(self, rows=None, fail=False):
        self.rows = rows if rows is not None else [{'token': TOKEN, 'environment': 'sandbox', 'bundle_id': None}]
        self.fail = fail
        self.deleted = []
        self.deleted_all = []
        self.stages = []

    def get_tokens(self, job_id):
        if self.fail:
            raise RuntimeError('supabase down')
        return list(self.rows)

    def delete_token(self, job_id, token):
        self.deleted.append((job_id, token))

    def delete_tokens(self, job_id):
        self.deleted_all.append(job_id)

    def persist_stage(self, job_id, stage):
        if self.fail:
            raise RuntimeError('column "stage" does not exist')
        self.stages.append((job_id, stage))


def make_client(handler):
    return apns.ApnsClient(CONFIG, transport=httpx.MockTransport(handler))


def run(coro):
    return asyncio.run(coro)


class PayloadTests(unittest.TestCase):
    def test_update_payload_shape(self):
        p = apns.build_payload('transcribing', 0.4234, chunk=1, total_chunks=3, message='Chunk 1 of 3', now=1000,
                               stale_seconds=600)
        self.assertEqual(p, {'aps': {
            'timestamp': 1000, 'event': 'update', 'stale-date': 1600,
            'content-state': {'stage': 'transcribing', 'progress': 0.423, 'chunk': 1, 'totalChunks': 3, 'message': 'Chunk 1 of 3'},
        }})

    def test_minimal_update_has_null_progress(self):
        p = apns.build_payload('preparing', None, now=5, stale_seconds=10)
        self.assertEqual(p['aps']['content-state'], {'stage': 'preparing', 'progress': None})

    def test_end_payload_has_alert_and_dismissal(self):
        done = apns.build_payload('done', 0.5, now=1000, dismissal_seconds=7200)['aps']
        self.assertEqual((done['event'], done['dismissal-date']), ('end', 8200))
        self.assertEqual(done['content-state'], {'stage': 'done', 'progress': 1.0})
        self.assertEqual(done['alert']['title'], 'Meeting ready')
        self.assertNotIn('stale-date', done)
        failed = apns.build_payload('failed', now=1000, dismissal_seconds=99999999)['aps']
        self.assertEqual(failed['dismissal-date'], 1000 + 4 * 3600)  # clamped to APNs' 4h max
        self.assertEqual(failed['alert']['title'], 'Transcription failed')

    def test_payload_far_below_4kb_and_message_capped(self):
        p = apns.build_payload('transcribing', 0.1, message='x' * 5000, now=1)
        self.assertLess(len(json.dumps(p).encode()), 4096)
        self.assertEqual(len(p['aps']['content-state']['message']), apns.MAX_MESSAGE_CHARS)


class JwtTests(unittest.TestCase):
    def test_es256_claims_and_cache_and_refresh(self):
        now = [1000.0]
        provider = apns.JwtProvider(PEM, 'KEYID12345', 'TEAMID1234', clock=lambda: now[0])
        first = provider.get()
        header = jwt.get_unverified_header(first)
        self.assertEqual((header['alg'], header['kid']), ('ES256', 'KEYID12345'))
        self.assertEqual(jwt.decode(first, options={'verify_signature': False})['iss'], 'TEAMID1234')
        now[0] += 49 * 60
        self.assertEqual(provider.get(), first)  # cached
        now[0] += 2 * 60
        self.assertNotEqual(provider.get(), first)  # >50 min: refreshed

    def test_forced_refresh_only_when_rejected_token_is_current_and_old(self):
        now = [1000.0]
        provider = apns.JwtProvider(PEM, 'K', 'T', clock=lambda: now[0])
        first = provider.get()
        self.assertEqual(provider.refresh_after_rejection(first), first)  # too young: no hammering
        now[0] += 400
        second = provider.refresh_after_rejection(first)
        self.assertNotEqual(second, first)
        self.assertEqual(provider.refresh_after_rejection(first), second)  # stale rejection: no second mint

    def test_concurrent_get_mints_once(self):
        provider = apns.JwtProvider(PEM, 'K', 'T')
        results = []
        threads = [threading.Thread(target=lambda: results.append(provider.get())) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(set(results)), 1)


class ClientTests(unittest.TestCase):
    def test_headers_host_and_body(self):
        seen = {}

        def handler(request):
            seen['url'] = str(request.url)
            seen['headers'] = dict(request.headers)
            seen['body'] = json.loads(request.content)
            return httpx.Response(200)

        payload = apns.build_payload('summarizing', 0.7, now=1000)
        result = run(make_client(handler).send(TOKEN, 'sandbox', payload, now=1000))
        self.assertTrue(result.ok)
        self.assertEqual(seen['url'], f'https://api.sandbox.push.apple.com/3/device/{TOKEN}')
        h = seen['headers']
        self.assertEqual(h['apns-push-type'], 'liveactivity')
        self.assertEqual(h['apns-topic'], 'com.example.snipnote.push-type.liveactivity')
        self.assertEqual(h['apns-priority'], '5')
        self.assertEqual(h['apns-expiration'], '1120')
        self.assertTrue(h['authorization'].startswith('bearer '))
        self.assertEqual(seen['body'], payload)

    def test_production_host_end_event_priority_and_token_bundle(self):
        seen = {}

        def handler(request):
            seen['url'] = str(request.url)
            seen['headers'] = dict(request.headers)
            return httpx.Response(200)

        run(make_client(handler).send(TOKEN, 'production', apns.build_payload('done', now=1000), bundle_id='com.other', now=1000))
        self.assertTrue(seen['url'].startswith('https://api.push.apple.com/3/device/'))
        self.assertEqual(seen['headers']['apns-priority'], '10')
        self.assertEqual(seen['headers']['apns-topic'], 'com.other.push-type.liveactivity')
        self.assertEqual(seen['headers']['apns-expiration'], str(1000 + 3600))

    def test_dead_token_signals(self):
        for status, reason in ((410, 'Unregistered'), (400, 'BadDeviceToken'), (410, None)):
            body = {'reason': reason} if reason else {}
            client = make_client(lambda request, s=status, b=body: httpx.Response(s, json=b))
            result = run(client.send(TOKEN, 'production', apns.build_payload('done')))
            self.assertTrue(result.dead_token, (status, reason))

    def test_topic_mismatch_is_not_a_dead_token(self):
        client = make_client(lambda request: httpx.Response(400, json={'reason': 'DeviceTokenNotForTopic'}))
        self.assertFalse(run(client.send(TOKEN, 'production', apns.build_payload('done'))).dead_token)

    def test_malformed_token_never_hits_network(self):
        def handler(request):
            raise AssertionError('must not send')
        result = run(make_client(handler).send('../../evil?x=1', 'production', apns.build_payload('done')))
        self.assertFalse(result.ok)
        self.assertTrue(result.dead_token)

    def test_expired_provider_token_refreshes_once(self):
        now = [1000.0]
        provider = apns.JwtProvider(PEM, 'K', 'T', clock=lambda: now[0])
        auths = []

        def handler(request):
            auths.append(request.headers['authorization'])
            if len(auths) == 1:
                now[0] += 400
                return httpx.Response(403, json={'reason': 'ExpiredProviderToken'})
            return httpx.Response(200)

        client = apns.ApnsClient(CONFIG, jwt_provider=provider, transport=httpx.MockTransport(handler))
        self.assertTrue(run(client.send(TOKEN, 'production', apns.build_payload('done'))).ok)
        self.assertEqual(len(auths), 2)
        self.assertNotEqual(auths[0], auths[1])

    def test_network_error_is_swallowed(self):
        def handler(request):
            raise httpx.ConnectError(f'boom {request.url}')
        result = run(make_client(handler).send(TOKEN, 'production', apns.build_payload('done')))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, 'ConnectError')  # class name only: message could embed the token URL


class NotifierTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.status = 200
        self.reason = None

        def handler(request):
            self.requests.append(json.loads(request.content))
            return httpx.Response(self.status, json={'reason': self.reason} if self.reason else {})

        self.client = make_client(handler)
        self.store = FakeStore()
        self.now = [100.0]
        self.notifier = apns.LiveActivityNotifier(self.client, self.store, progress_interval=20, clock=lambda: self.now[0])

    def states(self):
        return [r['aps']['content-state']['stage'] for r in self.requests]

    def test_stage_transitions_always_push_and_progress_is_throttled(self):
        n = self.notifier
        n.notify('job1', 'preparing', 0)
        n.notify('job1', 'transcribing', 10)       # transition: push
        self.now[0] += 5
        n.notify('job1', 'transcribing', 20)       # 5 s: throttled
        self.now[0] += 20
        n.notify('job1', 'transcribing', 30)       # >= 20 s: push
        n.notify('job1', 'transcribing', 30)       # same progress: skipped
        n.notify('job1', 'summarizing', 60)        # transition again immediately: push
        n.flush()
        self.assertEqual(self.states(), ['preparing', 'transcribing', 'transcribing', 'summarizing'])
        self.assertEqual([r['aps']['content-state']['progress'] for r in self.requests], [0.0, 0.1, 0.3, 0.6])

    def test_done_ends_activity_forgets_tokens_and_persists_stage(self):
        n = self.notifier
        n.notify('job1', 'summarizing', 60)
        n.notify('job1', 'done', 100)
        n.flush()
        last = self.requests[-1]['aps']
        self.assertEqual((last['event'], last['content-state']['stage']), ('end', 'done'))
        self.assertIn('alert', last)
        self.assertEqual(self.store.deleted_all, ['job1'])
        self.assertEqual(self.store.stages, [('job1', 'summarizing'), ('job1', 'done')])

    def test_failed_ends_with_alert(self):
        self.notifier.notify('job1', 'failed')
        self.notifier.flush()
        aps = self.requests[-1]['aps']
        self.assertEqual((aps['event'], aps['alert']['title']), ('end', 'Transcription failed'))

    def test_410_deletes_only_that_token(self):
        self.status, self.reason = 410, 'Unregistered'
        self.notifier.notify('job1', 'preparing', 0)
        self.notifier.flush()
        self.assertEqual(self.store.deleted, [('job1', TOKEN)])

    def test_no_tokens_means_no_requests(self):
        self.store.rows = []
        self.notifier.notify('job1', 'preparing', 0)
        self.notifier.flush()
        self.assertEqual(self.requests, [])
        self.assertEqual(self.store.stages, [('job1', 'preparing')])  # stage still persisted

    def test_unknown_stage_ignored(self):
        self.notifier.notify('job1', 'bogus')
        self.notifier.flush()
        self.assertEqual((self.requests, self.store.stages), ([], []))

    def test_store_and_apns_failures_never_propagate(self):
        self.store.fail = True
        self.notifier.notify('job1', 'preparing', 0)      # token lookup + persist both raise in the background
        self.notifier.notify('job1', 'done')
        self.notifier.flush()                              # returns; nothing raised
        self.client._transport = httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ReadTimeout('slow')))
        self.client._http = None
        self.store.fail = False
        self.notifier.notify('job2', 'preparing', 0)
        self.notifier.flush()

    def test_notify_returns_immediately_while_apns_is_slow(self):
        gate = threading.Event()

        class SlowClient:
            async def send(self, *a, **k):
                while not gate.is_set():
                    await asyncio.sleep(0.01)
                return apns.ApnsResult(ok=True)

        n = apns.LiveActivityNotifier(SlowClient(), self.store, progress_interval=20)
        import time
        started = time.monotonic()
        n.notify('job1', 'preparing', 0)
        n.notify('job1', 'transcribing', 10)
        self.assertLess(time.monotonic() - started, 1.0)
        gate.set()
        n.flush()

    def test_missing_stage_column_turns_persistence_off(self):
        self.store.fail = True
        self.notifier.notify('job1', 'preparing', 0)
        self.notifier.flush()
        self.assertTrue(self.notifier._stage_column_missing)


class DisabledTests(unittest.TestCase):
    def tearDown(self):
        apns.set_notifier(None)
        apns._notifier = None
        apns._disabled = False

    def test_config_absent_when_any_var_missing(self):
        for missing in ENV:
            env = {k: v for k, v in ENV.items() if k != missing}
            with patch.dict(os.environ, env, clear=True):
                self.assertIsNone(apns.ApnsConfig.from_env(), missing)

    def test_config_from_inline_pem_and_file(self):
        with patch.dict(os.environ, ENV, clear=True):
            self.assertEqual(apns.ApnsConfig.from_env().bundle_id, 'com.example.snipnote')
        one_line = PEM.replace('\n', '\\n')
        with patch.dict(os.environ, {**ENV, 'APNS_KEY_P8': one_line}, clear=True):
            self.assertIn('BEGIN PRIVATE KEY', apns.ApnsConfig.from_env().key_pem)
        import tempfile
        with tempfile.NamedTemporaryFile('w', suffix='.p8') as f:
            f.write(PEM)
            f.flush()
            with patch.dict(os.environ, {**ENV, 'APNS_KEY_P8': f.name}, clear=True):
                self.assertIsNotNone(apns.ApnsConfig.from_env())
        with patch.dict(os.environ, {**ENV, 'APNS_KEY_P8': '/nonexistent/key.p8'}, clear=True):
            self.assertIsNone(apns.ApnsConfig.from_env())

    def test_unconfigured_notifier_does_no_push_and_no_thread(self):
        store = FakeStore()
        with patch.dict(os.environ, {}, clear=True):
            apns.set_notifier(None)
            apns._notifier = None
            apns._disabled = False
            n = apns.get_notifier()
            self.assertFalse(n.push_enabled)
            n.persist_stage = False  # only push behaviour under test here
            n.store = store
            apns.notify_stage('job1', 'preparing', 0)
            apns.notify_stage('job1', 'done', 100)
            self.assertIsNone(n._thread)
            self.assertEqual(store.stages, [])

    def test_kill_switch_disables_everything(self):
        with patch.dict(os.environ, {**ENV, 'LIVE_ACTIVITY_ENABLED': 'false'}, clear=True):
            apns._notifier = None
            apns._disabled = False
            self.assertIsNone(apns.get_notifier())
            apns.notify_stage('job1', 'done')  # no-op, no raise

    def test_notify_stage_swallows_internal_errors(self):
        class Exploding:
            def notify(self, *a, **k):
                raise RuntimeError('boom')
        apns.set_notifier(Exploding())
        apns.notify_stage('job1', 'done')


class JobWiringTests(unittest.TestCase):
    """jobs.py reports real stage transitions (notify_stage patched; nothing else is real)."""

    def run_job(self, job, fail=None):
        results = {
            'download_audio': b'audio', 'download_chunk_from_storage': b'audio',
            'get_audio_chunks': [{'id': 'c1', 'chunk_index': 0, 'file_path': 'one'}, {'id': 'c2', 'chunk_index': 1, 'file_path': 'two'}],
            'update_job_status': None, 'update_job_progress': None, 'update_chunk_transcript': None,
            'update_chunks_processed': None, 'increment_retry_count': None, 'update_job_with_results': None,
            'transcribe_audio': {'transcript': 't', 'duration': 30},
            'generate_summary': 's', 'generate_overview': 'o', 'extract_actions': [],
        }
        with ExitStack() as stack:
            for name, value in results.items():
                stack.enter_context(patch.object(jobs, name, return_value=value, side_effect=fail if name == 'transcribe_audio' else None))
            notify = stack.enter_context(patch.object(jobs, 'notify_stage'))
            jobs.process_job(job)
        return [c.args[1] for c in notify.call_args_list], notify

    def test_regular_job_stage_sequence(self):
        stages, _ = self.run_job({'id': 'j', 'audio_url': 'http://x', 'transcription_provider': 'openai'})
        self.assertEqual(stages[0], 'preparing')
        self.assertIn('transcribing', stages)
        self.assertEqual([s for s in stages if s in ('summarizing', 'done')], ['summarizing', 'done'])
        self.assertEqual(stages[-1], 'done')

    def test_chunked_job_reports_chunk_counts(self):
        stages, notify = self.run_job({'id': 'j', 'meeting_id': 'm', 'is_chunked': True, 'total_chunks': 2, 'transcription_provider': 'openai'})
        self.assertEqual((stages[0], stages[-1]), ('preparing', 'done'))
        chunk_calls = [c.kwargs for c in notify.call_args_list if c.args[1] == 'transcribing' and c.kwargs.get('chunk')]
        self.assertEqual([c['chunk'] for c in chunk_calls], [1, 2])
        self.assertEqual({c['total_chunks'] for c in chunk_calls}, {2})

    def test_permanent_failure_sends_failed(self):
        stages, _ = self.run_job({'id': 'j', 'audio_url': 'http://x'}, fail=Exception('invalid audio'))
        self.assertEqual(stages[-1], 'failed')

    def test_retryable_failure_goes_back_to_queued_not_failed(self):
        stages, _ = self.run_job({'id': 'j', 'audio_url': 'http://x'}, fail=Exception('connection reset'))
        self.assertEqual(stages[-1], 'queued')
        self.assertNotIn('failed', stages)


if __name__ == '__main__':
    unittest.main()
