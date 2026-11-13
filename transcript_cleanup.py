"""Surgical cleanup of a reader-facing transcript.

The model returns pinpoint edits. This module applies an edit only when the
snippet matches the transcript once and the replacement stays inside the
rules below. A failed or skipped edit leaves the original words in place.
The meeting summary is context for spelling only; it is not written into
the transcript.
"""

import json
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from openai import OpenAI

from ai_config import CLEANUP_DEFAULT_CONFIG, create_response
from vocabulary import format_for_cleanup

WINDOW_WORDS = 1200
CONTEXT_CHARS = 2200
MAX_WINDOW_WORKERS = 4
# A misheard fix may introduce a short corrected form ("calendri" -> "Calendly",
# "tuberine" -> "tube uterine"). More than this is treated as a rewrite.
MISHEARD_NEW_WORD_LIMIT = 2
# A misheard fix may respell a broken token. It may not erase these words.
_PROTECTED = {"senza", "non", "mai", "niente", "nessuno", "not", "never"}

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_SPEAKER_HEADER = re.compile(r"^\[\d+:\d{2}:\d{2}–\d+:\d{2}:\d{2}\] Speaker \d+$")

SYSTEM = """You are a transcript cleanup editor for a meeting-transcription product. You receive a machine transcript of a real conversation and return surgical edits. The transcript is a record of what people said: your job is to remove machine and speech noise, NOT to improve the speakers' grammar, style, or word choice.

THE INVARIANT
After your edits, every remaining word must be a word a speaker actually said, in the order they said it. You may delete a span, or replace a span with its corrected form. You may not paraphrase, merge, reorder, translate, summarize, or write new sentences. The cleaned text must be readable as the same conversation.

EDIT FORMAT
Each edit is {"op", "find", "replace", "type", "confidence", "evidence", "reason"}:
- op: "delete" or "replace"
- find: EXACT verbatim substring copied from the transcript (same characters, punctuation, casing), 3-12 words, unique in the window. Use the smallest span that contains the problem plus enough context to be unique.
- replace: "" when op is "delete". When op is "replace", the minimal corrected text, keeping as much of the original span as possible.
- type: "filler" | "repetition" | "false_start" | "misheard" | "punctuation"
- confidence: "high" or "medium" (see below). Do not emit low-confidence edits.
- evidence: for "misheard", the VOCABULARY spelling, or the exact quote from MEETING CONTEXT that supports the corrected term; "" if the fix rests only on the word being impossible as written.
- reason: a few words, max 12.

CONFIDENCE
- high: mechanically verifiable. An isolated hesitation sound; an immediate duplicate word or n-gram; an abandoned fragment that is a strict prefix of the phrase the speaker then restarts; a mishearing replaced by its exact VOCABULARY spelling.
- medium: strong contextual inference, e.g. a nonsense word made meaningful by the surrounding sentence.
- Omit anything you would rate low. Never emit a guess.

WHAT TO REMOVE, AND WHEN NOT TO
1. filler — standalone hesitation sounds and pure placeholders: "um", "uh", "ehm", "eh", "hmm", "mm", "er". Remove discourse words only when they carry no meaning. They usually DO carry meaning, so keep them:
   - "cioè" introducing a clarification, "tipo" meaning kind/sort or introducing an example, "diciamo" meaning let's say, "praticamente" meaning practically, "come dire"...
   - connectors: "quindi", "allora", "comunque", "però", "insomma", "appunto", "so", "well", "you know", "like" when they structure the argument or keep the floor.
   - "no?" / "right?" when it is a real request for confirmation.
   When in doubt, keep the word.
2. repetition — only immediate duplicates with nothing meaningful between them: "il il", "che che", "sì sì sì sì" -> "sì sì". Keep repetition that is intentional (emphasis, reassurance, rhythm): "molto molto", "piano piano", "no no no".
3. false_start — only when the speaker begins a phrase, abandons it, and restarts the same phrase. Delete only the abandoned fragment. Never delete a fragment that contains a complete clause, a number, a name, a date, or a negation. Never delete the first half of a contrast such as "non ... ma". If either version could stand, keep both.
4. misheard — fix a token that cannot be what was said, or replace a mishearing with one exact VOCABULARY spelling.
   A VOCABULARY spelling may replace the mishearing even when the original looks like ordinary words. Use that spelling character for character. Do not add a hyphen, a symbol, or an internal capital that the vocabulary spelling does not have.
   When VOCABULARY lists it: "senza biologiche" becomes "Scienze Biologiche". "J-Garileo", "G-Galileo", "Gagli Leo", "Galilei", and "Galileo" become "JGalileo" when the speaker is naming that product.
   If the intended spelling is not in VOCABULARY, fix only a token that fails on its own: it is not a word ("fotofettario", "nanesi"), or the words around it make it nonsense ("qualità IVA"). A possible phrase stays. "precessione" stays "precessione". "giornata di testa" stays "giornata di testa".
   MEETING CONTEXT is not a second transcript. A term in the summary is not a spelling unless VOCABULARY lists it. Never invent a name, product, place, number, date, or amount. Never fix by sound alone.
5. punctuation — capitalization and sentence boundaries only. Do not re-punctuate lists, quotations, or numbers.

NEVER
- change tense, person, number, modality, or polarity. Do not drop or add "non", "mai", "niente", "nessuno", "senza", "not", "never", except when an exact VOCABULARY spelling replaces the misheard span that contains one of those words.
- remove hedges or opinion markers ("forse", "magari", "secondo me", "penso che", "credo", "mi sembra", "maybe", "I think").
- change numbers, dates, times, amounts, units, names, or technical terms.
- correct grammar, agreement, dialect, or colloquial syntax. Spoken Italian/English is not written prose.
- replace colloquial wording with formal wording, translate, or anglicize.
- delete a whole utterance or an entire sentence.
- use words that are not in the transcript, except the corrected form of a misheard word.

Before returning, re-read every edit and ask: "Could the speaker have meant the original?" If yes, drop the edit. A missed filler is a cosmetic loss; a wrong edit is a factual error in a record someone will rely on. An empty edit list is a valid answer."""

EDIT_FORMAT = {
    "type": "json_schema",
    "name": "edits",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["edits"],
        "properties": {"edits": {"type": "array", "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["op", "find", "replace", "type", "confidence", "evidence", "reason"],
            "properties": {
                "op": {"type": "string", "enum": ["delete", "replace"]},
                "find": {"type": "string"},
                "replace": {"type": "string"},
                "type": {"type": "string", "enum": ["filler", "repetition", "false_start", "misheard", "punctuation"]},
                "confidence": {"type": "string", "enum": ["high", "medium"]},
                "evidence": {"type": "string"},
                "reason": {"type": "string"},
            },
        }}},
    },
}


def split_speaker_blocks(text: str) -> list[tuple[str | None, str]]:
    """Separate speaker header lines from the words cleanup is allowed to edit."""
    lines = text.splitlines()
    if not lines or not _SPEAKER_HEADER.match(lines[0]):
        return [(None, text)]
    blocks: list[tuple[str | None, str]] = []
    header: str | None = None
    body: list[str] = []

    def flush():
        if header is not None:
            blocks.append((header, "\n".join(body)))

    for line in lines:
        if _SPEAKER_HEADER.match(line):
            flush()
            header = line
            body = []
        elif line == "":
            continue
        else:
            body.append(line)
    flush()
    return blocks


def join_speaker_blocks(blocks: list[tuple[str | None, str]]) -> str:
    if len(blocks) == 1 and blocks[0][0] is None:
        return blocks[0][1]
    return "\n\n".join(f"{header}\n{body}" for header, body in blocks) + "\n"


def windows(text: str) -> list[str]:
    words = text.split(" ")
    return [" ".join(words[i:i + WINDOW_WORDS]) for i in range(0, len(words), WINDOW_WORDS)]


def _words(text: str) -> list[str]:
    return [word.casefold() for word in _WORD.findall(text)]


def _is_subsequence(needles: list[str], haystack: list[str]) -> bool:
    index = 0
    for word in haystack:
        if index < len(needles) and word == needles[index]:
            index += 1
    return index == len(needles)


def _new_word_count(replace: str, find: str) -> int:
    available = _words(find)
    introduced = 0
    for word in _words(replace):
        if word in available:
            available.remove(word)
        else:
            introduced += 1
    return introduced


def _contiguous(needle: list[str], haystack: list[str]) -> bool:
    size = len(needle)
    if size == 0 or size > len(haystack):
        return False
    return any(haystack[index:index + size] == needle for index in range(len(haystack) - size + 1))


def _without_one(words: list[str], phrase: list[str]) -> list[str]:
    size = len(phrase)
    for index in range(len(words) - size + 1):
        if words[index:index + size] == phrase:
            return words[:index] + words[index + size:]
    return words


def _close_word(left: str, right: str) -> bool:
    if len(left) < 4 or len(right) < 4:
        return False
    if left in right or right in left:
        return True
    return left[:4] == right[:4] or left[-4:] == right[-4:]


def _is_vocabulary_fix(find: str, replace: str, vocabulary: list[dict]) -> bool:
    """A misheard span may become a known spelling. The surrounding words stay."""
    find_words = _words(find)
    replace_words = _words(replace)
    if not find_words or len(find_words) > 8:
        return False
    folded = find.casefold()
    for entry in vocabulary or []:
        term_words = _words(entry.get("term") or "")
        if not term_words or not _contiguous(term_words, replace_words):
            continue
        if _contiguous(term_words, find_words):
            continue
        aliases = [entry.get("term") or "", *(entry.get("heard_as") or [])]
        known = any(alias.casefold() in folded for alias in aliases if alias)
        similar = any(_close_word(word, term) for word in find_words for term in term_words)
        if not known and not similar:
            continue
        if _is_subsequence(_without_one(replace_words, term_words), find_words):
            return True
    return False


def _reject_reason(edit: dict, vocabulary: Optional[list[dict]] = None) -> Optional[str]:
    find = edit.get("find") or ""
    replace = "" if edit.get("op") == "delete" else (edit.get("replace") or "")
    if not find or replace == find:
        return "empty"
    kind = edit.get("type")
    if kind == "misheard":
        if _is_vocabulary_fix(find, replace, vocabulary or []):
            return None
        if _new_word_count(replace, find) > MISHEARD_NEW_WORD_LIMIT:
            return "too many new words"
        find_words = set(_words(find))
        replace_words = set(_words(replace))
        if find_words & _PROTECTED - replace_words:
            return "dropped function word"
        return None
    if kind not in ("filler", "repetition", "false_start", "punctuation"):
        return "unknown type"
    if not _is_subsequence(_words(replace), _words(find)):
        return "not a subsequence"
    return None


def apply_edits(text: str, edits: list[dict], vocabulary: Optional[list[dict]] = None) -> tuple[str, list[dict], list[dict]]:
    """Apply exact, unique, non-overlapping edits. Returns text, applied, skipped."""
    spans = []
    for edit in edits:
        if edit.get("op") == "delete":
            edit = {**edit, "replace": ""}
        reason = _reject_reason(edit, vocabulary)
        if reason:
            spans.append({"bad": reason, **edit})
            continue
        find = edit["find"]
        start = text.find(find)
        if start < 0:
            spans.append({"bad": "not found", **edit})
            continue
        if text.find(find, start + 1) >= 0:
            spans.append({"bad": "ambiguous", **edit})
            continue
        spans.append({"start": start, "end": start + len(find), **edit})

    good = sorted((span for span in spans if "start" in span), key=lambda span: span["start"])
    kept = []
    last_end = -1
    for span in good:
        if span["start"] < last_end:
            spans.append({"bad": "overlap", **{key: span[key] for key in ("op", "find", "replace", "type", "confidence", "evidence", "reason")}})
            continue
        kept.append(span)
        last_end = span["end"]

    pieces = []
    position = 0
    for span in kept:
        pieces.append(text[position:span["start"]])
        pieces.append(span["replace"])
        position = span["end"]
    pieces.append(text[position:])
    applied = [{key: span[key] for key in ("op", "find", "replace", "type", "confidence", "evidence", "reason")} for span in kept]
    skipped = [span for span in spans if "bad" in span]
    return "".join(pieces), applied, skipped


def _request_edits(client: OpenAI, window: str, context: str, language: Optional[str], vocabulary: str) -> list[dict]:
    user = (
        f"LANGUAGE: {language or 'auto'}\n\n"
        "VOCABULARY (exact spellings you may use for a mishearing; do not insert a term the speaker was not reaching for):\n"
        f"{vocabulary or '(none)'}\n\n"
        "MEETING CONTEXT (for spelling only; never copy facts from it into the transcript):\n"
        f"{context or '(none)'}\n\n"
        f"TRANSCRIPT WINDOW:\n{window}"
    )
    response = create_response(
        client,
        "transcript_cleanup",
        input=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
        defaults=CLEANUP_DEFAULT_CONFIG,
        text_format=EDIT_FORMAT,
    )
    message = next((item for item in response.output if item.type == "message"), None)
    if not message or not message.content:
        raise ValueError("cleanup response had no message")
    payload = json.loads(message.content[0].text)
    edits = payload.get("edits", [])
    if not isinstance(edits, list):
        raise ValueError("cleanup response edits were not a list")
    return edits


def _edits_or_none(client: OpenAI, window: str, context: str, language: Optional[str], vocabulary: str) -> list[dict]:
    try:
        return _request_edits(client, window, context, language, vocabulary)
    except Exception as error:
        print(f"   ⚠️ Transcript cleanup skipped one section ({error})")
        return []


def clean_transcript(
    transcript: str,
    language: Optional[str],
    context: str,
    client: OpenAI,
    vocabulary: Optional[list[dict]] = None,
) -> str:
    """Return the transcript with safe edits applied. Unusable output stays raw."""
    if not transcript.strip():
        return transcript
    blocks = split_speaker_blocks(transcript)
    if blocks[0][0] is None:
        return _clean_body(transcript, language, context, client, vocabulary)
    cleaned = [
        (header, _clean_body(body, language, context, client, vocabulary) if body.strip() else body)
        for header, body in blocks
    ]
    return join_speaker_blocks(cleaned)


def _clean_body(
    transcript: str,
    language: Optional[str],
    context: str,
    client: OpenAI,
    vocabulary: Optional[list[dict]] = None,
) -> str:
    if not transcript.strip():
        return transcript
    entries = vocabulary or []
    vocabulary_text = format_for_cleanup(entries) if entries else "(none)"
    meeting_context = (context or "")[:CONTEXT_CHARS]
    parts = windows(transcript)
    workers = min(MAX_WINDOW_WORKERS, len(parts))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        edit_lists = list(executor.map(
            lambda part: _edits_or_none(client, part, meeting_context, language, vocabulary_text),
            parts,
        ))
    cleaned_parts = []
    applied_count = 0
    for part, edits in zip(parts, edit_lists):
        cleaned, applied, _skipped = apply_edits(part, edits, entries)
        cleaned_parts.append(cleaned)
        applied_count += len(applied)
    cleaned = " ".join(cleaned_parts)
    if not cleaned.strip():
        print("   ⚠️ Transcript cleanup produced empty text; keeping the raw transcript")
        return transcript
    if applied_count:
        print(f"   ✨ Transcript cleanup applied {applied_count} edits")
    return cleaned
