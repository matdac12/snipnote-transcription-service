"""Idempotent sessions with injected repository/storage boundaries."""
import hashlib
import json
import os
from datetime import datetime, timezone, timedelta
from uuid import uuid4, UUID
from fastapi import HTTPException
from upload_models import UploadSessionResponse, UploadFileResponse

def background_upload_enabled(user_id):
    allowed = {x.strip() for x in os.getenv('BACKGROUND_UPLOAD_ALLOWED_USERS','').split(',') if x.strip()}
    return os.getenv('BACKGROUND_UPLOAD_ENABLED','false').lower() == 'true' and user_id in allowed

def utc(value):
    return value if isinstance(value,datetime) else datetime.fromisoformat(value.replace('Z','+00:00'))

class UploadSessions:
    def __init__(self, repository, storage, enabled=background_upload_enabled):
        self.repo, self.storage, self.enabled = repository, storage, enabled
    def bootstrap_upload(self, user_id, request):
        user_id=str(UUID(user_id)); meeting_id=str(request.meeting_id)
        # Verify meeting existence and ownership even for refresh, before signing.
        if not self.repo.owns_meeting(user_id,meeting_id): raise HTTPException(404,detail={'code':'meeting_not_found'})
        digest=hashlib.sha256(json.dumps(request.model_dump(mode='json'),sort_keys=True,separators=(',',':')).encode()).hexdigest()
        session=self.repo.find(user_id,meeting_id)
        if session is None:
            if not self.enabled(user_id): raise HTTPException(403,detail={'code':'background_upload_disabled'})
            sid=str(uuid4()); now=datetime.now(timezone.utc)
            session=dict(id=sid,user_id=user_id,meeting_id=meeting_id,reserved_job_id=str(uuid4()),manifest_digest=digest,transcription_provider=request.transcription_provider,language=request.language,duration=request.duration,status='awaiting_upload',upload_deadline=(now+timedelta(hours=24)).isoformat())
            files=[dict(session_id=sid,index=f.index,path=f'{user_id}/{meeting_id}/background/{sid}/{f.index}.{f.extension}',expected_bytes=f.expected_bytes,duration=f.duration,content_type=f.content_type,verified_at=None) for f in request.files]
            session=self.repo.create(session,files) # DB transaction handles racing callers.
        if session['manifest_digest'] != digest: raise HTTPException(409,detail={'code':'manifest_conflict'})
        if session['status']=='cancelled': raise HTTPException(409,detail={'code':'session_cancelled'})
        if session['status']=='expired' or (session['status']=='awaiting_upload' and utc(session['upload_deadline'])<=datetime.now(timezone.utc)):
            session=self.repo.renew(session['id'],(datetime.now(timezone.utc)+timedelta(hours=24)).isoformat())
        return self.response(session,sign=True)
    def get_upload_session(self,user_id,session_id):
        session=self.repo.get(str(UUID(session_id)))
        if not session or session['user_id'] != user_id or not self.repo.owns_meeting(user_id,session['meeting_id']): raise HTTPException(404,detail={'code':'session_not_found'})
        return self.response(session,sign=False)
    def response(self,session,sign):
        state=session['status']
        if state=='awaiting_upload' and utc(session['upload_deadline'])<=datetime.now(timezone.utc):state='expired'
        files=[]
        for f in self.repo.list_files(session['id']):
            item=UploadFileResponse(index=f['index'],verified=f.get('verified_at') is not None)
            if sign and state=='awaiting_upload' and not item.verified:
                try:
                    instructions=self.storage.sign(f['path'],f['content_type'],f"snipnote-{session['id']}-{f['index']}")
                    item=UploadFileResponse(index=f['index'],verified=False,**instructions)
                except Exception:
                    raise HTTPException(503,detail={'code':'upload_signing_unavailable'}) from None
            files.append(item)
        return UploadSessionResponse(session_id=session['id'],status=state,job_id=session['reserved_job_id'] if state=='queued' else None,upload_deadline=session['upload_deadline'],files=files)

class SupabaseUploadRepository:
    def __init__(self,client):self.client=client
    def owns_meeting(self,user,meeting):
        return bool(self.client.table('meetings').select('id').eq('id',meeting).eq('user_id',user).execute().data)
    def find(self,user,meeting):
        data=self.client.table('background_upload_sessions').select('*').eq('user_id',user).eq('meeting_id',meeting).execute().data
        return data[0] if data else None
    def get(self,sid):
        data=self.client.table('background_upload_sessions').select('*').eq('id',sid).execute().data
        return data[0] if data else None
    def create(self,session,files):
        self.client.rpc('register_background_upload',{'p_session':session,'p_files':files}).execute()
        return self.find(session['user_id'],session['meeting_id'])
    def list_files(self,sid):
        return self.client.table('background_upload_files').select('*').eq('session_id',sid).order('index').execute().data
    def renew(self,sid,deadline):
        # Status predicate prevents refresh racing promotion from regressing queued.
        self.client.table('background_upload_sessions').update({'status':'awaiting_upload','upload_deadline':deadline,'updated_at':datetime.now(timezone.utc).isoformat()}).eq('id',sid).in_('status',['expired','awaiting_upload']).execute()
        return self.get(sid)

class SupabaseUploadStorage:
    def __init__(self,client):self.bucket=client.storage.from_('recordings')
    def sign(self,path,content_type,boundary):
        signed=self.bucket.create_signed_upload_url(path)
        return {'upload_url':signed['signed_url'],'method':'PUT','headers':{'Content-Type':f'multipart/form-data; boundary={boundary}'},'expires_at':datetime.now(timezone.utc)+timedelta(hours=2)}

def service():
    from supabase_client import supabase
    return UploadSessions(SupabaseUploadRepository(supabase),SupabaseUploadStorage(supabase))
def bootstrap_upload(user_id,request):return service().bootstrap_upload(user_id,request)
def get_upload_session(user_id,session_id):return service().get_upload_session(user_id,session_id)
