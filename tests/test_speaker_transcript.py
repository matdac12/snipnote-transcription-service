import unittest

from speaker_transcript import (
    render_speaker_transcript,
    stitch_short_intrusions,
    turns_from_words,
)


class SpeakerTranscriptTests(unittest.TestCase):
    def test_renders_stitched_turns_with_one_based_speakers(self):
        words = [
            {"text": "Ciao", "start": 0.2, "end": 0.6, "speaker": 0},
            {"text": "Mattia.", "start": 0.6, "end": 1.1, "speaker": 0},
            {"text": "sì", "start": 1.2, "end": 1.3, "speaker": 1},
            {"text": "eccomi.", "start": 1.4, "end": 2.2, "speaker": 0},
            {"text": "Allora", "start": 2.3, "end": 2.8, "speaker": 0},
        ]
        text = render_speaker_transcript(stitch_short_intrusions(turns_from_words(words)))
        self.assertEqual(
            text,
            "[0:00:00–0:00:02] Speaker 1\nCiao Mattia. sì eccomi. Allora\n",
        )

    def test_keeps_a_short_reply_between_two_different_speakers(self):
        words = [
            {"text": "Domanda", "start": 0.0, "end": 1.0, "speaker": 0},
            {"text": "Sì.", "start": 1.2, "end": 1.5, "speaker": 1},
            {"text": "Continuo", "start": 2.0, "end": 3.0, "speaker": 2},
        ]
        turns = stitch_short_intrusions(turns_from_words(words))
        self.assertEqual([turn["speaker"] for turn in turns], [0, 1, 2])


if __name__ == "__main__":
    unittest.main()
