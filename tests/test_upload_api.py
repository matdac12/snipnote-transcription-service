"""HTTP integration at Auth/DB seams, including unchanged legacy job contract."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
import support
from fastapi.testclient import TestClient
from main import app
from upload_sessions import UploadSessions
from test_upload_sessions import Repository,Storage,OWNER,MEETING

class UploadAPITests(unittest.TestCase):
    def setUp(self):
        self.repo=Repository();self.service=UploadSessions(self.repo,Storage(),lambda u:u==OWNER)
        self.client=TestClient(app)
        self.body={'meeting_id':MEETING,'transcription_provider':'xai','language':'it','duration':10,'files':[{'index':0,'expected_bytes':50,'duration':10,'extension':'m4a','content_type':'audio/mp4'}]}
        self.patches=[patch('upload_sessions.service',return_value=self.service),patch('supabase_client.supabase.auth.get_user',side_effect=self.verify)]
        for p in self.patches:p.start()
        self.addCleanup(lambda:[p.stop() for p in self.patches])
    def verify(self,token):
        if token not in ['owner','foreign']:raise ValueError('invalid')
        return SimpleNamespace(user=SimpleNamespace(id=OWNER if token=='owner' else str(uuid4()),is_anonymous=False))
    def test_auth_failure_never_registers(self):
        for headers in [{},{'Authorization':'Bearer forged'}]:
            self.assertEqual(self.client.post('/upload-sessions',json=self.body,headers=headers).status_code,401)
        self.assertFalse(self.repo.sessions)
    def test_owner_response_refresh_and_status_contract(self):
        headers={'Authorization':'Bearer owner'}
        a=self.client.post('/upload-sessions',json=self.body,headers=headers)
        self.assertEqual(a.status_code,200,a.text)
        data=a.json();self.assertNotIn('job_id',data);self.assertEqual(data['files'][0]['method'],'PUT')
        b=self.client.get('/upload-sessions/'+data['session_id'],headers=headers)
        self.assertEqual(b.status_code,200);self.assertNotIn('upload_url',b.json()['files'][0])
        denied=self.client.get('/upload-sessions/'+data['session_id'],headers={'Authorization':'Bearer foreign'})
        self.assertEqual(denied.status_code,404)
    def test_capability_gate_is_verified_and_empty_allowlist_is_closed(self):
        with patch.dict('os.environ',{'BACKGROUND_UPLOAD_ENABLED':'true','BACKGROUND_UPLOAD_ALLOWED_USERS':''}):
            self.assertFalse(self.client.get('/upload-capabilities',headers={'Authorization':'Bearer owner'}).json()['background_upload_enabled'])
        self.assertEqual(self.client.get('/upload-capabilities').status_code,401)
    def file(self, expected_bytes, index=0, duration=10):
        return {'index': index, 'expected_bytes': expected_bytes, 'duration': duration, 'extension': 'm4a', 'content_type': 'audio/mp4'}

    def speaker_body(self, expected_bytes=524288000, **changes):
        body = {'meeting_id': MEETING, 'transcription_provider': 'xai', 'language': 'it', 'duration': 10, 'speaker_labels': True, 'files': [self.file(expected_bytes)]}
        body.update(changes)
        return body

    def test_one_500_mb_xai_speaker_file_is_accepted(self):
        response = self.client.post('/upload-sessions', json=self.speaker_body(), headers={'Authorization': 'Bearer owner'})
        self.assertEqual(response.status_code, 200, response.text)
        session = next(iter(self.repo.sessions.values()))
        self.assertIs(session['speaker_labels'], True)

    def test_speaker_file_over_500_mb_is_rejected(self):
        response = self.client.post('/upload-sessions', json=self.speaker_body(524288001), headers={'Authorization': 'Bearer owner'})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertFalse(self.repo.sessions)

    def test_speaker_labels_with_openai_or_two_files_is_rejected_before_storage(self):
        headers = {'Authorization': 'Bearer owner'}
        openai = self.speaker_body(50, transcription_provider='openai')
        two = self.speaker_body()
        two['duration'] = 20
        two['files'] = [self.file(50, 0, 10), self.file(50, 1, 10)]
        for body in (openai, two):
            self.repo.sessions.clear()
            response = self.client.post('/upload-sessions', json=body, headers=headers)
            self.assertEqual(response.status_code, 400, response.text)
            self.assertFalse(self.repo.sessions)

    def test_plain_file_over_15_mb_is_rejected(self):
        body = self.speaker_body(15728641, speaker_labels=False)
        response = self.client.post('/upload-sessions', json=body, headers={'Authorization': 'Bearer owner'})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertFalse(self.repo.sessions)

    def test_omitted_speaker_labels_behaves_as_false(self):
        headers = {'Authorization': 'Bearer owner'}
        plain = dict(self.body)
        self.assertNotIn('speaker_labels', plain)
        accepted = self.client.post('/upload-sessions', json=plain, headers=headers)
        self.assertEqual(accepted.status_code, 200, accepted.text)
        session = next(iter(self.repo.sessions.values()))
        self.assertIs(session['speaker_labels'], False)
        self.repo.sessions.clear()
        oversized = dict(plain)
        oversized['files'] = [self.file(15728641)]
        rejected = self.client.post('/upload-sessions', json=oversized, headers=headers)
        self.assertEqual(rejected.status_code, 422, rejected.text)
        self.assertFalse(self.repo.sessions)

    def test_legacy_job_contract_unchanged_when_gate_off(self):
        job=str(uuid4())
        with patch('main.API_KEY',''),patch('main.create_job',return_value={'id':job,'status':'pending','created_at':'2026-10-01T00:00:00Z'}),patch.dict('os.environ',{'BACKGROUND_UPLOAD_ENABLED':'false'}):
            response=self.client.post('/jobs',json={'user_id':OWNER,'meeting_id':MEETING,'audio_url':'https://offline.invalid/audio','transcription_provider':'xai'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json(),{'job_id':job,'status':'pending','created_at':'2026-10-01T00:00:00Z'})
