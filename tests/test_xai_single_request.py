import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import httpx
import support
from support import TEST_USER_ID, TEST_AUDIO_URL, chunk_path_for
import jobs
import transcribe
import transcription_provider as tp
import xai_single
from ai_config import get_task_config  # noqa: F401  (ensures module import order)


REAL_CLIENT = httpx.Client


def fake_prepare(payload=b'mp3-bytes'):
    def prepare(inputs, output):
        assert inputs and all(os.path.exists(p) for p in inputs)
        with open(output, 'wb') as f:
            f.write(payload)
    return prepare


class EnvMixin:
    def setUp(self):
        env = patch.dict(os.environ, {'XAI_API_KEY': 'xai-fixture-secret', 'XAI_SINGLE_REQUEST_ENABLED': 'true'})
        env.start()
        self.addCleanup(env.stop)
        self.workroot = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.workroot, True)
        env2 = patch.dict(os.environ, {'XAI_WORK_DIR': self.workroot})
        env2.start()
        self.addCleanup(env2.stop)

    def leftovers(self):
        return os.listdir(self.workroot)


class RunSingleRequestTests(EnvMixin, unittest.TestCase):
    def fetcher(self, data=b'audio'):
        def fetch(dest):
            with open(dest, 'wb') as f:
                f.write(data)
        return fetch

    def run_it(self, fetchers=None, transcribe_side=None, progress=None, **patches):
        fetchers = fetchers or [self.fetcher()]
        result = tp.TranscriptionResult(text='hello', provider='xai')
        with patch.object(xai_single, 'prepare_audio', side_effect=patches.pop('prepare', fake_prepare())) as prep, \
                patch.object(xai_single, 'probe_duration', return_value=patches.pop('duration', 61.5)), \
                patch.object(xai_single, 'transcribe_xai_file', side_effect=transcribe_side or (lambda *a, **k: result)) as send:
            out = xai_single.run_single_request(fetchers, 'it', progress)
        return out, prep, send

    def test_success_uses_one_request_and_cleans_up(self):
        stages = []
        out, prep, send = self.run_it(fetchers=[self.fetcher(), self.fetcher()], progress=lambda p, s: stages.append((p, s)))
        self.assertEqual(out.text, 'hello')
        self.assertEqual(out.duration, 61.5)
        send.assert_called_once()
        self.assertEqual(len(prep.call_args.args[0]), 2)  # both parts joined into one file
        self.assertEqual(send.call_args.args[1], 'it')
        self.assertEqual(self.leftovers(), [])
        self.assertEqual([s for _, s in stages], ['Downloading audio...', 'Preparing audio...', 'Transcribing audio...', 'Transcription complete'])
        self.assertEqual([p for p, _ in stages], sorted(p for p, _ in stages))

    def test_duration_falls_back_to_response_then_size_estimate(self):
        res = tp.TranscriptionResult(text='x', duration=9.0)
        out, *_ = self.run_it(duration=None, transcribe_side=lambda *a, **k: res)
        self.assertEqual(out.duration, 9.0)
        out, *_ = self.run_it(duration=None, fetchers=[self.fetcher(b'x' * 64000)])
        self.assertEqual(out.duration, 2.0)

    def test_too_large_falls_back_without_request(self):
        with patch.dict(os.environ, {'XAI_SINGLE_REQUEST_MAX_BYTES': '4'}):
            out, _, send = self.run_it()
        self.assertIsNone(out)
        send.assert_not_called()
        self.assertEqual(self.leftovers(), [])

    def test_prepare_failure_falls_back(self):
        def boom(inputs, output):
            raise xai_single.AudioPreparationError('ffmpeg is not installed')
        out, _, send = self.run_it(prepare=boom)
        self.assertIsNone(out)
        send.assert_not_called()
        self.assertEqual(self.leftovers(), [])

    def test_non_auth_failure_falls_back(self):
        for status in (None, 413, 429, 500, 400):
            out, *_ = self.run_it(transcribe_side=tp.TranscriptionProviderError('failed', status))
            self.assertIsNone(out, status)
        self.assertEqual(self.leftovers(), [])

    def test_auth_failure_raises_and_cleans_up(self):
        for status in (401, 403):
            with self.assertRaises(tp.TranscriptionProviderError):
                self.run_it(transcribe_side=tp.TranscriptionProviderError('HTTP', status))
        self.assertEqual(self.leftovers(), [])

    def test_missing_key_raises_before_download(self):
        fetch = MagicMock()
        with patch.dict(os.environ, {'XAI_API_KEY': ''}):
            with self.assertRaisesRegex(Exception, 'not configured'):
                xai_single.run_single_request([fetch], None)
        fetch.assert_not_called()

    def test_download_error_propagates_and_cleans_up(self):
        def bad(dest):
            open(dest, 'wb').write(b'partial')
            raise httpx.ReadTimeout('timeout')
        with self.assertRaises(httpx.ReadTimeout):
            self.run_it(fetchers=[bad])
        self.assertEqual(self.leftovers(), [])

    def test_stale_workdirs_swept_but_fresh_kept(self):
        import time
        old = os.path.join(self.workroot, 'snipnote-xai-old'); fresh = os.path.join(self.workroot, 'snipnote-xai-new')
        other = os.path.join(self.workroot, 'unrelated')
        for d in (old, fresh, other):
            os.mkdir(d)
        os.utime(old, (time.time() - 7 * 3600,) * 2)
        os.utime(other, (time.time() - 7 * 3600,) * 2)
        self.run_it()
        self.assertEqual(sorted(self.leftovers()), ['snipnote-xai-new', 'unrelated'])

    def test_kill_switch(self):
        self.assertTrue(xai_single.should_use_single_request('xai'))
        self.assertFalse(xai_single.should_use_single_request('openai'))
        for value in ('false', '0', 'off', 'NO'):
            with patch.dict(os.environ, {'XAI_SINGLE_REQUEST_ENABLED': value}):
                self.assertFalse(xai_single.should_use_single_request('xai'))


class PrepareAudioTests(unittest.TestCase):
    def run_prepare(self, inputs):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, 'audio.mp3')
            def run(cmd, **kwargs):
                open(out, 'wb').write(b'x')
                self.cmd = cmd
                return MagicMock(returncode=0, stderr='')
            with patch.object(xai_single.subprocess, 'run', side_effect=run):
                xai_single.prepare_audio(inputs, out)

    def test_multiple_inputs_use_concat_filter_in_order(self):
        self.run_prepare(['/a', '/b', '/c'])
        cmd = self.cmd
        self.assertEqual([cmd[i + 1] for i, v in enumerate(cmd) if v == '-i'], ['/a', '/b', '/c'])
        graph = cmd[cmd.index('-filter_complex') + 1]
        self.assertIn('[a0][a1][a2]concat=n=3:v=0:a=1[out]', graph)
        self.assertIn('libmp3lame', cmd)
        self.assertEqual(cmd[cmd.index('-ac') + 1], '1')
        self.assertEqual(cmd[cmd.index('-ar') + 1], '16000')

    def test_single_input_has_no_concat(self):
        self.run_prepare(['/a'])
        self.assertNotIn('-filter_complex', self.cmd)
        self.assertIn('-af', self.cmd)

    def test_missing_ffmpeg_and_failures_raise_preparation_error(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, 'a.mp3')
            with patch.object(xai_single.subprocess, 'run', side_effect=FileNotFoundError):
                with self.assertRaises(xai_single.AudioPreparationError):
                    xai_single.prepare_audio(['/a'], out)
            with patch.object(xai_single.subprocess, 'run', return_value=MagicMock(returncode=1, stderr='bad')):
                with self.assertRaises(xai_single.AudioPreparationError):
                    xai_single.prepare_audio(['/a'], out)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg/ffprobe not installed')
    def test_real_ffmpeg_joins_two_files(self):
        with tempfile.TemporaryDirectory() as d:
            parts = []
            for i, (rate, ch) in enumerate([(44100, 2), (22050, 1)]):
                path = os.path.join(d, f'p{i}.wav')
                os.system(f'ffmpeg -loglevel error -f lavfi -i "sine=frequency=440:duration=2:sample_rate={rate}" -ac {ch} {path}')
                parts.append(path)
            out = os.path.join(d, 'out.mp3')
            xai_single.prepare_audio(parts, out)
            self.assertAlmostEqual(xai_single.probe_duration(out), 4.0, delta=0.3)


class TranscribeXaiFileTests(EnvMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        import ai_config
        db = patch.object(ai_config.supabase, 'table')
        db.start().return_value.select.return_value.execute.side_effect = Exception('offline')
        self.addCleanup(db.stop)
        ai_config._cache = {}
        ai_config._cache_loaded_at = float('-inf')
        self.path = os.path.join(self.workroot, 'audio.mp3')
        with open(self.path, 'wb') as f:
            f.write(b'mp3-bytes')
        self.requests = []
        self.timeouts = []

    def send(self, statuses, sleeps=None, **kwargs):
        def handle(request):
            self.requests.append(request)
            status = statuses[min(len(self.requests) - 1, len(statuses) - 1)]
            body = {'text': 'full transcript', 'words': [{'text': 'full'}], 'duration': 12.5} if status == 200 else {'error': 'secret'}
            return httpx.Response(status, json=body)
        real = REAL_CLIENT
        def make(**kw):
            self.timeouts.append(kw.get('timeout'))
            return real(transport=httpx.MockTransport(handle))
        sleeps = [] if sleeps is None else sleeps
        with patch('transcription_provider.httpx.Client', side_effect=make):
            return tp.transcribe_xai_file(self.path, 'it', timeout=900, sleep=sleeps.append, **kwargs)

    def test_single_multipart_request_with_options(self):
        result = self.send([200])
        self.assertEqual((result.text, result.duration, result.words, result.provider), ('full transcript', 12.5, [{'text': 'full'}], 'xai'))
        self.assertEqual(len(self.requests), 1)
        body = self.requests[0].content
        for value in [b'mp3-bytes', b'name="language"\r\n\r\nit', b'name="format"\r\n\r\ntrue', b'filename="audio.mp3"', b'audio/mpeg', b'grok-voice-transcribe-2.0']:
            self.assertIn(value, body)
        self.assertLess(body.index(b'name="format"'), body.index(b'name="file"'))
        self.assertEqual(self.timeouts[0].read, 900)
        self.assertEqual(self.timeouts[0].write, 900)

    def test_transient_errors_retry_then_succeed(self):
        sleeps = []
        self.assertEqual(self.send([503, 429, 200], sleeps).text, 'full transcript')
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(len(sleeps), 2)
        for request in self.requests:  # file rewound every attempt
            self.assertIn(b'mp3-bytes', request.content)

    def test_transient_exhaustion_raises_last_error(self):
        with self.assertRaisesRegex(tp.TranscriptionProviderError, '500') as ctx:
            self.send([500], max_attempts=2)
        self.assertEqual(len(self.requests), 2)
        self.assertNotIn('secret', str(ctx.exception))

    def test_auth_and_client_errors_do_not_retry(self):
        for status in (401, 403, 413, 400):
            self.requests.clear()
            with self.assertRaises(tp.TranscriptionProviderError):
                self.send([status])
            self.assertEqual(len(self.requests), 1, status)

    def test_timeout_is_transient(self):
        def handle(request):
            self.requests.append(request)
            if len(self.requests) == 1:
                raise httpx.ReadTimeout('slow', request=request)
            return httpx.Response(200, json={'text': 'ok'})
        with patch('transcription_provider.httpx.Client', side_effect=lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handle))):
            result = tp.transcribe_xai_file(self.path, None, timeout=1, sleep=lambda s: None)
        self.assertEqual(result.text, 'ok')
        self.assertEqual(len(self.requests), 2)
        self.assertNotIn(b'name="format"', self.requests[0].content)


class JobRoutingTests(EnvMixin, unittest.TestCase):
    """process_job / process_chunked_job routing between single-request and chunked paths."""
    def run_job(self, provider, chunked=False, single=None):
        job = {'id': 'job', 'user_id': TEST_USER_ID, 'meeting_id': 'm', 'audio_url': TEST_AUDIO_URL, 'is_chunked': chunked,
               'total_chunks': 2, 'language': 'it', 'transcription_provider': provider}
        chunks = [{'id': 'c2', 'chunk_index': 1, 'file_path': chunk_path_for(1), 'duration_seconds': 5},
                  {'id': 'c1', 'chunk_index': 0, 'file_path': chunk_path_for(0), 'duration_seconds': 7}]
        names = ['update_job_status', 'update_job_progress', 'update_chunk_transcript', 'update_chunks_processed',
                 'increment_retry_count', 'update_job_with_results']
        m = {n: MagicMock() for n in names}
        with patch.multiple(jobs, **m), \
                patch.object(jobs, 'get_audio_chunks', return_value=chunks), \
                patch.object(jobs, 'download_audio', return_value=b'audio') as dl, \
                patch.object(jobs, 'download_chunk_from_storage', return_value=b'audio'), \
                patch.object(jobs, 'transcribe_audio', return_value={'transcript': 't', 'duration': 30}) as ta, \
                patch.object(jobs, 'generate_summary', return_value='s'), patch.object(jobs, 'generate_overview', return_value='o'), \
                patch.object(jobs, 'extract_actions', return_value=[]), \
                patch.object(jobs.xai_single, 'run_single_request', return_value=single) as rs:
            jobs.process_job(job)
        return m, ta, rs, dl

    def result(self):
        return tp.TranscriptionResult(text='whole', duration=77.0, provider='xai')

    def test_regular_xai_uses_single_request(self):
        m, ta, rs, dl = self.run_job('xai', single=self.result())
        rs.assert_called_once()
        ta.assert_not_called(); dl.assert_not_called()
        kw = m['update_job_with_results'].call_args.kwargs
        self.assertEqual((kw['transcript'], kw['duration']), ('whole', 77.0))

    def test_regular_xai_fallback_uses_old_path(self):
        m, ta, rs, dl = self.run_job('xai', single=None)
        ta.assert_called_once(); dl.assert_called_once()
        self.assertEqual(ta.call_args.kwargs['provider'], 'xai')
        self.assertEqual(m['update_job_with_results'].call_args.kwargs['transcript'], 't')
        m['increment_retry_count'].assert_not_called()

    def test_regular_openai_never_touches_single_request(self):
        m, ta, rs, dl = self.run_job('openai', single=self.result())
        rs.assert_not_called()
        ta.assert_called_once()
        self.assertEqual(ta.call_args.kwargs['provider'], 'openai')

    def test_chunked_xai_single_request_in_chunk_order(self):
        m, ta, rs, _ = self.run_job('xai', chunked=True, single=self.result())
        ta.assert_not_called()
        fetchers = rs.call_args.args[0]
        self.assertEqual(len(fetchers), 2)
        with patch.object(jobs, 'download_chunk_from_storage', return_value=b'x') as dl, tempfile.TemporaryDirectory() as d:
            for i, f in enumerate(fetchers):
                f(os.path.join(d, str(i)))
        self.assertEqual([c.args[0] for c in dl.call_args_list], [chunk_path_for(0), chunk_path_for(1)])
        m['update_chunk_transcript'].assert_not_called()
        m['update_chunks_processed'].assert_called_once_with('job', 2)
        kw = m['update_job_with_results'].call_args.kwargs
        self.assertEqual((kw['transcript'], kw['duration']), ('whole', 12))  # chunk durations win, as before

    def test_chunked_xai_fallback_uses_parallel_path(self):
        m, ta, rs, _ = self.run_job('xai', chunked=True, single=None)
        self.assertEqual(ta.call_count, 2)
        self.assertEqual(m['update_job_with_results'].call_args.kwargs['transcript'], 't\nt')

    def test_chunked_openai_unchanged(self):
        m, ta, rs, _ = self.run_job('openai', chunked=True, single=self.result())
        rs.assert_not_called()
        self.assertEqual(ta.call_count, 2)

    def test_single_request_auth_error_fails_job_without_fallback(self):
        job = {'id': 'job', 'user_id': TEST_USER_ID, 'audio_url': TEST_AUDIO_URL, 'transcription_provider': 'xai'}
        err = tp.TranscriptionProviderError('xAI transcription returned HTTP 401', 401)
        m = {n: MagicMock() for n in ('update_job_status', 'update_job_progress', 'increment_retry_count')}
        with patch.multiple(jobs, **m), \
                patch.object(jobs.xai_single, 'run_single_request', side_effect=err), \
                patch.object(jobs, 'transcribe_audio') as ta:
            jobs.process_job(job)
        ta.assert_not_called()
        m['increment_retry_count'].assert_not_called()
        self.assertEqual(m['update_job_status'].call_args.kwargs['status'], 'failed')


class DownloadToFileTests(unittest.TestCase):
    """Legacy audio_url downloads go through the service-key storage API (details: test_download_hardening)."""
    def test_streams_to_disk_in_blocks(self):
        payload = b'a' * (3 * 1024 * 1024 + 5)
        client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=payload)))
        with patch.object(support.supabase_client, 'storage_http', client), tempfile.TemporaryDirectory() as d:
            dest = os.path.join(d, 'f')
            jobs.download_audio_to_file(TEST_AUDIO_URL, dest, TEST_USER_ID)
            self.assertEqual(open(dest, 'rb').read(), payload)

    def test_http_error_raises(self):
        client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
        with patch.object(support.supabase_client, 'storage_http', client), tempfile.TemporaryDirectory() as d:
            with self.assertRaises(support.supabase_client.StorageError):
                jobs.download_audio_to_file(TEST_AUDIO_URL, os.path.join(d, 'f'), TEST_USER_ID)
            self.assertFalse(os.path.exists(os.path.join(d, 'f')))


class OpenAIPathUnchangedTests(unittest.TestCase):
    def test_transcribe_audio_openai_still_chunks_and_never_uses_single_request(self):
        with patch.object(transcribe, 'chunk_audio', return_value=[b'one', b'two']), \
                patch.object(transcribe, 'create_provider_transcription', return_value='t') as create, \
                patch.object(xai_single, 'run_single_request') as rs:
            out = transcribe.transcribe_audio(b'x' * (transcribe.MAX_CHUNK_SIZE_BYTES + 1), 'a.m4a', provider='openai')
        rs.assert_not_called()
        self.assertEqual(create.call_count, 2)
        self.assertEqual(out['transcript'], 't t')


if __name__ == '__main__':
    unittest.main()
