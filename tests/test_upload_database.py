"""Real PostgreSQL gates: ownership privileges, concurrency, rollback and legacy conflicts."""
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
import psycopg

DSN=os.getenv('UPLOAD_TEST_DATABASE_URL')
@unittest.skipUnless(DSN,'Set UPLOAD_TEST_DATABASE_URL to a disposable rehearsed database')
class UploadDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.user,self.meeting,self.sid,self.job=[uuid4() for _ in range(4)]
        with psycopg.connect(DSN) as c:
            c.execute('insert into auth.users values (%s)',(self.user,))
            c.execute("insert into meetings(id,user_id,name) values (%s,%s,'fixture')",(self.meeting,self.user))
            c.execute("insert into background_upload_sessions(id,user_id,meeting_id,reserved_job_id,manifest_digest,transcription_provider,duration,upload_deadline) values (%s,%s,%s,%s,'digest','xai',10,now()+interval '24 hours')",(self.sid,self.user,self.meeting,self.job))
            c.execute("insert into background_upload_files(session_id,index,path,expected_bytes,duration,content_type,verified_at) values (%s,0,%s,50,10,'audio/mp4',now())",(self.sid,str(self.sid)+'.m4a'))
    def promote(self):
        with psycopg.connect(DSN) as c:
            c.execute('set local role service_role')
            return c.execute('select promote_background_upload(%s)',(self.sid,)).fetchone()[0]
    def test_two_promotions_create_one_job(self):
        with ThreadPoolExecutor(2) as ex:results=list(ex.map(lambda _:self.promote(),range(2)))
        self.assertEqual(results,[self.job,self.job])
        with psycopg.connect(DSN) as c:
            self.assertEqual(c.execute('select count(*) from transcription_jobs where meeting_id=%s',(self.meeting,)).fetchone()[0],1)
            self.assertEqual(c.execute('select file_size,total_chunks from audio_chunks where meeting_id=%s',(self.meeting,)).fetchone(),(50,1))
            self.assertEqual(c.execute('select transcription_job_id,processing_state from meetings where id=%s',(self.meeting,)).fetchone(),(self.job,'transcribing'))
    def test_metadata_failure_rolls_back_promotion(self):
        with psycopg.connect(DSN) as c:
            c.execute("insert into audio_chunks(meeting_id,user_id,chunk_index,total_chunks,file_path,file_size,duration_seconds) values (%s,%s,0,1,'legacy',50,10)",(self.meeting,self.user))
        with self.assertRaises(psycopg.errors.UniqueViolation):self.promote()
        with psycopg.connect(DSN) as c:
            self.assertEqual(c.execute('select count(*) from transcription_jobs where id=%s',(self.job,)).fetchone()[0],0)
            self.assertEqual(c.execute('select status from background_upload_sessions where id=%s',(self.sid,)).fetchone()[0],'awaiting_upload')
    def test_existing_legacy_job_is_not_overwritten(self):
        legacy=uuid4()
        with psycopg.connect(DSN) as c:
            c.execute('insert into transcription_jobs(id,user_id,meeting_id) values (%s,%s,%s)',(legacy,self.user,self.meeting))
            c.execute('update meetings set transcription_job_id=%s where id=%s',(legacy,self.meeting))
        with self.assertRaises(psycopg.errors.RaiseException):self.promote()
        with psycopg.connect(DSN) as c:self.assertEqual(c.execute('select transcription_job_id from meetings where id=%s',(self.meeting,)).fetchone()[0],legacy)
    def test_client_roles_have_no_table_or_function_access(self):
        for role in ['anon','authenticated']:
            for sql in ["select * from background_upload_sessions", "insert into background_upload_files values (null,0,'bad',1,1,'audio/mp4',null)",f"select promote_background_upload('{self.sid}')", "select register_background_upload('{}','[]')"]:
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    with psycopg.connect(DSN) as c:
                        c.execute('set local role '+role);c.execute(sql)
    def test_unverified_file_cannot_promote(self):
        with psycopg.connect(DSN) as c:c.execute('update background_upload_files set verified_at=null where session_id=%s',(self.sid,))
        with self.assertRaises(psycopg.errors.RaiseException):self.promote()

    def test_registration_metadata_failure_is_atomic(self):
        from psycopg.types.json import Jsonb
        sid,mid,job=uuid4(),uuid4(),uuid4()
        with psycopg.connect(DSN) as c:
            c.execute("insert into meetings(id,user_id,name) values (%s,%s,'registration rollback')",(mid,self.user))
        session=dict(id=str(sid),user_id=str(self.user),meeting_id=str(mid),reserved_job_id=str(job),manifest_digest='registration',transcription_provider='xai',duration=10,upload_deadline='2099-01-01T00:00:00Z')
        files=[dict(index=0,path='same-'+str(sid),expected_bytes=50,duration=5,content_type='audio/mp4'),dict(index=1,path='same-'+str(sid),expected_bytes=50,duration=5,content_type='audio/mp4')]
        with self.assertRaises(psycopg.errors.UniqueViolation):
            with psycopg.connect(DSN) as c:
                c.execute('set local role service_role')
                c.execute('select register_background_upload(%s,%s)',(Jsonb(session),Jsonb(files)))
        with psycopg.connect(DSN) as c:self.assertEqual(c.execute('select count(*) from background_upload_sessions where id=%s',(sid,)).fetchone()[0],0)
