"""Characterize the pinned SDK's signed multipart PUT without contacting storage."""
import unittest
import httpx
from storage3 import SyncStorageClient
from upload_sessions import SupabaseUploadStorage

class SignedTransportTests(unittest.TestCase):
    def test_sdk_signed_upload_is_multipart_put(self):
        requests=[]
        def handle(request):
            requests.append(request)
            if request.method=='POST': return httpx.Response(200,json={'url':'/object/upload/sign/recordings/owner/test.m4a?token=opaque'})
            return httpx.Response(200,json={'Key':'recordings/owner/test.m4a'})
        http=httpx.Client(transport=httpx.MockTransport(handle))
        from storage3._sync.file_api import SyncBucketProxy
        http.base_url='https://storage.invalid/storage/v1'
        http.headers['Authorization']='Bearer test'
        bucket=SyncBucketProxy('recordings',http)
        signed=bucket.create_signed_upload_url('owner/test.m4a')
        bucket.upload_to_signed_url('owner/test.m4a',signed['token'],b'abc',{'content-type':'audio/mp4'})
        put=requests[1]
        self.assertEqual(put.method,'PUT');self.assertIn('multipart/form-data; boundary=',put.headers['content-type'])
        self.assertIn(b'Content-Type: audio/mp4',put.content);self.assertIn(b'abc',put.content)
        self.assertEqual(signed['signed_url'],'https://storage.invalid/storage/v1//object/upload/sign/recordings/owner/test.m4a?token=opaque')

    def test_existing_unverified_object_can_receive_replacement_credentials(self):
        from types import SimpleNamespace
        from storage3._sync.file_api import SyncBucketProxy
        captured=[]
        def handle(request):
            captured.append(request)
            if request.headers.get('x-upsert') != 'true':
                return httpx.Response(400,json={'statusCode':400,'error':'Duplicate','message':'The resource already exists'})
            return httpx.Response(200,json={'url':'/object/upload/sign/recordings/owner/test.m4a?token=opaque'})
        http=httpx.Client(base_url='https://storage.invalid/storage/v1/',transport=httpx.MockTransport(handle))
        bucket=SyncBucketProxy('recordings',http)
        client=SimpleNamespace(storage=SimpleNamespace(from_=lambda name:bucket))
        result=SupabaseUploadStorage(client).sign('owner/test.m4a','audio/mp4','snipnote-test')
        self.assertEqual(captured[0].headers.get('x-upsert'),'true')
        self.assertEqual(result['method'],'PUT')
        self.assertIn('/object/upload/sign/recordings/owner/test.m4a?token=opaque',result['upload_url'])
