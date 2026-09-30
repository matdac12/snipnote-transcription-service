"""Server-side minutes debit in the worker (SERVER_MINUTES_DEBIT_ENABLED). No network, no database."""
import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import support
from support import TEST_USER_ID, TEST_MEETING_ID, TEST_AUDIO_URL, chunk_path_for
import jobs
import minutes
import supabase_client as sc
import transcription_provider as tp

JOB_ID = '3f2e1d0c-9b8a-4c7d-8e6f-5a4b3c2d1e0f'


class Env(unittest.TestCase):
    enabled = True

    def setUp(self):
        env = patch.dict(os.environ, {'SERVER_MINUTES_DEBIT_ENABLED': 'true' if self.enabled else '', 'XAI_SINGLE_REQUEST_ENABLED': 'true'})
        env.start()
        self.addCleanup(env.stop)
        minutes._last_sweep = 0.0


class Pure(Env):
    def test_minutes_rounding_matches_the_app(self):
        for seconds, expected in ((None, 0), (0, 0), (-5, 0), (0.4, 1), (1, 1), (60, 1), (60.01, 2), (125, 3), (3600, 60), (5423.5, 91),
                                  ('abc', 0), (float('inf'), 0), (float('nan'), 0)):
            self.assertEqual(minutes.minutes_for_seconds(seconds), expected, seconds)

    def test_billing_seconds_prefers_measured(self):
        self.assertEqual(minutes.choose_billing_seconds(300.0, [10, 20]), 300.0)
        self.assertEqual(minutes.choose_billing_seconds(None, [None, 0, 20, 30]), 20.0)
        self.assertEqual(minutes.choose_billing_seconds(0, [-1, float('nan')]), None)

    def test_flag_default_is_off(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('SERVER_MINUTES_DEBIT_ENABLED', None)
            self.assertFalse(minutes.debit_enabled())
        for value in ('true', '1', 'YES', 'on'):
            with patch.dict(os.environ, {'SERVER_MINUTES_DEBIT_ENABLED': value}):
                self.assertTrue(minutes.debit_enabled())
        for value in ('', 'false', '0', 'no', 'banana'):
            with patch.dict(os.environ, {'SERVER_MINUTES_DEBIT_ENABLED': value}):
                self.assertFalse(minutes.debit_enabled())


class WorkerBase(Env):
    def run_job(self, job, *, single=None, probe=None, chunk_probe=None, rpc=None, chunks=None, transcribe_duration=30, uploaded=None):
        names = ['update_job_status', 'update_job_progress', 'increment_retry_count', 'update_job_with_results', 'update_chunks_processed', 'update_chunk_transcript']
        self.m = {n: MagicMock() for n in names}
        self.rpc = MagicMock(side_effect=rpc) if rpc else MagicMock(return_value={'status': 'debited', 'minutes_debited': 1})
        with patch.multiple(jobs, **self.m), patch.object(sc, 'debit_minutes_for_job', self.rpc), \
                patch.object(jobs.minutes.db, 'debit_minutes_for_job', self.rpc), \
                patch.object(jobs, 'notify_stage'), \
                patch.object(jobs, 'download_audio', return_value=b'audio'), patch.object(jobs, 'download_chunk_from_storage', return_value=b'audio'), \
                patch.object(jobs, 'get_audio_chunks', return_value=chunks or []), \
                patch.object(jobs, 'measure_audio_seconds', side_effect=(chunk_probe if callable(chunk_probe) else (lambda data: probe if chunk_probe is None else chunk_probe))) as self.probe_mock, \
                patch.object(jobs, 'transcribe_audio', return_value={'transcript': 't', 'duration': transcribe_duration}), \
                patch.object(jobs.xai_single, 'run_single_request', return_value=single), \
                patch.object(jobs, 'generate_summary', return_value='s'), patch.object(jobs, 'generate_overview', return_value='o'), patch.object(jobs, 'extract_actions', return_value=[]):
            jobs.process_job(job)
        return self.m['update_job_with_results'].call_args.kwargs if self.m['update_job_with_results'].called else None

    def regular(self, provider='openai', **extra):
        return {'id': JOB_ID, 'user_id': TEST_USER_ID, 'meeting_id': TEST_MEETING_ID, 'audio_url': TEST_AUDIO_URL,
                'is_chunked': False, 'transcription_provider': provider, **extra}

    def chunked(self, provider='openai', **extra):
        return {'id': JOB_ID, 'user_id': TEST_USER_ID, 'meeting_id': TEST_MEETING_ID, 'is_chunked': True, 'total_chunks': 2,
                'transcription_provider': provider, **extra}


class DisabledTests(WorkerBase):
    enabled = False

    def test_nothing_changes_when_the_flag_is_off(self):
        kw = self.run_job(self.regular(), probe=125.0)
        self.assertNotIn('billable_seconds', kw)
        self.rpc.assert_not_called()
        self.assertEqual(kw['duration'], 125.0)   # stored duration still benefits from the measurement

    def test_chunked_flag_off_does_not_even_probe_chunks(self):
        chunks = [{'id': 'c1', 'chunk_index': 0, 'file_path': chunk_path_for(0)}, {'id': 'c2', 'chunk_index': 1, 'file_path': chunk_path_for(1)}]
        self.run_job(self.chunked(), chunks=chunks)
        # measure_audio_seconds is only reached through process_single_chunk, which run_job's transcribe path
        # exercises; with the flag off it must not be called for chunks (billing-only work).
        self.probe_mock.assert_not_called()
        self.assertNotIn('billable_seconds', self.m['update_job_with_results'].call_args.kwargs)
        self.rpc.assert_not_called()

    def test_sweep_is_a_noop(self):
        with patch.object(sc, 'list_undebited_completed_jobs') as listing:
            self.assertEqual(minutes.sweep_undebited(force=True), {'checked': 0, 'debited': 0, 'errors': 0})
        listing.assert_not_called()


class RegularJobTests(WorkerBase):
    def test_measured_duration_is_billed_once_keyed_by_meeting_id(self):
        kw = self.run_job(self.regular(), probe=3725.0, transcribe_duration=900)   # estimate 900 s would under-bill by 4x
        self.assertEqual((kw['billable_seconds'], kw['duration']), (3725.0, 3725.0))
        self.rpc.assert_called_once_with(TEST_USER_ID, TEST_MEETING_ID, JOB_ID, 63, 3725.0, 'openai')

    def test_xai_single_request_duration_is_used(self):
        single = tp.TranscriptionResult(text='whole', duration=125.0, provider='xai')
        kw = self.run_job(self.regular('xai'), single=single)
        self.assertEqual(kw['billable_seconds'], 125.0)
        self.rpc.assert_called_once_with(TEST_USER_ID, TEST_MEETING_ID, JOB_ID, 3, 125.0, 'xai')

    def test_unmeasured_falls_back_to_reported_then_estimate(self):
        kw = self.run_job(self.regular(duration=500.0), probe=None, transcribe_duration=30)
        self.assertEqual(kw['billable_seconds'], 500.0)
        kw = self.run_job(self.regular(), probe=None, transcribe_duration=30)
        self.assertEqual(kw['billable_seconds'], 30)
        self.assertEqual(kw['duration'], 30)       # stored duration unchanged when nothing was measured

    def test_rpc_failure_never_fails_the_job(self):
        kw = self.run_job(self.regular(), probe=60.0, rpc=Exception('permission denied for function debit_minutes_for_job'))
        self.assertIsNotNone(kw)
        self.m['increment_retry_count'].assert_not_called()
        for call in self.m['update_job_status'].call_args_list:
            self.assertNotEqual(call.kwargs.get('status'), 'failed')

    def test_zero_length_audio_is_not_debited(self):
        self.run_job(self.regular(), probe=None, transcribe_duration=0)
        self.rpc.assert_not_called()

    def test_debit_happens_after_results_are_saved(self):
        order = []
        with patch.object(jobs, 'update_job_with_results', side_effect=lambda **k: order.append('saved')):
            self.rpc = MagicMock(side_effect=lambda *a: order.append('debit') or {})
            with patch.object(jobs.minutes.db, 'debit_minutes_for_job', self.rpc), patch.object(jobs, 'update_job_status'), patch.object(jobs, 'update_job_progress'), \
                    patch.object(jobs, 'notify_stage'), patch.object(jobs, 'download_audio', return_value=b'a'), patch.object(jobs, 'measure_audio_seconds', return_value=10.0), \
                    patch.object(jobs, 'transcribe_audio', return_value={'transcript': 't', 'duration': 1}), patch.object(jobs, 'generate_summary', return_value='s'), \
                    patch.object(jobs, 'generate_overview', return_value='o'), patch.object(jobs, 'extract_actions', return_value=[]):
                jobs.process_job(self.regular())
        self.assertEqual(order, ['saved', 'debit'])

    def test_job_that_fails_is_never_debited(self):
        with patch.object(jobs, 'download_audio', side_effect=Exception('invalid audio')):
            self.run_job_failing()
        self.rpc.assert_not_called()

    def run_job_failing(self):
        self.rpc = MagicMock()
        with patch.object(jobs.minutes.db, 'debit_minutes_for_job', self.rpc), patch.object(jobs, 'update_job_status'), patch.object(jobs, 'update_job_progress'), \
                patch.object(jobs, 'notify_stage'), patch.object(jobs, 'increment_retry_count'):
            jobs.process_job(self.regular())


class ChunkedJobTests(WorkerBase):
    chunk_rows = [{'id': 'c1', 'chunk_index': 0, 'file_path': chunk_path_for(0), 'duration_seconds': 50},
                  {'id': 'c2', 'chunk_index': 1, 'file_path': chunk_path_for(1), 'duration_seconds': 50}]

    def test_single_request_duration_wins(self):
        single = tp.TranscriptionResult(text='whole', duration=240.0, provider='xai')
        kw = self.run_job(self.chunked('xai', duration=100.0), single=single, chunks=[dict(c) for c in self.chunk_rows])
        self.assertEqual((kw['billable_seconds'], kw['duration']), (240.0, 100.0))   # stored duration keeps preferring the app's
        self.rpc.assert_called_once_with(TEST_USER_ID, TEST_MEETING_ID, JOB_ID, 4, 240.0, 'xai')

    def test_chunks_are_probed_and_summed_when_no_single_request(self):
        kw = self.run_job(self.chunked(duration=100.0), chunks=[dict(c) for c in self.chunk_rows], chunk_probe=lambda data: 150.0)
        self.assertEqual(kw['billable_seconds'], 300.0)

    def test_unprobeable_chunk_falls_back_to_the_apps_duration(self):
        results = iter([150.0, None])
        kw = self.run_job(self.chunked(duration=100.0), chunks=[dict(c) for c in self.chunk_rows], chunk_probe=lambda data: next(results))
        self.assertEqual(kw['billable_seconds'], 100.0)
        kw = self.run_job(self.chunked(), chunks=[dict(c) for c in self.chunk_rows], chunk_probe=lambda data: None)
        self.assertEqual(kw['billable_seconds'], 100.0)    # sum of the rows' duration_seconds


class UploadedJobTests(Env):
    def test_segmented_upload_bills_the_probed_duration(self):
        job = {'id': JOB_ID, 'user_id': TEST_USER_ID, 'meeting_id': TEST_MEETING_ID, 'storage_path': f'{TEST_USER_ID}/{TEST_MEETING_ID}.m4a',
               'expected_bytes': 20, 'is_chunked': False, 'transcription_provider': 'openai', 'duration': None}
        m = {n: MagicMock() for n in ('update_job_status', 'update_job_progress', 'increment_retry_count', 'update_job_with_results')}
        rpc = MagicMock(return_value={'status': 'debited'})

        def download(path, dest):
            with open(dest, 'wb') as f:
                f.write(b'x' * 20)
        with patch.multiple(jobs, **m), patch.object(jobs.minutes.db, 'debit_minutes_for_job', rpc), patch.object(jobs, 'notify_stage'), \
                patch.object(jobs, 'download_storage_object_to_file', side_effect=download), \
                patch.object(jobs.xai_single, 'probe_duration', return_value=5400.0), \
                patch.dict(os.environ, {'LARGE_FILE_THRESHOLD_BYTES': '10'}), \
                patch.object(jobs.large_audio, 'transcribe_large_file', return_value=('text', 5400.0)), \
                patch.object(jobs, 'generate_summary', return_value='s'), patch.object(jobs, 'generate_overview', return_value='o'), patch.object(jobs, 'extract_actions', return_value=[]):
            jobs.process_job(job)
        self.assertEqual(m['update_job_with_results'].call_args.kwargs['billable_seconds'], 5400.0)
        rpc.assert_called_once_with(TEST_USER_ID, TEST_MEETING_ID, JOB_ID, 90, 5400.0, 'openai')


class SweepTests(Env):
    def jobs(self, n=2):
        return [{'id': f'job{i}', 'user_id': TEST_USER_ID, 'meeting_id': f'm{i}', 'billable_seconds': 61.0, 'transcription_provider': 'xai'} for i in range(n)]

    def test_sweep_retries_undebited_jobs_with_their_billable_seconds(self):
        with patch.object(sc, 'list_undebited_completed_jobs', return_value=self.jobs()) as listing, \
                patch.object(sc, 'debit_minutes_for_job', return_value={'status': 'debited'}) as rpc:
            stats = minutes.sweep_undebited(now=datetime(2026, 9, 30, 12, tzinfo=timezone.utc))
        self.assertEqual(stats, {'checked': 2, 'debited': 2, 'errors': 0})
        self.assertEqual([c.args for c in rpc.call_args_list], [(TEST_USER_ID, 'm0', 'job0', 2, 61.0, 'xai'), (TEST_USER_ID, 'm1', 'job1', 2, 61.0, 'xai')])
        self.assertEqual(listing.call_args.args[0], '2026-09-27T12:00:00+00:00')   # 72 h lookback
        self.assertEqual(listing.call_args.args[1], 25)

    def test_sweep_is_throttled(self):
        with patch.object(sc, 'list_undebited_completed_jobs', return_value=[]) as listing:
            minutes.sweep_undebited(); minutes.sweep_undebited(); minutes.sweep_undebited()
            self.assertEqual(listing.call_count, 1)
            minutes.sweep_undebited(force=True)
            self.assertEqual(listing.call_count, 2)
            with patch.dict(os.environ, {'SERVER_MINUTES_DEBIT_SWEEP_INTERVAL_SECONDS': '1'}), patch.object(minutes.time, 'monotonic', return_value=minutes._last_sweep + 2):
                minutes.sweep_undebited()
            self.assertEqual(listing.call_count, 3)

    def test_sweep_never_raises(self):
        with patch.object(sc, 'list_undebited_completed_jobs', side_effect=Exception('relation does not exist')):
            self.assertEqual(minutes.sweep_undebited()['errors'], 1)
        minutes._last_sweep = 0.0
        with patch.object(sc, 'list_undebited_completed_jobs', return_value=self.jobs()), patch.object(sc, 'debit_minutes_for_job', side_effect=Exception('boom')):
            self.assertEqual(minutes.sweep_undebited(), {'checked': 2, 'debited': 0, 'errors': 2})

    def test_query_only_selects_worker_billed_unpaid_recent_completed_jobs(self):
        table = MagicMock()
        for name in ('select', 'eq', 'is_', 'gte', 'order', 'limit'):
            getattr(table, name).return_value = table
        table.not_ = MagicMock(); table.not_.is_.return_value = table
        table.execute.return_value = SimpleNamespace(data=[{'id': 'x'}])
        with patch.object(sc.supabase, 'table', return_value=table):
            self.assertEqual(sc.list_undebited_completed_jobs('2026-09-27T00:00:00', 10), [{'id': 'x'}])
        table.eq.assert_called_with('status', 'completed')
        table.is_.assert_called_with('debited_at', 'null')
        table.not_.is_.assert_called_with('billable_seconds', 'null')   # on-device rows saved by the app have NULL: never swept
        table.gte.assert_called_with('completed_at', '2026-09-27T00:00:00')

    def test_worker_loop_runs_the_sweep(self):
        import asyncio
        with patch.object(jobs.minutes, 'sweep_undebited') as sweep, patch.object(jobs.background_upload, 'promote_uploaded_jobs'), patch.object(jobs, 'get_pending_jobs', return_value=[]):
            asyncio.run(jobs.process_pending_jobs())
        sweep.assert_called_once()


class ClientTests(unittest.TestCase):
    def test_rpc_parameters_match_the_migration(self):
        rpc = MagicMock()
        rpc.return_value.execute.return_value = SimpleNamespace(data={'status': 'debited'})
        with patch.object(sc.supabase, 'rpc', rpc):
            self.assertEqual(sc.debit_minutes_for_job('u', 'm', 'j', 3, 125.0, 'xai'), {'status': 'debited'})
        rpc.assert_called_once_with('debit_minutes_for_job', {'p_user_id': 'u', 'p_meeting_id': 'm', 'p_job_id': 'j', 'p_minutes': 3, 'p_seconds': 125.0, 'p_provider': 'xai'})

    def test_balance_wrapper(self):
        rpc = MagicMock()
        with patch.object(sc.supabase, 'rpc', rpc):
            for data, expected in ((42, 42), (None, None), (True, None), ('7', None), ({}, None)):
                rpc.return_value.execute.return_value = SimpleNamespace(data=data)
                self.assertEqual(sc.get_minutes_balance('u'), expected)
        rpc.assert_called_with('get_minutes_balance_for_user', {'p_user_id': 'u'})

    def test_results_update_includes_billable_seconds_only_when_given(self):
        table = MagicMock()
        table.update.return_value = table; table.eq.return_value = table
        table.execute.return_value = SimpleNamespace(data=[{'id': 'j'}])
        with patch.object(sc.supabase, 'table', return_value=table):
            sc.update_job_with_results('j', 't', 'o', 's', [], 1.0)
            self.assertNotIn('billable_seconds', table.update.call_args.args[0])
            sc.update_job_with_results('j', 't', 'o', 's', [], 1.0, billable_seconds=61.0)
            self.assertEqual(table.update.call_args.args[0]['billable_seconds'], 61.0)

    def test_missing_column_does_not_lose_the_finished_job(self):
        table = MagicMock()
        table.update.return_value = table; table.eq.return_value = table
        calls = []

        def execute():
            calls.append(dict(table.update.call_args.args[0]))
            if 'billable_seconds' in calls[-1]:
                raise Exception('column "billable_seconds" of relation "transcription_jobs" does not exist')
            return SimpleNamespace(data=[{'id': 'j'}])
        table.execute.side_effect = execute
        with patch.object(sc.supabase, 'table', return_value=table):
            self.assertEqual(sc.update_job_with_results('j', 't', 'o', 's', [], 1.0, billable_seconds=61.0), {'id': 'j'})
        self.assertEqual(len(calls), 2)
        self.assertNotIn('billable_seconds', calls[1])
        with patch.object(sc.supabase, 'table', return_value=table), self.assertRaises(Exception):
            table.execute.side_effect = Exception('network down')
            sc.update_job_with_results('j', 't', 'o', 's', [], 1.0, billable_seconds=61.0)


if __name__ == '__main__':
    unittest.main()
