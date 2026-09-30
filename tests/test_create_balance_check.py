"""Create-time balance check (402) behind SERVER_MINUTES_DEBIT_ENABLED. No network."""
import os
import unittest
from unittest.mock import patch

import support
from support import TEST_USER_ID, TEST_MEETING_ID
import main
import supabase_client as sc
import test_background_upload as tbu
import test_legacy_create_policy as tlp


class BalanceBase(tlp.PolicyBase):
    def setUp(self):
        super().setUp()
        self.balance = 10
        self.balance_calls = []

        def balance(user):
            self.balance_calls.append(user)
            if isinstance(self.balance, Exception):
                raise self.balance
            return self.balance
        p = patch.object(sc, 'get_minutes_balance', side_effect=balance)
        p.start()
        self.addCleanup(p.stop)
        os.environ['SERVER_MINUTES_DEBIT_ENABLED'] = 'true'
        self.addCleanup(os.environ.pop, 'SERVER_MINUTES_DEBIT_ENABLED', None)


class LegacyBalanceTests(BalanceBase):
    def test_flag_off_means_no_balance_lookup_at_all(self):
        os.environ.pop('SERVER_MINUTES_DEBIT_ENABLED')
        self.balance = 0
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(self.balance_calls, [])

    def test_chunked_job_needs_ceil_minutes_of_its_duration(self):
        self.assertEqual(self.post(self.chunked(duration=600.0)).status_code, 200)            # 10 min, balance 10
        r = self.post(self.chunked(duration=601.0))                                           # 11 min
        self.assertEqual(r.status_code, 402)
        self.assertEqual(r.json()['detail'], {'error': 'insufficient_minutes', 'required_minutes': 11, 'reserved_minutes': 0, 'balance_minutes': 10})
        self.assertEqual(len(self.created), 1)
        self.assertEqual(self.balance_calls, [TEST_USER_ID, TEST_USER_ID])

    def test_regular_job_uses_the_recordings_row_duration(self):
        self.recording = {'file_path': f'{TEST_USER_ID}/{TEST_MEETING_ID}.m4a', 'duration': 3600, 'file_size': 1}
        self.assertEqual(self.post().status_code, 402)
        self.recording['duration'] = 60
        self.assertEqual(self.post().status_code, 200)

    def test_other_queued_jobs_reserve_their_minutes(self):
        self.active = [{'id': 'a', 'meeting_id': '11111111-0000-4000-8000-000000000000', 'status': 'processing', 'duration': 360.0},   # 6 min
                       {'id': 'b', 'meeting_id': '22222222-0000-4000-8000-000000000000', 'status': 'awaiting_upload', 'duration': 9999.0}]  # not reserved yet
        self.assertEqual(self.post(self.chunked(duration=240.0)).status_code, 200)    # 4 + 6 = 10
        r = self.post(self.chunked(duration=241.0))                                    # 5 + 6 = 11
        self.assertEqual((r.status_code, r.json()['detail']['reserved_minutes']), (402, 6))

    def test_unknown_duration_unknown_balance_or_rpc_failure_never_block(self):
        self.assertEqual(self.post({k: v for k, v in self.chunked().items() if k != 'duration'}).status_code, 200)   # no duration
        self.recording = None
        self.assertEqual(self.post().status_code, 200)                                  # regular job, no row: duration unknown
        self.balance = None
        self.assertEqual(self.post(self.chunked(duration=40000.0)).status_code, 200)
        self.balance = Exception('function get_minutes_balance_for_user does not exist')
        self.assertEqual(self.post(self.chunked(duration=40000.0)).status_code, 200)

    def test_applies_in_every_auth_mode_when_the_duration_is_in_the_request(self):
        for mode in ('off', 'log'):
            os.environ['AUTH_MODE'] = mode
            self.assertEqual(self.post(self.chunked(duration=40000.0)).status_code, 402, mode)

    def test_duplicate_active_job_is_returned_without_a_balance_check(self):
        self.balance = 0
        self.active = [{'id': 'existing', 'meeting_id': TEST_MEETING_ID, 'status': 'pending', 'created_at': 'x', 'duration': 120.0}]
        self.assertEqual(self.post(self.chunked(duration=120.0)).json()['job_id'], 'existing')
        self.assertEqual(self.balance_calls, [])


class UploadModeBalanceTests(tbu.EnvBase):
    def setUp(self):
        super().setUp()
        self.db = tbu.FakeStorageAndDB()
        from contextlib import ExitStack
        stack = ExitStack()
        self.db.patch(stack)
        self.addCleanup(stack.close)
        for name, value in (('get_minutes_balance', 5), ('list_active_jobs', [])):
            p = patch.object(sc, name, side_effect=(lambda *a, v=value, **k: v))
            p.start()
            self.addCleanup(p.stop)
        os.environ['SERVER_MINUTES_DEBIT_ENABLED'] = 'true'
        self.addCleanup(os.environ.pop, 'SERVER_MINUTES_DEBIT_ENABLED', None)
        from fastapi.testclient import TestClient
        self.api = TestClient(main.app)
        self.body = {'upload_pending': True, 'meeting_id': tbu.MEETING, 'expected_bytes': 1000, 'duration': 600.0}

    def post(self, **extra):
        return self.api.post('/jobs', json={**self.body, **extra}, headers={'Authorization': f'Bearer {tbu.make_token()}'})

    def test_402_is_not_turned_into_a_500_and_creates_nothing(self):
        r = self.post(duration=601.0)   # 11 min > 5
        self.assertEqual(r.status_code, 402, r.text)
        self.assertEqual(r.json()['detail']['error'], 'insufficient_minutes')
        self.assertEqual(self.db.rows, {})

    def test_enough_balance_or_unknown_duration_creates_the_job(self):
        self.assertEqual(self.post(duration=300.0).status_code, 200)
        self.db.rows.clear()
        self.assertEqual(self.api.post('/jobs', json={k: v for k, v in self.body.items() if k != 'duration'},
                                       headers={'Authorization': f'Bearer {tbu.make_token()}'}).status_code, 200)

    def test_flag_off_skips_the_check(self):
        os.environ.pop('SERVER_MINUTES_DEBIT_ENABLED')
        self.assertEqual(self.post(duration=40000.0).status_code, 200)


if __name__ == '__main__':
    unittest.main()
