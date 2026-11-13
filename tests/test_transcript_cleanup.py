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

    def test_a_failed_window_stays_raw(self):
        with patch.object(transcript_cleanup, "_request_edits", side_effect=RuntimeError("429")):
            cleaned = transcript_cleanup.clean_transcript("parte parte il disastro", "it", "", client=object())
        self.assertEqual(cleaned, "parte parte il disastro")
