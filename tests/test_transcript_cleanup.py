import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import support
import transcript_cleanup


def _edit(find, replace, kind="repetition", op="replace"):
    return {
        "op": op, "find": find, "replace": replace, "type": kind,
        "confidence": "high", "evidence": "", "reason": "test",
    }


class ApplyTests(unittest.TestCase):
    def test_immediate_repetition_is_collapsed(self):
        text = "fino a lunedì parte parte il il disastro nel senso"
        cleaned, applied, skipped = transcript_cleanup.apply_edits(text, [
            _edit("parte parte il il disastro", "parte il disastro"),
        ])
        self.assertEqual(cleaned, "fino a lunedì parte il disastro nel senso")
        self.assertEqual(len(applied), 1)
        self.assertEqual(skipped, [])

    def test_false_start_keeps_the_restarted_phrase(self):
        text = "No, che sa No, che sa tutte le mie cose"
        cleaned, applied, _skipped = transcript_cleanup.apply_edits(text, [
            _edit("No, che sa No, che sa tutte", "No, che sa tutte", kind="false_start"),
        ])
        self.assertEqual(cleaned, "No, che sa tutte le mie cose")
        self.assertEqual(len(applied), 1)

    def test_misheard_term_can_introduce_the_corrected_form(self):
        text = "nella pagina di calendri possiamo vedere"
        cleaned, applied, _skipped = transcript_cleanup.apply_edits(text, [
            _edit("pagina di calendri possiamo", "pagina di Calendly possiamo", kind="misheard"),
        ])
        self.assertEqual(cleaned, "nella pagina di Calendly possiamo vedere")
        self.assertEqual(applied[0]["replace"], "pagina di Calendly possiamo")

    def test_paraphrase_is_not_applied(self):
        text = "come tutti i telefoni oggi"
        _cleaned, applied, skipped = transcript_cleanup.apply_edits(text, [
            _edit("come tutti i telefoni", "come ti dicevo al telefono", kind="false_start"),
        ])
        self.assertEqual(applied, [])
        self.assertEqual(skipped[0]["bad"], "not a subsequence")
        self.assertEqual(_cleaned, text)

    def test_misheard_cannot_drop_senza(self):
        text = "Comunque, senza biologiche, un anno."
        cleaned, applied, skipped = transcript_cleanup.apply_edits(text, [
            _edit("senza biologiche, un anno", "Scienze Biologiche, un anno", kind="misheard"),
        ])
        self.assertEqual(cleaned, text)
        self.assertEqual(applied, [])
        self.assertEqual(skipped[0]["bad"], "dropped function word")

    def test_vocabulary_spelling_may_replace_senza_biologiche(self):
        text = "Comunque, senza biologiche, un anno."
        vocabulary = [{"term": "Scienze Biologiche", "heard_as": ["senza biologiche"]}]
        cleaned, applied, skipped = transcript_cleanup.apply_edits(text, [
            _edit("senza biologiche, un anno", "Scienze Biologiche, un anno", kind="misheard"),
        ], vocabulary)
        self.assertEqual(cleaned, "Comunque, Scienze Biologiche, un anno.")
        self.assertEqual(len(applied), 1)
        self.assertEqual(skipped, [])

    def test_vocabulary_spelling_replaces_a_garbled_product_name(self):
        text = "nel gestionale J-Garileo va direttamente"
        vocabulary = [{"term": "JGalileo", "heard_as": ["J-Garileo", "Galileo"]}]
        cleaned, applied, _skipped = transcript_cleanup.apply_edits(text, [
            _edit("gestionale J-Garileo va", "gestionale JGalileo va", kind="misheard"),
        ], vocabulary)
        self.assertEqual(cleaned, "nel gestionale JGalileo va direttamente")
        self.assertEqual(len(applied), 1)

    def test_another_vocabulary_term_does_not_drop_senza(self):
        text = "Comunque, senza biologiche, un anno."
        vocabulary = [{"term": "JGalileo", "heard_as": ["Galileo"]}]
        cleaned, applied, skipped = transcript_cleanup.apply_edits(text, [
            _edit("senza biologiche, un anno", "Scienze Biologiche, un anno", kind="misheard"),
        ], vocabulary)
        self.assertEqual(cleaned, text)
        self.assertEqual(applied, [])
        self.assertEqual(skipped[0]["bad"], "dropped function word")

    def test_misheard_rewrite_with_several_new_words_is_not_applied(self):
        text = "mandarmi un team summit oppure"
        cleaned, _applied, skipped = transcript_cleanup.apply_edits(text, [
            _edit("un team summit oppure", "un Teams o Meet oppure", kind="misheard"),
        ])
        self.assertEqual(cleaned, text)
        self.assertEqual(skipped[0]["bad"], "too many new words")

    def test_ambiguous_and_missing_snippets_are_skipped(self):
        text = "il il budget e poi il il budget"
        cleaned, applied, skipped = transcript_cleanup.apply_edits(text, [
            _edit("il il", "il"),
            _edit("parola assente qui", "parola"),
        ])
        self.assertEqual(cleaned, text)
        self.assertEqual(applied, [])
        self.assertEqual({item["bad"] for item in skipped}, {"ambiguous", "not found"})

    def test_overlapping_edits_keep_the_earlier_span(self):
        text = "stavo stavo aspettando qui"
        cleaned, applied, skipped = transcript_cleanup.apply_edits(text, [
            _edit("stavo stavo aspettando", "stavo aspettando"),
            _edit("stavo aspettando qui", "stavo qui"),
        ])
        self.assertEqual(cleaned, "stavo aspettando qui")
        self.assertEqual(len(applied), 1)
        self.assertEqual(skipped[0]["bad"], "overlap")

    def test_unchanged_spacing_survives_a_window_with_no_edits(self):
        text = "prima  seconda\nterza"
        with patch.object(transcript_cleanup, "WINDOW_WORDS", 1), patch.object(transcript_cleanup, "_request_edits", return_value=[]):
            cleaned = transcript_cleanup.clean_transcript(text, "it", "contesto", client=object())
        self.assertEqual(cleaned, text)


class RequestTests(unittest.TestCase):
    def test_summary_is_context_and_edits_come_back_as_json(self):
        response = SimpleNamespace(output=[SimpleNamespace(
            type="message",
            content=[SimpleNamespace(text=json.dumps({"edits": [
                _edit("attivi attivi e", "attivi e"),
            ]}))],
        )])
        with patch.object(transcript_cleanup, "create_response", return_value=response) as create:
            cleaned = transcript_cleanup.clean_transcript(
                "i messaggi effimeri attivi attivi e quindi",
                "it",
                "Il sistema è un assistente AI",
                client="client",
            )
        self.assertEqual(cleaned, "i messaggi effimeri attivi e quindi")
        self.assertEqual(create.call_args.args[1], "transcript_cleanup")
        self.assertEqual(create.call_args.kwargs["text_format"]["type"], "json_schema")
        self.assertIs(create.call_args.kwargs["defaults"], transcript_cleanup.CLEANUP_DEFAULT_CONFIG)
        user = create.call_args.kwargs["input"][1]["content"]
        self.assertIn("LANGUAGE: it", user)
        self.assertIn("VOCABULARY", user)
        self.assertIn("(none)", user)
        self.assertIn("Il sistema è un assistente AI", user)
        self.assertIn("attivi attivi", user)

    def test_speaker_header_is_not_editable_and_filler_still_is(self):
        text = "[0:00:00\u20130:00:04] Speaker 1\nCiao ehm Mattia.\n"
        seen = []

        def fake(_client, window, _context, _language, _vocabulary):
            seen.append(window)
            return [
                _edit("Speaker 1", "", op="delete", kind="filler"),
                _edit("Ciao ehm Mattia", "Ciao Mattia", kind="filler"),
            ]

        with patch.object(transcript_cleanup, "_request_edits", side_effect=fake):
            cleaned = transcript_cleanup.clean_transcript(text, "it", "contesto", client=object())
        self.assertEqual(seen, ["Ciao ehm Mattia."])
        self.assertEqual(cleaned, "[0:00:00\u20130:00:04] Speaker 1\nCiao Mattia.\n")

    def test_plain_transcript_still_uses_one_cleanup_window(self):
        seen = []

        def fake(_client, window, _context, _language, _vocabulary):
            seen.append(window)
            return []

        with patch.object(transcript_cleanup, "_request_edits", side_effect=fake):
            cleaned = transcript_cleanup.clean_transcript("prima seconda terza", "it", "", client=object())
        self.assertEqual(seen, ["prima seconda terza"])
        self.assertEqual(cleaned, "prima seconda terza")

    def test_a_failed_window_stays_raw(self):
        with patch.object(transcript_cleanup, "_request_edits", side_effect=RuntimeError("429")):
            cleaned = transcript_cleanup.clean_transcript("parte parte il disastro", "it", "", client=object())
        self.assertEqual(cleaned, "parte parte il disastro")


SEPARATOR = "\n␞\n"


def _speaker(turns):
    return "\n\n".join(
        f"[0:00:{index:02d}–0:00:{index + 1:02d}] Speaker {index % 2 + 1}\n{body}"
        for index, body in enumerate(turns)
    ) + "\n"


class SpeakerGroupingTests(unittest.TestCase):
    def test_short_turns_share_model_calls_and_headers_stay_byte_for_byte(self):
        turns = ["Ciao ehm Mattia", "buongiorno a tutti", "il il budget", "va bene grazie"]
        text = _speaker(turns)
        seen = []

        def fake(_client, window, _context, _language, _vocabulary):
            seen.append(window)
            if "ehm" in window:
                return [_edit("Ciao ehm Mattia", "Ciao Mattia", kind="filler")]
            return []

        with patch.object(transcript_cleanup, "WINDOW_WORDS", 6), patch.object(transcript_cleanup, "_request_edits", side_effect=fake):
            cleaned = transcript_cleanup.clean_transcript(text, "it", "", client=object())
        self.assertEqual(len(seen), 2)
        self.assertLess(len(seen), len(turns))
        self.assertEqual(sorted(seen), sorted([
            "Ciao ehm Mattia" + SEPARATOR + "buongiorno a tutti",
            "il il budget" + SEPARATOR + "va bene grazie",
        ]))
        for window in seen:
            self.assertNotIn("Speaker", window)
        expected = _speaker(["Ciao Mattia", "buongiorno a tutti", "il il budget", "va bene grazie"])
        self.assertEqual(cleaned, expected)
        original_headers = [line for line in text.splitlines() if line.startswith("[")]
        cleaned_headers = [line for line in cleaned.splitlines() if line.startswith("[")]
        self.assertEqual(cleaned_headers, original_headers)

    def test_an_edit_across_the_separator_keeps_that_group_raw(self):
        text = _speaker(["fine fine", "fine del discorso"])

        def fake(_client, window, _context, _language, _vocabulary):
            return [_edit("fine" + SEPARATOR + "fine del", "fine del", kind="repetition")]

        with patch.object(transcript_cleanup, "_request_edits", side_effect=fake):
            cleaned = transcript_cleanup.clean_transcript(text, "it", "", client=object())
        self.assertEqual(cleaned, text)

    def test_an_edit_that_breaks_the_separator_line_keeps_that_group_raw(self):
        text = _speaker(["prima parte qui.", "seconda parte qui"])

        def fake(_client, window, _context, _language, _vocabulary):
            return [_edit("prima parte qui.\n", "prima parte qui.", kind="punctuation")]

        with patch.object(transcript_cleanup, "_request_edits", side_effect=fake):
            cleaned = transcript_cleanup.clean_transcript(text, "it", "", client=object())
        self.assertEqual(cleaned, text)

    def test_body_with_record_separator_is_cleaned_alone(self):
        turns = ["uno due tre", "strano ␞ testo", "quattro cinque"]
        text = _speaker(turns)
        seen = []

        def fake(_client, window, _context, _language, _vocabulary):
            seen.append(window)
            return []

        with patch.object(transcript_cleanup, "_request_edits", side_effect=fake):
            cleaned = transcript_cleanup.clean_transcript(text, "it", "", client=object())
        # The odd turn goes alone, and neighbours are not grouped across it.
        self.assertEqual(sorted(seen), sorted(["uno due tre", "strano ␞ testo", "quattro cinque"]))
        self.assertEqual(cleaned, text)

    def test_long_turn_is_windowed_and_rejoined_under_its_own_header(self):
        turns = ["a b c d e", "uno due"]
        text = _speaker(turns)
        seen = []

        def fake(_client, window, _context, _language, _vocabulary):
            seen.append(window)
            return []

        with patch.object(transcript_cleanup, "WINDOW_WORDS", 2), patch.object(transcript_cleanup, "_request_edits", side_effect=fake):
            cleaned = transcript_cleanup.clean_transcript(text, "it", "", client=object())
        self.assertEqual(sorted(seen), sorted(["a b", "c d", "e", "uno due"]))
        self.assertEqual(cleaned, text)

    def test_failed_group_keeps_its_original_words(self):
        text = _speaker(["il il budget", "parte parte qui", "ciao ciao a tutti"])

        def fake(_client, window, _context, _language, _vocabulary):
            if "budget" in window:
                raise RuntimeError("429")
            return [_edit("parte parte qui", "parte qui")]

        with patch.object(transcript_cleanup, "WINDOW_WORDS", 3), patch.object(transcript_cleanup, "_request_edits", side_effect=fake):
            cleaned = transcript_cleanup.clean_transcript(text, "it", "", client=object())
        self.assertEqual(cleaned, _speaker(["il il budget", "parte qui", "ciao ciao a tutti"]))


class CleanupProgressTests(unittest.TestCase):
    def test_progress_runs_from_zero_through_the_group_count(self):
        text = _speaker(["uno due", "tre quattro", "cinque sei", "sette otto"])
        calls = []
        with patch.object(transcript_cleanup, "WINDOW_WORDS", 4), patch.object(transcript_cleanup, "_request_edits", return_value=[]):
            transcript_cleanup.clean_transcript(
                text, "it", "", client=object(), on_progress=lambda done, total: calls.append((done, total)),
            )
        self.assertEqual(calls, [(0, 2), (1, 2), (2, 2)])

    def test_plain_transcript_reports_progress_per_window(self):
        calls = []
        with patch.object(transcript_cleanup, "_request_edits", return_value=[]):
            transcript_cleanup.clean_transcript(
                "prima seconda terza", "it", "", client=object(),
                on_progress=lambda done, total: calls.append((done, total)),
            )
        self.assertEqual(calls, [(0, 1), (1, 1)])

    def test_raising_callback_aborts_and_stops_scheduling(self):
        text = _speaker(["uno", "due", "tre", "quattro"])
        requests = []

        class Stop(Exception):
            pass

        def progress(done, total):
            if done == 1:
                raise Stop()

        def fake(_client, window, _context, _language, _vocabulary):
            requests.append(window)
            return []

        with patch.object(transcript_cleanup, "WINDOW_WORDS", 1), patch.object(transcript_cleanup, "MAX_WINDOW_WORKERS", 1), \
                patch.object(transcript_cleanup, "_request_edits", side_effect=fake):
            with self.assertRaises(Stop):
                transcript_cleanup.clean_transcript(text, "it", "", client=object(), on_progress=progress)
        self.assertEqual(requests, ["uno"])

    def test_raising_on_the_zero_update_makes_no_model_call(self):
        class Stop(Exception):
            pass

        def progress(done, total):
            raise Stop()

        with patch.object(transcript_cleanup, "_request_edits") as request:
            with self.assertRaises(Stop):
                transcript_cleanup.clean_transcript(_speaker(["uno"]), "it", "", client=object(), on_progress=progress)
        request.assert_not_called()
