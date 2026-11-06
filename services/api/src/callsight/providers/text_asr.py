"""Parses plain-text transcripts of the form `[mm:ss] Speaker: text`.

This is the first "ASR" provider. Audio transcription providers (e.g. a local
Whisper model) will implement the same interface later.
"""

from __future__ import annotations

import re
from dataclasses import replace

from ..models import Segment, Transcript
from ..registry import register

# [mm:ss] or [h:mm:ss], then "Speaker: text"
_LINE = re.compile(r"^\[(?:(\d+):)?(\d{1,2}):(\d{2})\]\s*([^:\[\]]{1,40}):\s*(.+)$")


@register("asr", "text")
class TextTranscriptParser:
    name = "text"

    def __init__(self, **_: object) -> None:
        pass

    def transcribe(self, source: bytes | str, call_id: str) -> Transcript:
        text = source.decode("utf-8", errors="replace") if isinstance(source, bytes) else source
        segments: list[Segment] = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            match = _LINE.match(line)
            if match:
                hours, minutes, seconds, speaker, body = match.groups()
                start = int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)
                segments.append(Segment(len(segments), float(start), speaker.strip(), body.strip()))
            elif segments:
                # Continuation of the previous speaker's turn.
                last = segments[-1]
                segments[-1] = replace(last, text=f"{last.text} {line}")
        if not segments:
            raise ValueError("no transcript lines found; expected lines like '[00:12] Speaker: text'")
        return Transcript(call_id=call_id, segments=segments)
