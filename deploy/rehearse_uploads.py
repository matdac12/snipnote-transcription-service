"""Apply ONLY upload migrations to an empty disposable LOCAL database and compare legacy DDL.
Usage: UPLOAD_TEST_DATABASE_URL=... python deploy/rehearse_uploads.py /path/to/app
Database must already exist, be empty, and be on loopback. No cloud connection allowed.
"""
import os
from pathlib import Path
import sys
from urllib.parse import urlparse
from uuid import uuid4
import psycopg
from psycopg.types.json import Jsonb

LEGACY=('meetings','audio_chunks','recordings','transcription_jobs')
def schema(c):
    return c.execute("select table_name,column_name,data_type,is_nullable,column_default from information_schema.columns where table_schema='public' and table_name=any(%s) order by table_name,ordinal_position",(list(LEGACY),)).fetchall()

def main():
    dsn=os.environ['UPLOAD_TEST_DATABASE_URL']
    parsed=urlparse(dsn)
    if parsed.hostname not in ('localhost','127.0.0.1','::1') or parsed.path in ('','/postgres','/template0','/template1'):
        raise SystemExit('Rehearsal requires a named disposable database on loopback')
    app=Path(sys.argv[1]).resolve()
    fixture=Path(__file__).resolve().parent.parent/'tests/upload_legacy_fixture.sql'
    migrations=[
        app/'supabase/migrations/20261001064743_background_upload_sessions.sql',
        app/'supabase/migrations/20261001065143_promote_background_upload.sql',
        app/'supabase/migrations/20261113140000_xai_speaker_labels.sql',
    ]
    with psycopg.connect(dsn,autocommit=True) as c:
        if c.execute("select count(*) from information_schema.tables where table_schema='public'").fetchone()[0]:
            raise SystemExit('Refusing to rehearse in a nonempty database')
        c.execute(fixture.read_text())
        before=schema(c)
        for migration in migrations:c.execute(migration.read_text());print('Applied locally:',migration.name)
        after=schema(c)
        added=[row for row in after if (row[0],row[1]) not in {(t,col) for t,col,_,_,_ in before}]
        removed=[row for row in before if (row[0],row[1]) not in {(t,col) for t,col,_,_,_ in after}]
        if removed or [row[1] for row in added] != ['speaker_labels'] or any(row[0] != 'transcription_jobs' or row[2] != 'boolean' or row[3] != 'NO' for row in added):
            raise SystemExit(f'Unexpected legacy column change: added={added} removed={removed}')
        print('Legacy columns unchanged except transcription_jobs.speaker_labels; six public tables after')
        prove_speaker_rules(c)
        for table in ['background_upload_sessions','background_upload_files']:
            rls=c.execute('select relrowsecurity from pg_class where oid=%s::regclass',('public.'+table,)).fetchone()[0]
            if not rls:raise SystemExit('Missing RLS')
        for role in ['anon','authenticated']:
            for table in ['background_upload_sessions','background_upload_files']:
                privileges=c.execute("select has_table_privilege(%s,%s,'SELECT,INSERT,UPDATE,DELETE')",(role,'public.'+table)).fetchone()[0]
                if privileges:raise SystemExit('Unexpected client table access')
            for function in ['register_background_upload(jsonb,jsonb)','promote_background_upload(uuid)']:
                if c.execute('select has_function_privilege(%s,%s,\'EXECUTE\')',(role,'public.'+function)).fetchone()[0]:raise SystemExit('Unexpected client RPC access')
        print('Both new tables RLS enabled; anon/authenticated table/RPC access denied')

def prove_speaker_rules(c):
    """500 MB one-file xAI speaker session promotes as one chunk. OpenAI 16 MB and two speaker files do not."""
    user=str(uuid4())
    c.execute('insert into auth.users values (%s)',(user,))
    def session(provider,speaker,meeting,sid,job,duration):
        return dict(id=sid,user_id=user,meeting_id=meeting,reserved_job_id=job,manifest_digest='speaker',transcription_provider=provider,language='it',duration=duration,speaker_labels=speaker,upload_deadline='2099-01-01T00:00:00Z')
    def one(meeting,sid,size,duration):
        return [dict(index=0,path=f'{user}/{meeting}/background/{sid}/0.m4a',expected_bytes=size,duration=duration,content_type='audio/mp4')]
    meeting,sid,job=[str(uuid4()) for _ in range(3)]
    c.execute("insert into meetings(id,user_id,name) values (%s,%s,'speaker')",(meeting,user))
    c.execute('set role service_role')
    c.execute('select register_background_upload(%s,%s)',(Jsonb(session('xai',True,meeting,sid,job,600)),Jsonb(one(meeting,sid,524288000,600))))
    c.execute('reset role')
    c.execute('update background_upload_files set verified_at=now() where session_id=%s',(sid,))
    c.execute('set role service_role')
    c.execute('select promote_background_upload(%s)',(sid,))
    c.execute('reset role')
    stored=c.execute('select speaker_labels,is_chunked,total_chunks from transcription_jobs where id=%s',(job,)).fetchone()
    chunk=c.execute('select file_size,total_chunks from audio_chunks where meeting_id=%s',(meeting,)).fetchone()
    if stored!=(True,True,1) or chunk!=(524288000,1):
        raise SystemExit(f'Speaker promotion mismatch: job={stored} chunk={chunk}')
    print('500 MB xAI speaker session registered and promoted with speaker_labels and total_chunks 1')
    legacy_meeting, sid2, job2 = str(uuid4()), str(uuid4()), str(uuid4())
    c.execute("insert into meetings(id,user_id,name) values (%s,%s,'legacy')", (legacy_meeting, user))
    c.execute("insert into transcription_jobs(id,user_id,meeting_id,status) values (%s,%s,%s,'failed')", (job2, user, legacy_meeting))
    c.execute('set role service_role')
    try:
        c.execute('select register_background_upload(%s,%s)', (Jsonb(session('xai', True, legacy_meeting, sid2, str(uuid4()), 600)), Jsonb(one(legacy_meeting, sid2, 50, 600))))
    except psycopg.errors.RaiseException as error:
        c.execute('reset role')
        if 'existing_job_conflict' not in str(error) or c.execute('select count(*) from background_upload_sessions where id=%s', (sid2,)).fetchone()[0]:
            raise SystemExit('existing job stored a second session') from error
        print('Rejected: second session after a job exists')
    else:
        raise SystemExit('second session after a job exists was accepted')
    for label,provider,speaker,files in [
        ('openai 16 MB', 'openai', False, None),
        ('two-file speaker', 'xai', True, 'two'),
    ]:
        meeting,sid,job=[str(uuid4()) for _ in range(3)]
        c.execute("insert into meetings(id,user_id,name) values (%s,%s,%s)",(meeting,user,label))
        if files=='two':
            payload=[dict(index=0,path=f'{sid}/0.m4a',expected_bytes=50,duration=5,content_type='audio/mp4'),dict(index=1,path=f'{sid}/1.m4a',expected_bytes=50,duration=5,content_type='audio/mp4')]
            duration=10
        else:
            payload=one(meeting,sid,16777216,10)
            duration=10
        c.execute('set role service_role')
        try:
            c.execute('select register_background_upload(%s,%s)',(Jsonb(session(provider,speaker,meeting,sid,job,duration)),Jsonb(payload)))
        except psycopg.errors.RaiseException as error:
            c.execute('reset role')
            if c.execute('select count(*) from background_upload_sessions where id=%s',(sid,)).fetchone()[0]:
                raise SystemExit(f'{label} stored a session') from error
            print('Rejected:',label)
            continue
        raise SystemExit(f'{label} was accepted')

if __name__=='__main__':main()
