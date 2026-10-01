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
    def test_legacy_job_contract_unchanged_when_gate_off(self):
        job=str(uuid4())
        with patch('main.API_KEY',''),patch('main.create_job',return_value={'id':job,'status':'pending','created_at':'2026-10-01T00:00:00Z'}),patch.dict('os.environ',{'BACKGROUND_UPLOAD_ENABLED':'false'}):
            response=self.client.post('/jobs',json={'user_id':OWNER,'meeting_id':MEETING,'audio_url':'https://offline.invalid/audio','transcription_provider':'xai'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json(),{'job_id':job,'status':'pending','created_at':'2026-10-01T00:00:00Z'})
