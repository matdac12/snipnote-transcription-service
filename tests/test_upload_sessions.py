"""Catches duplicate sessions, gate bypasses, manifest drift and credential leaks."""
import unittest
from datetime import datetime, timezone, timedelta
from uuid import uuid4
from fastapi import HTTPException
from upload_models import UploadBootstrapRequest
from upload_sessions import UploadSessions

OWNER = str(uuid4())
MEETING = str(uuid4())

class Repository:
    def __init__(self):
        self.sessions = {}
        self.files = {}
        self.owner = OWNER
    def owns_meeting(self, user, meeting):
        return user == self.owner and meeting == MEETING
    def find(self, user, meeting):
        return next((s for s in self.sessions.values() if s['user_id']==user and s['meeting_id']==meeting), None)
    def get(self, sid): return self.sessions.get(sid)
    def create(self, session, files):
        existing = self.find(session['user_id'],session['meeting_id'])
        if existing: return existing
        self.sessions[session['id']] = session
        self.files[session['id']] = files
        return session
    def list_files(self, sid): return self.files[sid]
    def renew(self, sid, deadline):
        self.sessions[sid].update(status='awaiting_upload',upload_deadline=deadline)
        return self.sessions[sid]

class Storage:
    fail = False
    def sign(self, path, content_type, boundary):
        if self.fail: raise RuntimeError('sensitive credential')
        return {'upload_url':'https://storage.invalid/signed','method':'PUT','headers':{'Content-Type':f'multipart/form-data; boundary={boundary}'},'expires_at':datetime.now(timezone.utc)+timedelta(hours=2)}

class UploadSessionTests(unittest.TestCase):
    def setUp(self):
        self.repo=Repository(); self.storage=Storage()
        self.enabled=True; self.allowed={OWNER}
        self.service=UploadSessions(self.repo,self.storage,lambda u:self.enabled and u in self.allowed)
        self.request=UploadBootstrapRequest(meeting_id=MEETING, transcription_provider='xai',language='it',duration=10,files=[dict(index=0,expected_bytes=50,duration=10,extension='m4a',content_type='audio/mp4')])
    def bootstrap(self, user=OWNER): return self.service.bootstrap_upload(user,self.request)
    def test_repeated_bootstrap_returns_same_session(self):
        a=self.bootstrap();b=self.bootstrap()
        self.assertEqual(a.session_id,b.session_id);self.assertIsNone(a.job_id)
        self.assertEqual(len(self.repo.sessions),1)
        self.assertAlmostEqual((a.upload_deadline-datetime.now(timezone.utc)).total_seconds(),86400,delta=2)
        s=self.repo.get(str(a.session_id));self.assertEqual((s['transcription_provider'],s['language']),('xai','it'))
    def test_conflicting_manifest_returns_409(self):
        self.bootstrap();self.request.language='en'
        with self.assertRaises(HTTPException) as e:self.bootstrap()
        self.assertEqual(e.exception.status_code,409)
    def test_unverified_or_other_owner_rejected(self):
        with self.assertRaises(HTTPException):self.bootstrap(str(uuid4()))
        self.assertFalse(self.repo.sessions)
        a=self.bootstrap()
        with self.assertRaises(HTTPException):self.service.get_upload_session(str(uuid4()),str(a.session_id))
    def test_disabled_bootstrap_does_not_affect_legacy_jobs(self):
        self.enabled=False
        with self.assertRaises(HTTPException) as e:self.bootstrap()
        self.assertEqual(e.exception.detail['code'],'background_upload_disabled'); self.assertFalse(self.repo.sessions)
    def test_only_owner_can_start_background_session(self):
        self.allowed=set()
        with self.assertRaises(HTTPException):self.bootstrap()
        self.assertFalse(self.repo.sessions)
    def test_flag_change_after_capability_check_rejects_new_session(self):
        self.assertTrue(self.service.enabled(OWNER));self.enabled=False
        with self.assertRaises(HTTPException):self.bootstrap()
    def test_flag_off_allows_existing_session_refresh(self):
        a=self.bootstrap();self.enabled=False
        self.assertEqual(self.bootstrap().session_id,a.session_id)
    def test_expired_refresh_preserves_verified_files(self):
        a=self.bootstrap();sid=str(a.session_id)
        self.repo.sessions[sid]['status']='expired';self.repo.sessions[sid]['upload_deadline']=datetime.now(timezone.utc)-timedelta(seconds=1)
        self.repo.files[sid][0]['verified_at']=datetime.now(timezone.utc)
        b=self.bootstrap();self.assertTrue(b.files[0].verified);self.assertIsNone(b.files[0].upload_url)
        self.assertGreater(b.upload_deadline,datetime.now(timezone.utc))
        self.assertIsNone(self.service.get_upload_session(OWNER,sid).files[0].upload_url)
    def test_signing_failure_leaves_retryable_session(self):
        self.storage.fail=True
        with self.assertRaises(HTTPException) as e:self.bootstrap()
        self.assertEqual(e.exception.status_code,503);self.assertNotIn('sensitive',str(e.exception.detail))
        self.assertEqual(len(self.repo.sessions),1);self.storage.fail=False;self.bootstrap()
    def test_client_paths_nonfinite_and_bad_files_rejected(self):
        for changes in [dict(duration=float('nan')),dict(files=[]),dict(files=[dict(index=1,expected_bytes=50,duration=10,extension='m4a',content_type='audio/mp4')]),dict(files=[dict(index=0,expected_bytes=50,duration=10,extension='exe',content_type='audio/mp4')]),dict(path='foreign')]:
            with self.assertRaises(ValueError):UploadBootstrapRequest(**(self.request.model_dump()|changes))
