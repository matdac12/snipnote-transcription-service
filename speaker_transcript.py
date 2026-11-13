"""Turn xAI word timings into a speaker transcript string."""


def turns_from_words(words: list[dict]) -> list[dict]:
    turns: list[dict] = []
    for word in words:
        speaker = word["speaker"]
        if turns and turns[-1]["speaker"] == speaker:
            turns[-1]["words"].append(word["text"])
            turns[-1]["end"] = word["end"]
            continue
        turns.append({
            "speaker": speaker,
            "start": word["start"],
            "end": word["end"],
            "words": [word["text"]],
        })
    return turns


def stitch_short_intrusions(turns: list[dict], max_words: int = 2) -> list[dict]:
    items = [
        {
            "speaker": turn["speaker"],
            "start": turn["start"],
            "end": turn["end"],
            "words": list(turn["words"]),
        }
        for turn in turns
    ]
    index = 1
    while index < len(items) - 1:
        previous = items[index - 1]
        current = items[index]
        following = items[index + 1]
        if len(current["words"]) <= max_words and previous["speaker"] == following["speaker"]:
            previous["words"].extend(current["words"])
            previous["words"].extend(following["words"])
            previous["end"] = following["end"]
            del items[index:index + 2]
            continue
        index += 1
    return items


def _clock(seconds: float) -> str:
    whole = int(seconds)
    hours, remainder = divmod(whole, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def render_speaker_transcript(turns: list[dict]) -> str:
    if not turns:
        return ""
    blocks = []
    for turn in turns:
        header = f"[{_clock(turn['start'])}\u2013{_clock(turn['end'])}] Speaker {turn['speaker'] + 1}"
        body = " ".join(turn["words"])
        blocks.append(f"{header}\n{body}")
    return "\n\n".join(blocks) + "\n"
