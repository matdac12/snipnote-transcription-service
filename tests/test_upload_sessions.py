"""Catches duplicate sessions, gate bypasses, manifest drift and credential leaks."""
import hashlib
import json
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
    def mark_verified(self, sid, index):
        self.files[sid][index]["verified_at"]=datetime.now(timezone.utc)
    def renew(self, sid, deadline):
        self.sessions[sid].update(status='awaiting_upload',upload_deadline=deadline)
        return self.sessions[sid]
    def get_job(self, job_id):
        return getattr(self, 'jobs', {}).get(job_id)
    def job_exists(self, user, meeting):
        return any(job['user_id'] == user and job['meeting_id'] == meeting for job in getattr(self, 'jobs', {}).values())
    def requeue_failed_job(self, job_id):
        job = self.jobs[job_id]
        job.update(status='pending', retry_count=0, error_message=None)

class Storage:
    fail = False
    size = None
    def size_of(self,path): return self.size
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

    def test_failed_speaker_job_can_be_requeued_and_a_live_job_cannot(self):
        failed = str(uuid4())
        live = str(uuid4())
        self.repo.jobs = {
            failed: dict(id=failed, user_id=OWNER, meeting_id=MEETING, status='failed', retry_count=5, error_message='no speaker'),
            live: dict(id=live, user_id=OWNER, meeting_id=MEETING, status='processing', retry_count=0, error_message=None),
        }
        self.assertEqual(self.service.retry_failed_upload(OWNER, failed)['status'], 'pending')
        self.assertEqual(self.repo.jobs[failed]['status'], 'pending')
        self.assertEqual(self.repo.jobs[failed]['retry_count'], 0)
        self.assertIsNone(self.repo.jobs[failed]['error_message'])
        self.assertEqual(self.service.retry_failed_upload(OWNER, live)['status'], 'processing')
        self.assertEqual(self.repo.jobs[live]['status'], 'processing')
        self.assertEqual(self.repo.jobs[live]['retry_count'], 0)
        with self.assertRaises(HTTPException) as missing:
            self.service.retry_failed_upload(str(uuid4()), failed)
        self.assertEqual(missing.exception.status_code, 404)
        pending = str(uuid4())
        self.repo.jobs[pending] = dict(id=pending, user_id=OWNER, meeting_id=MEETING, status='pending', retry_count=2, error_message='keep')
        self.assertEqual(self.service.retry_failed_upload(OWNER, pending)['status'], 'pending')
        self.assertEqual(self.repo.jobs[pending]['retry_count'], 2)
        self.assertEqual(self.repo.jobs[pending]['error_message'], 'keep')

    def test_new_session_rejects_a_meeting_that_already_has_a_job(self):
        self.repo.jobs = {str(uuid4()): dict(user_id=OWNER, meeting_id=MEETING, status='failed')}
        with self.assertRaises(HTTPException) as error:
            self.bootstrap()
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(error.exception.detail['code'], 'existing_job_conflict')
        self.assertFalse(self.repo.sessions)

    def test_pre_update_session_refreshes_and_still_rejects_a_real_change(self):
        payload = self.request.model_dump(mode='json')
        payload.pop('speaker_labels', None)
        old = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        sid = str(uuid4())
        self.repo.sessions[sid] = dict(id=sid, user_id=OWNER, meeting_id=MEETING, reserved_job_id=str(uuid4()), manifest_digest=old, transcription_provider='xai', language='it', duration=10, speaker_labels=False, status='awaiting_upload', upload_deadline=(datetime.now(timezone.utc) + timedelta(hours=24)).isoformat())
        self.repo.files[sid] = [dict(session_id=sid, index=0, path='p', expected_bytes=50, duration=10, content_type='audio/mp4', verified_at=None)]
        self.assertEqual(str(self.bootstrap().session_id), sid)
        self.request.language = 'en'
        with self.assertRaises(HTTPException) as changed:
            self.bootstrap()
        self.assertEqual(changed.exception.status_code, 409)
        self.request.language = 'it'
        self.request.speaker_labels = True
        with self.assertRaises(HTTPException) as speakers:
            self.bootstrap()
        self.assertEqual(speakers.exception.status_code, 409)

    def test_register_payload_includes_speaker_labels(self):
        self.request = UploadBootstrapRequest(meeting_id=MEETING, transcription_provider='xai', language='it', duration=10, speaker_labels=True, files=[dict(index=0, expected_bytes=524288000, duration=10, extension='m4a', content_type='audio/mp4')])
        created = self.bootstrap()
        session = self.repo.get(str(created.session_id))
        self.assertIs(session['speaker_labels'], True)
        self.assertEqual(self.repo.files[session['id']][0]['expected_bytes'], 524288000)

    def test_refresh_checks_existing_object_before_resigning(self):
        a=self.bootstrap();self.storage.size=50
        b=self.bootstrap()
        self.assertTrue(b.files[0].verified);self.assertIsNone(b.files[0].upload_url)
