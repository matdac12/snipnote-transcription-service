"""Apply ONLY upload migrations to an empty disposable LOCAL database and compare legacy DDL.
Usage: UPLOAD_TEST_DATABASE_URL=... python deploy/rehearse_uploads.py /path/to/app
Database must already exist, be empty, and be on loopback. No cloud connection allowed.
"""
import os
from pathlib import Path
import sys
from urllib.parse import urlparse
import psycopg

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
    migrations=[app/'supabase/migrations/20261001064743_background_upload_sessions.sql',app/'supabase/migrations/20261001065143_promote_background_upload.sql']
    with psycopg.connect(dsn,autocommit=True) as c:
        if c.execute("select count(*) from information_schema.tables where table_schema='public'").fetchone()[0]:
            raise SystemExit('Refusing to rehearse in a nonempty database')
        c.execute(fixture.read_text())
        before=schema(c)
        for migration in migrations:c.execute(migration.read_text());print('Applied locally:',migration.name)
        if schema(c)!=before:raise SystemExit('Legacy column contract changed')
        print('Legacy column contract unchanged; four legacy tables before, six public tables after')
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
if __name__=='__main__':main()
