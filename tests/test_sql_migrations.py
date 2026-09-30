"""Execute the SQL migrations on a real PostgreSQL (optional).

Needs `pip install pgserver psycopg2-binary` (an embedded PostgreSQL 16); skipped otherwise. The Supabase
platform is mocked (tests/sql/supabase_mock.sql) and so is the minutes ledger, because the real ledger SQL is
not in any repo: these tests prove the migrations' own logic, NOT that they fit the production schema.
"""
import os
import unittest
import uuid

try:
    import pgserver
    import psycopg2
except ImportError:  # pragma: no cover
    pgserver = None

HERE = os.path.dirname(os.path.abspath(__file__))
MIGRATIONS = os.path.join(HERE, '..', 'migrations')
USER = '8a1c2f3e-4b5d-4e6f-8a7b-9c0d1e2f3a4b'
OTHER = '5d4c3b2a-1f0e-4d9c-8b7a-6f5e4d3c2b1a'

_server = None


def server():
    global _server
    if _server is None:
        import tempfile
        import warnings
        warnings.filterwarnings('ignore')
        _server = pgserver.get_server(tempfile.mkdtemp(prefix='snipnote-pg-'), cleanup_mode='delete')
    return _server


def read(*parts):
    with open(os.path.join(*parts)) as f:
        return f.read()


class Db:
    """One throwaway database with the repo's schema applied up to a given migration."""

    def __init__(self):
        self.name = 't_' + uuid.uuid4().hex[:12]
        admin = psycopg2.connect(server().get_uri())
        admin.autocommit = True
        admin.cursor().execute(f'CREATE DATABASE {self.name}')
        admin.close()
        self.conn = psycopg2.connect(server().get_uri(self.name))
        self.conn.autocommit = True
        self.cur = self.conn.cursor()

    def run(self, sql, args=None):
        self.cur.execute(sql, args)
        try:
            return self.cur.fetchall()
        except psycopg2.ProgrammingError:
            return None

    def script(self, sql):
        """Each call is its own implicit transaction (006 must commit before 007 runs)."""
        self.cur.execute(sql)

    def migration(self, name):
        self.script(read(MIGRATIONS, name))

    def base(self):
        self.script(read(HERE, 'sql', 'supabase_mock.sql'))
        self.script(read(HERE, 'sql', 'ios_baseline.sql'))
        for name in sorted(os.listdir(MIGRATIONS)):
            if name[:3] in ('001', '002', '003', '004', '005', '006', '007', '008', '009'):
                self.migration(name)

    def ledger(self, meeting_type='text'):
        self.script(read(HERE, 'sql', 'mock_minutes_ledger.sql').replace('__MEETING_TYPE__', meeting_type))

    def as_role(self, role, sub=None):
        self.run(f'RESET ROLE')
        self.run(f'SET ROLE {role}')
        if sub:
            self.run("SELECT set_config('request.jwt.claim.sub', %s, false)", (sub,))

    def reset(self):
        self.run('RESET ROLE')
        self.run("SELECT set_config('request.jwt.claim.sub', '', false)")

    def close(self):
        self.conn.close()


@unittest.skipIf(pgserver is None, 'pgserver/psycopg2 not installed (optional)')
class MigrationOrderTests(unittest.TestCase):
    def test_full_chain_applies_in_order_and_twice(self):
        db = Db()
        self.addCleanup(db.close)
        db.base()
        for name in sorted(os.listdir(MIGRATIONS)):      # idempotency: re-run everything numbered 006+
            if name[:3] in ('006', '007', '008', '009'):
                db.migration(name)
        statuses = [r[0] for r in db.run("SELECT unnest(enum_range(NULL::transcription_job_status))::text")]
        self.assertIn('awaiting_upload', statuses)
        self.assertEqual(db.run("SELECT count(*) FROM information_schema.columns WHERE table_name='transcription_jobs' AND column_name IN ('stage','storage_path','expected_bytes')")[0][0], 3)

    def test_006_must_commit_before_007(self):
        """In one transaction 007's partial index cannot use the new enum value (documents the ordering rule)."""
        db = Db()
        self.addCleanup(db.close)
        db.script(read(HERE, 'sql', 'supabase_mock.sql'))
        db.script(read(HERE, 'sql', 'ios_baseline.sql'))
        for name in ('001_create_audio_chunks_table.sql', '002_update_transcription_jobs_for_chunking.sql', '004_make_audio_url_nullable.sql', '005_add_language_column.sql'):
            db.migration(name)
        with self.assertRaises(psycopg2.Error):
            db.script('BEGIN;\n' + read(MIGRATIONS, '006_add_awaiting_upload_status.sql') + '\n' + read(MIGRATIONS, '007_background_upload_columns.sql') + '\nCOMMIT;')
        db.conn.rollback()


@unittest.skipIf(pgserver is None, 'pgserver/psycopg2 not installed (optional)')
class ServerMinutesDebitTests(unittest.TestCase):
    meeting_type = 'text'

    def setUp(self):
        self.db = Db()
        self.addCleanup(self.db.close)
        self.db.base()
        self.db.ledger(self.meeting_type)
        self.db.migration('011_server_minutes_debit.sql')
        self.meeting = str(uuid.uuid4())
        self.job = str(uuid.uuid4())
        self.db.run("INSERT INTO auth.users VALUES (%s), (%s)", (USER, OTHER))
        self.db.run("INSERT INTO mock_minutes_balance VALUES (%s, 100), (%s, 50)", (USER, OTHER))
        self.db.run("INSERT INTO transcription_jobs (id, user_id, meeting_id, audio_url, status, billable_seconds) VALUES (%s, %s, %s, 'x', 'completed', 125)",
                    (self.job, USER, self.meeting))

    def debit(self, minutes=3, user=USER, meeting=None, job=None, seconds=125.0, provider='xai'):
        self.db.as_role('service_role')
        try:
            return self.db.run('SELECT public.debit_minutes_for_job(%s, %s, %s, %s, %s, %s)',
                               (user, meeting or self.meeting, job or self.job, minutes, seconds, provider))[0][0]
        finally:
            self.db.reset()

    def balance(self, user=USER):
        return self.db.run('SELECT balance FROM mock_minutes_balance WHERE user_id=%s', (user,))[0][0]

    def job_row(self, job=None):
        return self.db.run('SELECT minutes_debited, debit_status, debited_at IS NOT NULL FROM transcription_jobs WHERE id=%s', (job or self.job,))[0]

    def test_debits_once_through_the_real_ledger_and_records_the_job(self):
        result = self.debit()
        self.assertEqual((result['status'], result['minutes_debited'], result['balance_after']), ('debited', 3, 97))
        self.assertEqual(self.balance(), 97)
        self.assertEqual(self.db.run('SELECT count(*), sum(amount) FROM mock_minutes_ledger')[0], (1, 3))
        self.assertEqual(self.job_row(), (3, 'debited', True))
        self.assertEqual(self.db.run('SELECT provider, billed_seconds, minutes_requested, minutes_debited, status FROM server_minutes_debits')[0], ('xai', 125.0, 3, 3, 'debited'))

    def test_repeat_calls_are_idempotent_per_meeting_and_per_job(self):
        self.debit(); second = self.debit(); third = self.debit(minutes=9)
        self.assertEqual((second['status'], third['status']), ('already_debited', 'already_debited'))
        self.assertEqual(self.balance(), 97)
        other_job_same_meeting = str(uuid.uuid4())
        self.db.run("INSERT INTO transcription_jobs (id, user_id, meeting_id, audio_url, status) VALUES (%s, %s, %s, 'x', 'completed')", (other_job_same_meeting, USER, self.meeting))
        self.assertEqual(self.debit(job=other_job_same_meeting)['status'], 'already_debited')
        self.assertEqual(self.balance(), 97)
        self.assertEqual(self.db.run('SELECT count(*) FROM server_minutes_debits')[0][0], 1)
        self.assertEqual(self.job_row(other_job_same_meeting)[:2], (3, 'debited'))   # its row is also marked: the sweep stops retrying it

    def test_client_debit_first_then_worker_charges_nothing(self):
        """On-device fallback ran first: the real ledger already holds this meeting."""
        self.db.as_role('authenticated', USER)
        self.db.run('SELECT public.debit_minutes(3, %s)', (self.meeting,))
        self.db.reset()
        result = self.debit()
        self.assertEqual((result['status'], result['minutes_debited']), ('already_debited_by_client', 0))
        self.assertEqual(self.balance(), 97)
        self.assertEqual(self.job_row(), (0, 'already_debited_by_client', True))

    def test_worker_first_then_client_fallback_is_a_duplicate_for_the_app(self):
        self.debit()
        self.db.as_role('authenticated', USER)
        with self.assertRaises(psycopg2.errors.UniqueViolation):      # the app's isDuplicateDebitError treats this as success
            self.db.run('SELECT public.debit_minutes(3, %s)', (self.meeting,))
        self.db.reset()
        self.assertEqual(self.balance(), 97)

    def test_clamp_at_zero_when_balance_is_short(self):
        self.db.run('UPDATE mock_minutes_balance SET balance = 2 WHERE user_id=%s', (USER,))
        result = self.debit(minutes=5)
        self.assertEqual((result['status'], result['minutes_debited'], result['shortfall_minutes'], result['balance_after']), ('partial', 2, 3, 0))
        self.assertEqual(self.balance(), 0)
        self.assertEqual(self.db.run('SELECT shortfall_minutes FROM server_minutes_debits')[0][0], 3)

    def test_zero_balance_takes_nothing_and_does_not_fail(self):
        self.db.run('UPDATE mock_minutes_balance SET balance = 0 WHERE user_id=%s', (USER,))
        result = self.debit(minutes=4)
        self.assertEqual((result['status'], result['minutes_debited'], result['shortfall_minutes']), ('zero_balance', 0, 4))
        self.assertEqual(self.job_row()[:2], (0, 'zero_balance'))

    def test_non_positive_minutes_are_skipped(self):
        self.assertEqual(self.debit(minutes=0)['status'], 'skipped_zero')
        self.assertEqual(self.balance(), 100)

    def test_users_are_independent(self):
        other_job = str(uuid.uuid4())
        self.db.run("INSERT INTO transcription_jobs (id, user_id, meeting_id, audio_url, status) VALUES (%s, %s, %s, 'x', 'completed')", (other_job, OTHER, self.meeting))
        self.debit(); self.debit(user=OTHER, job=other_job, minutes=7)
        self.assertEqual((self.balance(USER), self.balance(OTHER)), (97, 43))

    def test_balance_lookup(self):
        self.db.as_role('service_role')
        self.assertEqual(self.db.run('SELECT public.get_minutes_balance_for_user(%s)', (USER,))[0][0], 100)
        self.assertEqual(self.db.run('SELECT public.get_minutes_balance_for_user(%s)', (str(uuid.uuid4()),))[0][0], 0)
        self.db.reset()

    def test_only_service_role_may_call_the_functions(self):
        for role in ('anon', 'authenticated'):
            self.db.as_role(role, USER)
            with self.assertRaises(psycopg2.errors.InsufficientPrivilege):
                self.db.run('SELECT public.debit_minutes_for_job(%s, %s, %s, 1)', (USER, self.meeting, self.job))
            with self.assertRaises(psycopg2.errors.InsufficientPrivilege):
                self.db.run('SELECT public.get_minutes_balance_for_user(%s)', (USER,))
            self.db.reset()
        self.db.as_role('authenticated', USER)
        with self.assertRaises(psycopg2.errors.InsufficientPrivilege):
            self.db.run('SELECT * FROM server_minutes_debits')
        self.db.reset()

    def test_impersonation_is_restored(self):
        self.db.conn.autocommit = False
        self.db.run("SELECT set_config('request.jwt.claim.sub', %s, true)", (OTHER,))
        self.db.run('SELECT public.debit_minutes_for_job(%s, %s, %s, 1)', (USER, self.meeting, self.job))
        self.assertEqual(self.db.run("SELECT current_setting('request.jwt.claim.sub', true)")[0][0], OTHER)
        self.db.conn.rollback()
        self.db.conn.autocommit = True

    def test_migration_is_idempotent(self):
        self.debit()
        self.db.migration('011_server_minutes_debit.sql')
        self.assertEqual(self.debit()['status'], 'already_debited')

    def test_unexpected_ledger_errors_surface_instead_of_being_swallowed(self):
        self.db.run("CREATE OR REPLACE FUNCTION public.debit_minutes(p_amount integer, p_meeting_id %s DEFAULT NULL) RETURNS integer LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'permission denied for table ledger'; END $$" % self.meeting_type)
        with self.assertRaises(psycopg2.Error) as ctx:
            self.debit()
        self.assertIn('permission denied', str(ctx.exception))
        self.assertEqual(self.db.run('SELECT count(*) FROM server_minutes_debits')[0][0], 0)   # nothing recorded: the sweep will retry


class ServerMinutesDebitUuidMeetingTypeTests(ServerMinutesDebitTests):
    """The real debit_minutes may take a uuid instead of text for p_meeting_id."""
    meeting_type = 'uuid'


@unittest.skipIf(pgserver is None, 'pgserver/psycopg2 not installed (optional)')
class TranscriptionJobsRlsTests(unittest.TestCase):
    """Migration 010 against the ORIGINAL policies of the iOS repo (tests/sql/ios_baseline.sql)."""

    IOS_INSERT = ("INSERT INTO transcription_jobs (user_id, meeting_id, audio_url, status, transcript, duration, completed_at, overview, summary, actions, "
                  "progress_percentage, current_stage) VALUES (%s, %s, 'https://x/recordings/u/m.m4a', 'completed', 't', 61.5, now(), 'o', 's', '[]'::jsonb, 100, 'Completed')")
    IOS_UPDATE = ("UPDATE transcription_jobs SET user_id=%s, meeting_id=%s, audio_url='https://x/recordings/u/m.m4a', status='completed', transcript='t2', duration=61.5, "
                  "error_message=NULL, completed_at=now(), overview='o', summary='s', actions='[]'::jsonb, progress_percentage=100, current_stage='Completed' WHERE id=%s")

    def setUp(self):
        self.db = Db()
        self.addCleanup(self.db.close)
        self.db.base()
        self.db.run("INSERT INTO auth.users VALUES (%s), (%s)", (USER, OTHER))
        self.meeting = str(uuid.uuid4())
        self.db.migration('010_harden_transcription_jobs_rls.sql')
        self.db.migration('012_enable_rls_audio_chunks.sql')

    def as_user(self, who=USER, role='authenticated'):
        self.db.as_role(role, who)

    def row(self, status, user=USER, **cols):
        """Insert a worker-side row (superuser bypasses RLS)."""
        self.db.reset()
        rid = str(uuid.uuid4())
        self.db.run("INSERT INTO transcription_jobs (id, user_id, meeting_id, audio_url, status) VALUES (%s, %s, %s, 'https://x/a', %s)", (rid, user, self.meeting, status))
        return rid

    def denied(self, sql, args=None, error=psycopg2.errors.InsufficientPrivilege):
        with self.assertRaises(error):
            self.db.run(sql, args)
        self.db.reset()

    def test_legit_ios_insert_and_update_still_work(self):
        self.as_user()
        self.db.run(self.IOS_INSERT, (USER, self.meeting))
        rid = self.db.run("SELECT id FROM transcription_jobs WHERE meeting_id=%s", (self.meeting,))[0][0]
        self.db.run(self.IOS_UPDATE, (USER, self.meeting, rid))
        self.assertEqual(self.db.run("SELECT transcript, status::text FROM transcription_jobs WHERE id=%s", (rid,))[0], ('t2', 'completed'))

    def test_ios_update_of_a_failed_or_still_running_server_job_works(self):
        """On-device fallback completes a job whose server run failed / is still pending or processing."""
        for status in ('failed', 'pending', 'processing'):
            rid = self.row(status)
            self.as_user()
            self.db.run(self.IOS_UPDATE, (USER, self.meeting, rid))
            self.assertEqual(self.db.run("SELECT status::text FROM transcription_jobs WHERE id=%s", (rid,))[0][0], 'completed')
            self.db.reset()

    def test_user_cannot_queue_a_job_through_postgrest(self):
        for status in ('pending', 'processing', 'awaiting_upload', 'failed'):
            self.as_user()
            self.denied("INSERT INTO transcription_jobs (user_id, meeting_id, audio_url, status) VALUES (%s, %s, 'http://169.254.169.254/', %s)",
                        (USER, self.meeting, status))
        # status omitted -> column default 'pending' -> not 'completed' -> RLS violation
        self.as_user()
        self.denied("INSERT INTO transcription_jobs (user_id, meeting_id, audio_url) VALUES (%s, %s, 'http://169.254.169.254/')", (USER, self.meeting))

    def test_user_cannot_requeue_or_retarget_an_existing_row(self):
        rid = self.row('completed')
        for set_clause in ("status='pending'", "status='processing'", "status='awaiting_upload'", "status='failed'"):
            self.as_user()
            self.denied(f"UPDATE transcription_jobs SET {set_clause} WHERE id=%s", (rid,), psycopg2.errors.InsufficientPrivilege)
        self.assertEqual(self.db.run("SELECT status::text FROM transcription_jobs WHERE id=%s", (rid,))[0][0], 'completed')

    def test_user_cannot_touch_worker_owned_columns(self):
        rid = self.row('pending')
        for col, value in (('transcription_provider', "'xai'"), ('is_chunked', 'true'), ('total_chunks', '500'),
                           ('storage_path', "'other/x.m4a'"), ('expected_bytes', '1'), ('upload_deadline', 'now()'), ('stage', "'queued'"),
                           ('language', "'it'"), ('created_at', 'now()'), ('id', 'gen_random_uuid()'), ('chunks_processed', '3')):
            self.as_user()
            self.denied(f"UPDATE transcription_jobs SET {col}={value} WHERE id=%s", (rid,))
            self.as_user()
            self.denied(f"INSERT INTO transcription_jobs (user_id, meeting_id, status, {col}) VALUES (%s, %s, 'completed', {value})", (USER, self.meeting))

    def test_user_cannot_touch_the_billing_columns_of_migration_011(self):
        self.db.ledger('text')
        self.db.migration('011_server_minutes_debit.sql')
        rid = self.row('completed')
        for col, value in (('billable_seconds', '1'), ('minutes_debited', '0'), ('debited_at', 'now()'), ('debit_status', "'debited'")):
            self.as_user()
            self.denied(f"UPDATE transcription_jobs SET {col}={value} WHERE id=%s", (rid,))

    def test_user_cannot_write_other_users_rows(self):
        other_row = self.row('completed', user=OTHER)
        mine = self.row('completed')
        self.as_user()
        self.denied(self.IOS_INSERT, (OTHER, self.meeting))
        self.as_user()
        self.db.run("UPDATE transcription_jobs SET transcript='hacked' WHERE id=%s", (other_row,))   # RLS hides the row: 0 rows updated
        self.db.reset()
        self.assertIsNone(self.db.run("SELECT transcript FROM transcription_jobs WHERE id=%s", (other_row,))[0][0])
        self.as_user()
        self.denied("UPDATE transcription_jobs SET user_id=%s WHERE id=%s", (OTHER, mine))   # cannot hand a row to someone else

    def test_no_delete_no_anon_and_select_still_scoped(self):
        mine, theirs = self.row('completed'), self.row('completed', user=OTHER)
        self.as_user()
        self.denied("DELETE FROM transcription_jobs WHERE id=%s", (mine,))
        self.as_user()
        self.assertEqual([r[0] for r in self.db.run("SELECT id::text FROM transcription_jobs")], [mine])
        self.db.reset()
        self.as_user(role='anon')
        self.denied("SELECT * FROM transcription_jobs")
        self.as_user(role='anon')
        self.denied(self.IOS_INSERT, (USER, self.meeting))

    def test_service_role_keeps_full_control(self):
        rid = self.row('pending')
        self.db.as_role('service_role')
        self.db.run("UPDATE transcription_jobs SET status='processing', stage='transcribing', transcription_provider='xai' WHERE id=%s", (rid,))
        self.db.run("UPDATE transcription_jobs SET status='pending' WHERE id=%s", (rid,))
        self.db.run("INSERT INTO transcription_jobs (user_id, meeting_id, status, storage_path) VALUES (%s, %s, 'awaiting_upload', 'p')", (USER, self.meeting))
        self.db.run("DELETE FROM transcription_jobs WHERE id=%s", (rid,))
        self.db.reset()

    def test_live_activity_token_policy_still_works(self):
        rid = self.row('pending')
        self.as_user()
        self.db.run("INSERT INTO live_activity_tokens (job_id, user_id, token, environment) VALUES (%s, %s, %s, 'sandbox')", (rid, USER, 'ab' * 32))
        self.db.reset()

    def test_retry_count_is_not_defined_by_any_repo_migration(self):
        """Documents a gap: supabase_client.increment_retry_count writes retry_count, but no migration creates it."""
        cols = [r[0] for r in self.db.run("SELECT column_name FROM information_schema.columns WHERE table_name='transcription_jobs'")]
        self.assertNotIn('retry_count', cols)

    def test_migration_is_idempotent_and_policies_are_exactly_ours(self):
        self.db.migration('010_harden_transcription_jobs_rls.sql')
        names = sorted(r[0] for r in self.db.run("SELECT policyname FROM pg_policies WHERE tablename='transcription_jobs'"))
        self.assertEqual(names, sorted(['Users can insert completed transcription results', 'Users can update own rows to completed results',
                                        'Users can view their own transcription jobs', 'Service role has full access to transcription jobs']))

    def test_warns_about_foreign_permissive_policies(self):
        self.db.run('CREATE POLICY dashboard_made ON transcription_jobs FOR INSERT TO authenticated WITH CHECK (true)')
        self.db.conn.notices.clear()
        self.db.migration('010_harden_transcription_jobs_rls.sql')
        self.assertTrue(any('dashboard_made' in n for n in self.db.conn.notices))

    # --- audio_chunks (012) ---
    def chunk_insert(self, path, user=USER, idx=0):
        return self.db.run("INSERT INTO audio_chunks (meeting_id, user_id, chunk_index, total_chunks, file_path, file_size, duration_seconds) VALUES (%s, %s, %s, 2, %s, 10, 5)",
                           (self.meeting, user, idx, path))

    def test_audio_chunks_legit_insert_and_scoped_select(self):
        self.as_user()
        self.chunk_insert(f'{USER}/{self.meeting}_chunk_0.m4a')
        self.assertEqual(len(self.db.run('SELECT * FROM audio_chunks')), 1)
        self.db.reset()
        self.as_user(OTHER)
        self.assertEqual(self.db.run('SELECT * FROM audio_chunks'), [])

    def test_audio_chunks_rejects_foreign_or_traversal_paths_and_other_writes(self):
        self.as_user()
        for bad in (f'{OTHER}/x_chunk_0.m4a', 'one', f'{USER}/../{OTHER}/x.m4a', f'{USER.upper()}/x.m4a'):
            with self.assertRaises(psycopg2.errors.InsufficientPrivilege):
                self.chunk_insert(bad)
            self.db.reset(); self.as_user()
        with self.assertRaises(psycopg2.errors.InsufficientPrivilege):
            self.chunk_insert(f'{OTHER}/x_chunk_0.m4a', user=OTHER)
        self.db.reset(); self.as_user()
        with self.assertRaises(psycopg2.errors.InsufficientPrivilege):
            self.db.run("UPDATE audio_chunks SET transcript='x'")
        self.db.reset(); self.as_user()
        with self.assertRaises(psycopg2.errors.InsufficientPrivilege):
            self.db.run("DELETE FROM audio_chunks")
        self.db.reset()
        self.as_user(role='anon')
        with self.assertRaises(psycopg2.errors.InsufficientPrivilege):
            self.db.run("SELECT * FROM audio_chunks")
        self.db.reset()

    def test_worker_can_still_update_chunks(self):
        self.as_user()
        self.chunk_insert(f'{USER}/{self.meeting}_chunk_0.m4a')
        self.db.reset()
        self.db.as_role('service_role')
        self.db.run("UPDATE audio_chunks SET transcript='done', transcribed=true")
        self.db.reset()


@unittest.skipIf(pgserver is None, 'pgserver/psycopg2 not installed (optional)')
class MissingLedgerTests(unittest.TestCase):
    def test_migration_011_aborts_cleanly_without_the_ledger_functions(self):
        db = Db()
        self.addCleanup(db.close)
        db.base()
        with self.assertRaises(psycopg2.Error) as ctx:
            db.migration('011_server_minutes_debit.sql')
        self.assertIn('TODO(owner)', str(ctx.exception))
        self.assertEqual(db.run("SELECT count(*) FROM information_schema.columns WHERE table_name='transcription_jobs' AND column_name='billable_seconds'")[0][0], 0)
        self.assertIsNone(db.run("SELECT to_regclass('public.server_minutes_debits')")[0][0])


if __name__ == '__main__':
    unittest.main()
