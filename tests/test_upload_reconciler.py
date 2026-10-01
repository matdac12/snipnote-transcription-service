"""Exact bytes, fair independent scans, and expiration without data deletion."""
import unittest
from datetime import datetime,timezone,timedelta
from upload_reconciler import UploadReconciler

class Repo:
    def __init__(self):self.verified=[];self.promoted=[];self.expired=[];self.checked=[]
    def scan(self,limit):return [{'id':'s','status':'awaiting_upload','upload_deadline':datetime.now(timezone.utc)+timedelta(hours=24)}][:limit]
    def list_files(self,sid):return [dict(index=0,path='path',expected_bytes=50,duration=10,verified_at=None)]
    def mark_verified(self,sid,index):self.verified.append(index)
    def promote(self,sid):self.promoted.append(sid)
    def expire(self,sid):self.expired.append(sid)
    def checked_at(self,sid):self.checked.append(sid)
class Storage:
    size=None
    def size_of(self,path):return self.size
class UploadReconcilerTests(unittest.TestCase):
    def setUp(self):self.repo=Repo();self.storage=Storage();self.r=UploadReconciler(self.repo,self.storage)
    def test_missing_or_wrong_size_never_queues(self):
        for size in [None,49,51]:
            self.storage.size=size;self.r.reconcile_uploads()
        self.assertFalse(self.repo.promoted);self.assertFalse(self.repo.verified)
    def test_busy_transcriber_does_not_delay_upload_checks(self):
        self.storage.size=50;report=self.r.reconcile_uploads()
        self.assertEqual(report.queued,1);self.assertEqual(self.repo.verified,[0]);self.assertEqual(self.repo.promoted,['s'])
    def test_expiration_does_not_delete_referenced_audio(self):
        self.repo.scan=lambda _: [dict(id='s',status='awaiting_upload',upload_deadline=datetime.now(timezone.utc)-timedelta(seconds=1))]
        self.r.reconcile_uploads();self.assertEqual(self.repo.expired,['s']);self.assertFalse(self.repo.verified);self.assertFalse(self.repo.promoted)
    def test_failed_session_does_not_starve_later_scan(self):
        def failure(path):raise ValueError('storage offline')
        self.storage.size_of=failure;report=self.r.reconcile_uploads()
        self.assertEqual(report.errors,1);self.assertEqual(self.repo.checked,['s'])
