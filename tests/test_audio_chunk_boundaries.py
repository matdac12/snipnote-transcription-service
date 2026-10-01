"""Real codec coverage for redundant overlapping tail chunks."""
import io
import unittest

import support
from pydub import AudioSegment
from transcribe import chunk_audio


class AudioChunkBoundaryTests(unittest.TestCase):
    def wav(self, duration_ms):
        audio = AudioSegment.silent(duration=duration_ms, frame_rate=16000)
        data = io.BytesIO()
        audio.export(data, format='wav')
        return data.getvalue()

    def durations(self, chunks):
        return [len(AudioSegment.from_file(io.BytesIO(chunk), format='mp3'))
                for chunk in chunks]

    def test_tail_already_in_overlap_is_not_submitted_separately(self):
        # The 369 ms tail is included in the first chunk's two-second overlap.
        chunks = chunk_audio(self.wav(60369), 'meeting.wav')
        self.assertEqual(len(chunks), 1)
        self.assertAlmostEqual(self.durations(chunks)[0], 60369, delta=2)

    def test_audio_beyond_overlap_keeps_final_chunk(self):
        chunks = chunk_audio(self.wav(62369), 'meeting.wav')
        self.assertEqual(len(chunks), 2)
        durations = self.durations(chunks)
        self.assertAlmostEqual(durations[0], 62000, delta=2)
        self.assertAlmostEqual(durations[1], 2369, delta=2)
        self.assertAlmostEqual(sum(durations) - 2000, 62369, delta=4)
