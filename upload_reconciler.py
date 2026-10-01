"""Independent object verification; never holds a DB lock during Storage I/O."""
from dataclasses import dataclass
from datetime import datetime, timezone
from upload_sessions import SupabaseUploadRepository, utc

@dataclass(frozen=True)
class VerifiedUploadFile:
    index: int
    path: str
    bytes: int
    duration: float

@dataclass
class ReconcileReport:
    checked: int = 0
    queued: int = 0
    expired: int = 0
    errors: int = 0

class UploadReconciler:
    def __init__(self,repository,storage):self.repo,self.storage=repository,storage
    def verify_upload_files(self,session_id):
        verified=[]
        for f in self.repo.list_files(session_id):
            if f.get('verified_at') or self.storage.size_of(f['path']) == f['expected_bytes']:
                if not f.get('verified_at'):self.repo.mark_verified(session_id,f['index'])
                verified.append(VerifiedUploadFile(f['index'],f['path'],f['expected_bytes'],f['duration']))
        return verified
    def reconcile_uploads(self,limit=100):
        report=ReconcileReport()
        for session in self.repo.scan(min(max(limit,1),100)):
            sid=session['id'];report.checked+=1
            try:
                if utc(session['upload_deadline'])<=datetime.now(timezone.utc):
                    self.repo.expire(sid);report.expired+=1
                    # Keep expired files for explicit retry. No storage deletes here.
                    continue
                expected=self.repo.list_files(sid)
                verified=self.verify_upload_files(sid)
                if expected and len(verified)==len(expected):self.repo.promote(sid);report.queued+=1
            except Exception:
                # Do not log exception payloads; SDK errors may include credentials.
                report.errors+=1
            finally:self.repo.checked_at(sid)
        return report

class ReconcilerRepository(SupabaseUploadRepository):
    def scan(self,limit):
        return self.client.table('background_upload_sessions').select('*').eq('status','awaiting_upload').order('last_checked_at').order('id').limit(limit).execute().data
    def mark_verified(self,sid,index):
        self.client.table('background_upload_files').update({'verified_at':datetime.now(timezone.utc).isoformat()}).eq('session_id',sid).eq('index',index).execute()
    def promote(self,sid):return self.client.rpc('promote_background_upload',{'p_session_id':sid}).execute().data
    def expire(self,sid):
        self.client.table('background_upload_sessions').update({'status':'expired'}).eq('id',sid).eq('status','awaiting_upload').lte('upload_deadline',datetime.now(timezone.utc).isoformat()).execute()
    def checked_at(self,sid):
        self.client.table('background_upload_sessions').update({'last_checked_at':datetime.now(timezone.utc).isoformat()}).eq('id',sid).execute()

class ObjectStorage:
    def __init__(self,client):self.bucket=client.storage.from_('recordings')
    def size_of(self,path):
        parent,name=path.rsplit('/',1)
        objects=self.bucket.list(parent,{'limit':100,'search':name})
        for obj in objects:
            if obj.get('name')==name:
                size=(obj.get('metadata') or {}).get('size')
                return int(size) if size is not None else None
        return None

def reconciler():
    from supabase_client import supabase
    return UploadReconciler(ReconcilerRepository(supabase),ObjectStorage(supabase))
def reconcile_uploads(limit: int = 100):return reconciler().reconcile_uploads(limit)
def verify_upload_files(session_id: str):return reconciler().verify_upload_files(session_id)
