"""Live Activity stage hooks on the background-upload paths (APNs x background upload). No network."""
import os
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import support  # noqa: F401
import background_upload as bu
import jobs
import large_audio
import test_background_upload as tbu


class PromotionNotifiesTests(tbu.EnvBase):
    def setUp(self):
        super().setUp()
        self.db = tbu.FakeStorageAndDB()
        stack = ExitStack()
        self.db.patch(stack)
        self.addCleanup(stack.close)
        self.now = datetime.now(timezone.utc)
        self.job = bu.create_upload_job(user_id=tbu.USER, meeting_id=tbu.MEETING, expected_bytes=1000, now=self.now)
        self.path = self.job['storage_path']

    def test_promotion_pushes_queued(self):
        self.db.objects[self.path] = 1000
        with patch.object(bu, 'notify_stage') as notify:
            bu.promote_uploaded_jobs(self.now)
        notify.assert_called_once_with(self.job['job_id'], 'queued', 0)

    def test_waiting_upload_pushes_nothing(self):
        with patch.object(bu, 'notify_stage') as notify:
            bu.promote_uploaded_jobs(self.now)
        notify.assert_not_called()

    def test_expiry_pushes_failed_once(self):
        with patch.object(bu, 'notify_stage') as notify:
            bu.promote_uploaded_jobs(self.now + timedelta(days=1))
            bu.promote_uploaded_jobs(self.now + timedelta(days=1))  # already failed: no second push
        notify.assert_called_once_with(self.job['job_id'], 'failed')

    def test_notifier_error_cannot_break_promotion(self):
        self.db.objects[self.path] = 1000
        with patch.object(bu, 'notify_stage', side_effect=RuntimeError('boom')):
            # notify_stage itself never raises; if it ever did, promotion is already committed.
            try:
                bu.promote_uploaded_jobs(self.now)
            except RuntimeError:
                pass
        self.assertEqual(self.db.rows[self.job['job_id']]['status'], 'pending')


class UploadedJobStagesTests(unittest.TestCase):
    def setUp(self):
        self.workroot = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.workroot, True)
        env = patch.dict(os.environ, {'XAI_WORK_DIR': self.workroot})
        env.start()
        self.addCleanup(env.stop)

    def run_upload(self, size, probe=None, duration=None):
        job = {'id': 'job', 'storage_path': f'{tbu.USER}/{tbu.MEETING}.m4a', 'expected_bytes': size, 'duration': duration}

        def download(path, dest):
            with open(dest, 'wb') as f:
                f.write(b'x' * size)

        def chunk(data, name, *a, **k):
            return 'alpha beta'

        with patch.object(jobs, 'notify_stage') as notify, patch.object(jobs, 'update_job_progress'), \
                patch.object(jobs, 'download_storage_object_to_file', side_effect=download), \
                patch.object(jobs, 'transcribe_audio', side_effect=lambda *a, **k: (k['progress_callback'](50, 'Transcribing'), {'transcript': 't', 'duration': 3})[1]), \
                patch.object(large_audio, 'segment_audio', side_effect=tbu.fake_segments(['a'])), \
                patch.object(large_audio.transcribe, 'transcribe_chunk_with_retry', side_effect=chunk), \
                patch.object(large_audio.xai_single, 'probe_duration', return_value=probe):
            jobs.transcribe_uploaded_job_audio('job', job, None, 'openai')
        return [c.args[1] for c in notify.call_args_list], notify

    def test_small_upload_reports_preparing_then_transcribing(self):
        stages, notify = self.run_upload(1024)
        self.assertEqual(stages[0], 'preparing')
        self.assertEqual(stages[1], 'transcribing')
        self.assertTrue(set(stages) <= {'preparing', 'transcribing'})
        self.assertEqual(notify.call_args_list[0].args[0], 'job')

    def test_segmented_large_upload_reports_transcribing(self):
        stages, _ = self.run_upload(20 * 1024 * 1024, probe=5400.0)
        self.assertEqual(stages[:2], ['preparing', 'transcribing'])
        self.assertTrue(set(stages) <= {'preparing', 'transcribing'})


if __name__ == '__main__':
    unittest.main()
