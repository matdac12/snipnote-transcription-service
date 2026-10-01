"""A decoded, forged or missing token cannot authorize session writes."""
import unittest
from types import SimpleNamespace
from uuid import uuid4
from fastapi import HTTPException
from upload_auth import verify_upload_user

class UploadAuthTests(unittest.TestCase):
    def test_remote_verified_identity(self):
        uid=str(uuid4()); seen=[]
        def verify(token):
            seen.append(token);return SimpleNamespace(user=SimpleNamespace(id=uid,is_anonymous=False))
        self.assertEqual(verify_upload_user('Bearer real-token',verify),uid);self.assertEqual(seen,['real-token'])
    def test_unverified_rejected(self):
        def invalid(token):raise ValueError('secret')
        for header in [None,'Basic token','Bearer forged']:
            with self.assertRaises(HTTPException) as e:verify_upload_user(header,invalid)
            self.assertEqual(e.exception.status_code,401);self.assertNotIn('secret',str(e.exception.detail))
    def test_anonymous_user_rejected(self):
        with self.assertRaises(HTTPException):verify_upload_user('Bearer token',lambda _:SimpleNamespace(user=SimpleNamespace(id=str(uuid4()),is_anonymous=True)))
