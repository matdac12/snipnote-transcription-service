import io
import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import httpx
import support
import ai_config
import transcribe
from transcription_provider import create_provider_transcription, validate_transcription_provider


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.client = MagicMock()
        self.file = io.BytesIO(b'original-audio-bytes')
        self.file.name = 'meeting.m4a'
        self.config = {'task': 'transcription_xai', 'model': 'configured-grok',
                       'reasoning_effort': None, 'verbosity': None, 'fallback_model': None}
        self.db = patch.object(ai_config.supabase, 'table')
        table = self.db.start()
        table.return_value.select.return_value.execute.side_effect = lambda: SimpleNamespace(data=[self.config])
        ai_config._cache_loaded_at = float('-inf')
        self.key = patch.dict(os.environ, {'XAI_API_KEY': 'xai-fixture-secret'})
        self.key.start()
        self.addCleanup(self.db.stop)
        self.addCleanup(self.key.stop)

    def send(self, statuses=(200,), outputs=None, language='it', provider='xai'):
        def handle(request):
            self.calls.append(request)
            index = len(self.calls)-1
            status = statuses[min(index, len(statuses)-1)]
            body = outputs[index] if outputs else {'text': 'Meeting transcript'}
            return httpx.Response(status, json=body)
        http = httpx.Client(transport=httpx.MockTransport(handle))
        with patch('transcription_provider.httpx.Client', return_value=http):
            return create_provider_transcription(self.client, self.file, language, provider)

    def test_xai_uses_configured_model_and_file_last(self):
        self.assertEqual(self.send(), 'Meeting transcript')
        request = self.calls[0]; body = request.content
        self.assertEqual(str(request.url), 'https://api.x.ai/v1/stt')
        self.assertEqual(request.headers['Authorization'], 'Bearer xai-fixture-secret')
        for value in [b'configured-grok', b'name="language"\r\n\r\nit', b'name="format"\r\n\r\ntrue', b'filename="meeting.m4a"', b'Content-Type: audio/mp4']:
            self.assertIn(value, body)
        self.assertLess(body.index(b'name="model"'), body.index(b'name="file"'))
        self.assertLess(body.index(b'name="format"'), body.index(b'name="file"'))
        for value in [b'response_format', b'audio_format', b'sample_rate']:
            self.assertNotIn(value, body)

    def test_xai_auto_language_omits_format(self):
        self.send(language=None)
        self.assertNotIn(b'name="language"', self.calls[0].content)
        self.assertNotIn(b'name="format"', self.calls[0].content)

    def test_openai_default_preserves_existing_config(self):
        self.config.update(task='transcription', model='configured-gpt')
        self.client.audio.transcriptions.create.return_value.text = 'OpenAI transcript'
        self.assertEqual(create_provider_transcription(self.client, self.file), 'OpenAI transcript')
        self.assertEqual(self.client.audio.transcriptions.create.call_args.kwargs['model'], 'configured-gpt')

    def test_missing_xai_key_never_calls_openai(self):
        with patch.dict(os.environ, {'XAI_API_KEY': ''}):
            with self.assertRaisesRegex(Exception, 'xAI transcription is not configured'):
                self.send()
        self.assertEqual(self.calls, [])
        self.client.audio.transcriptions.create.assert_not_called()

    def test_invalid_provider_rejected(self):
        for value in ['unknown', '', None]:
            with self.assertRaises(ValueError):
                validate_transcription_provider(value)
        self.client.audio.transcriptions.create.assert_not_called()

    def test_xai_fallback_rewinds_audio(self):
        self.config['fallback_model'] = 'fallback-grok'
        self.assertEqual(self.send((400, 200)), 'Meeting transcript')
        for call in self.calls:
            self.assertIn(b'original-audio-bytes', call.content)
        self.assertIn(b'fallback-grok', self.calls[1].content)
        self.client.audio.transcriptions.create.assert_not_called()

    def test_xai_429_never_switches_provider(self):
        self.config['fallback_model'] = 'fallback-grok'
        with self.assertRaisesRegex(Exception, '429') as error:
            self.send((429,), outputs=[{'error': 'xai-fixture-secret private audio'}])
        self.assertNotIn('xai-fixture-secret', str(error.exception))
        self.assertEqual(len(self.calls), 1)
        self.client.audio.transcriptions.create.assert_not_called()

    def test_xai_rejects_malformed_or_blank_text(self):
        for value in [{}, {'text': '  '}, {'text': 4}, [], 'invalid']:
            self.calls.clear()
            with self.assertRaisesRegex(Exception, '502'):
                self.send(outputs=[value])

    def test_all_internal_chunks_keep_provider(self):
        providers = []
        def create(_client, file, language=None, provider='openai'):
            providers.append(provider)
            return 'chunk transcript'
        with patch.object(transcribe, 'chunk_audio', return_value=[b'one', b'two']), patch.object(transcribe, 'create_provider_transcription', side_effect=create):
            result = transcribe.transcribe_audio(b'x' * (transcribe.MAX_CHUNK_SIZE_BYTES+1), 'meeting.m4a', language='it', provider='xai')
            self.assertEqual(result['transcript'], 'chunk transcript chunk transcript')
        self.assertEqual(providers, ['xai', 'xai'])

    def test_transient_retry_keeps_provider_and_audio(self):
        with patch.object(transcribe, 'create_provider_transcription', side_effect=[RuntimeError('HTTP 500'), 'transcript']) as create, patch.object(transcribe.time, 'sleep'):
            self.assertEqual(transcribe.transcribe_chunk_with_retry(b'audio', 'chunk.mp3', provider='xai'), 'transcript')
            self.assertEqual([c.args[3] for c in create.call_args_list], ['xai', 'xai'])
            self.assertEqual([c.args[1].getvalue() for c in create.call_args_list], [b'audio', b'audio'])
