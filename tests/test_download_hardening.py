"""Worker download hardening: SSRF, cross-user access, redirects, size caps (audit A2/A4). No network."""
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import httpx

import support
from support import TEST_USER_ID, TEST_MEETING_ID, TEST_AUDIO_URL, chunk_path_for
import audio_access as aa
import jobs
import supabase_client as sc

OTHER_USER = '5d4c3b2a-1f0e-4d9c-8b7a-6f5e4d3c2b1a'
BASE = 'https://offline.invalid'


def url(path, kind='public', bucket='recordings', host=BASE, query=''):
    return f'{host}/storage/v1/object/{kind}/{bucket}/{path}{query}'


class UrlValidationTests(unittest.TestCase):
    def ok(self, u, user=TEST_USER_ID):
        return aa.storage_path_from_url(u, user)

    def bad(self, u, user=TEST_USER_ID):
        with self.assertRaises(aa.AudioAccessError) as ctx:
            aa.storage_path_from_url(u, user)
        if u:
            self.assertNotIn(u, str(ctx.exception))  # the URL may carry a signed token
        return str(ctx.exception)

    def test_app_generated_url_is_accepted(self):
        self.assertEqual(self.ok(TEST_AUDIO_URL), f'{TEST_USER_ID}/{TEST_MEETING_ID}.m4a')

    def test_all_three_storage_url_kinds_and_signed_token(self):
        for kind in ('public', 'sign', 'authenticated'):
            self.assertEqual(self.ok(url(f'{TEST_USER_ID}/a.m4a', kind, query='?token=abc')), f'{TEST_USER_ID}/a.m4a')

    def test_user_id_comparison_is_case_insensitive(self):
        # Storage paths are case-sensitive: the path is used exactly as stored, only the owner match ignores case.
        self.assertEqual(self.ok(url(f'{TEST_USER_ID.upper()}/a.m4a'), TEST_USER_ID), f'{TEST_USER_ID.upper()}/a.m4a')
        self.assertEqual(self.ok(url(f'{TEST_USER_ID}/a.m4a'), TEST_USER_ID.upper()), f'{TEST_USER_ID}/a.m4a')

    def test_ssrf_targets_are_rejected(self):
        for target in ('http://169.254.169.254/latest/meta-data', 'http://127.0.0.1:8100/', 'https://localhost/x',
                       'https://evil.example/storage/v1/object/public/recordings/%s/a.m4a' % TEST_USER_ID,
                       'file:///etc/passwd', 'ftp://offline.invalid/x', '', None, 'not a url'):
            self.bad(target)

    def test_host_tricks(self):
        self.bad(f'https://offline.invalid@evil.example/storage/v1/object/public/recordings/{TEST_USER_ID}/a.m4a')
        self.bad(f'https://user:pw@offline.invalid/storage/v1/object/public/recordings/{TEST_USER_ID}/a.m4a')
        self.bad(f'https://offline.invalid.evil.example/storage/v1/object/public/recordings/{TEST_USER_ID}/a.m4a')
        self.bad(f'https://offline.invalid:8443/storage/v1/object/public/recordings/{TEST_USER_ID}/a.m4a')
        self.bad(f'http://offline.invalid/storage/v1/object/public/recordings/{TEST_USER_ID}/a.m4a')  # http only if SUPABASE_URL is http

    def test_other_users_objects_are_rejected(self):
        self.bad(url(f'{OTHER_USER}/a.m4a'))
        self.bad(url(f'{TEST_USER_ID}x/a.m4a'))
        self.bad(url('a.m4a'))

    def test_path_traversal_and_encoding_tricks(self):
        self.bad(url(f'{TEST_USER_ID}/../{OTHER_USER}/a.m4a'))
        self.bad(url(f'{TEST_USER_ID}/%2e%2e/{OTHER_USER}/a.m4a'))
        self.bad(url(f'{TEST_USER_ID}%2F..%2F{OTHER_USER}%2Fa.m4a'))
        self.bad(url(f'{TEST_USER_ID}/%252e%252e/a.m4a'))   # double encoding
        self.bad(url(f'{TEST_USER_ID}/a%00.m4a'))
        self.bad(url(f'{TEST_USER_ID}//a.m4a'))
        self.bad(url(f'{TEST_USER_ID}/a\\b.m4a'))

    def test_other_bucket_or_endpoint_is_rejected(self):
        self.bad(url(f'{TEST_USER_ID}/a.m4a', bucket='avatars'))
        self.bad(f'{BASE}/rest/v1/transcription_jobs?select=*')
        self.bad(f'{BASE}/storage/v1/object/list/recordings')
        self.bad(f'{BASE}/auth/v1/admin/users')

    def test_custom_domain_needs_explicit_allowlist(self):
        custom = url(f'{TEST_USER_ID}/a.m4a', host='https://cdn.snipnote.app')
        self.bad(custom)
        with patch.dict(os.environ, {'AUDIO_URL_ALLOWED_HOSTS': 'cdn.snipnote.app, other.example'}):
            self.assertEqual(self.ok(custom), f'{TEST_USER_ID}/a.m4a')
            self.bad(url(f'{OTHER_USER}/a.m4a', host='https://cdn.snipnote.app'))

    def test_chunk_paths(self):
        self.assertEqual(aa.validate_storage_path(chunk_path_for(0), TEST_USER_ID), chunk_path_for(0))
        for path in (f'{OTHER_USER}/x.m4a', 'one', '', None, f'{TEST_USER_ID}/../{OTHER_USER}/x', f'/{TEST_USER_ID}/x',
                     f'{TEST_USER_ID}/', 'https://offline.invalid/x'):
            with self.assertRaises(aa.AudioAccessError):
                aa.validate_storage_path(path, TEST_USER_ID)
        with self.assertRaises(aa.AudioAccessError):
            aa.validate_storage_path(chunk_path_for(0), 'not-a-uuid')

    def test_error_text_is_classified_permanent(self):
        self.assertFalse(jobs.is_retryable_error(aa.AudioAccessError('URL host is not the Supabase project host')))


class StreamingDownloadTests(unittest.TestCase):
    def client(self, handler):
        return httpx.Client(transport=httpx.MockTransport(handler))

    def fetch(self, handler, **kw):
        with patch.object(sc, 'storage_http', self.client(handler)), tempfile.TemporaryDirectory() as d:
            dest = os.path.join(d, 'f')
            try:
                sc.download_storage_object_to_file(f'{TEST_USER_ID}/a.m4a', dest, **kw)
            finally:
                self.leftover = os.path.exists(dest)
            return open(dest, 'rb').read()

    def test_request_goes_to_supabase_with_service_key_by_path(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, content=b'audio')
        self.assertEqual(self.fetch(handler), b'audio')
        req = seen[0]
        self.assertEqual(str(req.url), f'{BASE}/storage/v1/object/authenticated/recordings/{TEST_USER_ID}/a.m4a')
        self.assertEqual(req.headers['authorization'], 'Bearer offline-service-key')

    def test_redirects_are_not_followed(self):
        seen = []

        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(302, headers={'location': 'http://169.254.169.254/latest/meta-data'})
        with self.assertRaises(sc.StorageError):
            self.fetch(handler)
        self.assertEqual(len(seen), 1)
        self.assertFalse(self.leftover)

    def test_content_length_over_cap_is_refused_before_reading(self):
        def handler(request):
            return httpx.Response(200, headers={'content-length': '2000'}, content=b'x' * 2000)
        with patch.dict(os.environ, {'MAX_DOWNLOAD_BYTES': '1000'}):
            with self.assertRaises(sc.DownloadTooLarge) as ctx:
                self.fetch(handler)
        self.assertFalse(jobs.is_retryable_error(ctx.exception))
        self.assertFalse(self.leftover)

    def test_streamed_overflow_without_content_length_is_cut_off(self):
        def handler(request):
            return httpx.Response(200, content=(b'x' * 1024 for _ in range(4096)))  # 4 MiB, chunked, no length
        with patch.dict(os.environ, {'MAX_DOWNLOAD_BYTES': str(2 * 1024 * 1024)}):
            with self.assertRaises(sc.DownloadTooLarge):
                self.fetch(handler)
        self.assertFalse(self.leftover)

    def test_default_cap_is_300_mib_and_bytes_variant_is_capped_too(self):
        self.assertEqual(aa.max_download_bytes(), 300 * 1024 * 1024)
        with patch.object(sc, 'storage_http', self.client(lambda r: httpx.Response(200, content=b'x' * 50))):
            self.assertEqual(sc.download_storage_object_bytes('u/a', max_bytes=50), b'x' * 50)
            with self.assertRaises(sc.DownloadTooLarge):
                sc.download_storage_object_bytes('u/a', max_bytes=49)

    def test_transport_error_is_sanitised(self):
        def handler(request):
            raise httpx.ConnectError('boom https://x?token=secret')
        with self.assertRaises(sc.StorageError) as ctx:
            self.fetch(handler)
        self.assertNotIn('secret', str(ctx.exception))


class WorkerJobTests(unittest.TestCase):
    def run_job(self, job):
        names = ['update_job_status', 'update_job_progress', 'increment_retry_count', 'update_job_with_results',
                 'update_chunk_transcript', 'update_chunks_processed']
        m = {n: MagicMock() for n in names}
        boom = MagicMock(side_effect=AssertionError('no HTTP request may happen'))
        with patch.multiple(jobs, **m), patch.object(sc, 'storage_http', MagicMock(stream=boom, post=boom, get=boom)), \
                patch.object(jobs, 'notify_stage'), patch.object(jobs, 'transcribe_audio', return_value={'transcript': 't', 'duration': 3}) as ta, \
                patch.object(jobs, 'generate_summary', return_value='s'), patch.object(jobs, 'generate_overview', return_value='o'), \
                patch.object(jobs, 'extract_actions', return_value=[]), \
                patch.dict(os.environ, {'XAI_SINGLE_REQUEST_ENABLED': 'false'}), \
                patch.object(jobs, 'is_retryable_error', wraps=jobs.is_retryable_error):
            jobs.process_job(job)
        return m, ta, boom

    def test_ssrf_url_fails_job_permanently_without_any_request(self):
        for evil in ('http://169.254.169.254/latest/meta-data', f'https://evil.example/storage/v1/object/public/recordings/{TEST_USER_ID}/a.m4a',
                     url(f'{OTHER_USER}/a.m4a')):
            job = {'id': 'job', 'user_id': TEST_USER_ID, 'meeting_id': 'm', 'audio_url': evil, 'transcription_provider': 'openai'}
            m, ta, boom = self.run_job(job)
            m['increment_retry_count'].assert_not_called()   # permanent: no retry loop
            self.assertEqual(m['update_job_status'].call_args.kwargs['status'], 'failed')
            self.assertIn('invalid audio location', m['update_job_status'].call_args.kwargs['error'])
            ta.assert_not_called()
            boom.assert_not_called()

    def test_legit_url_is_downloaded_by_path(self):
        job = {'id': 'job', 'user_id': TEST_USER_ID, 'meeting_id': 'm', 'audio_url': TEST_AUDIO_URL, 'transcription_provider': 'openai'}
        with patch.object(jobs, 'download_storage_object_bytes', return_value=b'audio') as dl:
            m, ta, _ = self.run_job(job)
        dl.assert_called_once_with(f'{TEST_USER_ID}/{TEST_MEETING_ID}.m4a')
        ta.assert_called_once()
        m['update_job_with_results'].assert_called_once()

    def test_chunks_of_other_users_or_traversal_fail_before_any_download(self):
        for bad in (f'{OTHER_USER}/x_chunk_0.m4a', f'{TEST_USER_ID}/../{OTHER_USER}/x.m4a', 'one'):
            chunks = [{'id': 'c1', 'chunk_index': 0, 'file_path': chunk_path_for(0)}, {'id': 'c2', 'chunk_index': 1, 'file_path': bad}]
            job = {'id': 'job', 'user_id': TEST_USER_ID, 'meeting_id': TEST_MEETING_ID, 'is_chunked': True, 'total_chunks': 2}
            with patch.object(jobs, 'get_audio_chunks', return_value=chunks) as gac, patch.object(jobs, 'download_chunk_from_storage') as dl:
                m, ta, boom = self.run_job(job)
            gac.assert_called_once_with(TEST_MEETING_ID, TEST_USER_ID)
            dl.assert_not_called()
            self.assertEqual(m['update_job_status'].call_args.kwargs['status'], 'failed')
            m['increment_retry_count'].assert_not_called()

    def test_too_many_chunks_fails_job(self):
        chunks = [{'id': f'c{i}', 'chunk_index': i, 'file_path': chunk_path_for(i)} for i in range(5)]
        job = {'id': 'job', 'user_id': TEST_USER_ID, 'meeting_id': TEST_MEETING_ID, 'is_chunked': True, 'total_chunks': 5}
        with patch.dict(os.environ, {'MAX_CHUNKS_PER_JOB': '4'}), patch.object(jobs, 'get_audio_chunks', return_value=chunks), \
                patch.object(jobs, 'download_chunk_from_storage') as dl:
            m, _, _ = self.run_job(job)
        dl.assert_not_called()
        self.assertEqual(m['update_job_status'].call_args.kwargs['status'], 'failed')


class ChunkQueryTests(unittest.TestCase):
    def test_get_audio_chunks_filters_by_meeting_and_user(self):
        table = MagicMock()
        table.select.return_value = table; table.eq.return_value = table; table.order.return_value = table
        table.execute.return_value = MagicMock(data=[{'id': 'c'}])
        with patch.object(sc.supabase, 'table', return_value=table) as t:
            self.assertEqual(sc.get_audio_chunks(TEST_MEETING_ID, TEST_USER_ID), [{'id': 'c'}])
        t.assert_called_once_with('audio_chunks')
        self.assertEqual([c.args for c in table.eq.call_args_list], [('meeting_id', TEST_MEETING_ID), ('user_id', TEST_USER_ID)])

    def test_user_id_is_mandatory(self):
        with patch.object(sc.supabase, 'table') as t:
            for missing in (None, ''):
                with self.assertRaises(ValueError):
                    sc.get_audio_chunks(TEST_MEETING_ID, missing)
            with self.assertRaises(TypeError):
                sc.get_audio_chunks(TEST_MEETING_ID)
        t.assert_not_called()


if __name__ == '__main__':
    unittest.main()
