"""Local speech-to-text with faster-whisper (https://github.com/SYSTRAN/faster-whisper, MIT).

Speakers are separated by channel: on a stereo recording (the usual telephony layout, one
party per channel) each channel is transcribed on its own and the turns are merged by time.
shortcut: a mono recording comes back as a single speaker; add pyannote diarization when
mono calls turn out to matter (it needs torch and a gated Hugging Face model).
"""

from __future__ import annotations

from typing import Iterable

from ..models import Segment, Transcript
from ..registry import register

SPEAKERS = ("Speaker 1", "Speaker 2")  # left, right channel
# Whisper's own segments run across pauses (and so across the other party's turns), which
# puts evidence timestamps tens of seconds off. A new turn starts at any silence this long.
TURN_GAP_S = 1.0


def split_on_pauses(words: Iterable[tuple[float, float, str]], gap_s: float = TURN_GAP_S) -> list[tuple[float, str]]:
    """Group (start_s, end_s, word) into (start_s, text) turns, breaking at silences >= gap_s."""
    turns: list[tuple[float, list[str]]] = []
    last_end = None
    for start, end, word in words:
        if last_end is None or start - last_end >= gap_s:
            turns.append((start, []))
        turns[-1][1].append(word)
        last_end = end
    return [(start, "".join(parts).strip()) for start, parts in turns]


def merge_channels(channels: Iterable[tuple[str, Iterable[tuple[float, str]]]]) -> list[Segment]:
    """Interleave (speaker, [(start_s, text), ...]) streams into one time-ordered list of turns."""
    turns = sorted(
        ((start, speaker, text.strip()) for speaker, parts in channels for start, text in parts if text.strip()),
        key=lambda t: t[0],
    )
    return [Segment(i, round(start, 2), speaker, text) for i, (start, speaker, text) in enumerate(turns)]


@register("asr", "whisper")
class WhisperASR:
    name = "whisper"

    def __init__(self, *, model: str = "small", device: str = "auto", compute_type: str = "default", **_: object):
        from faster_whisper import WhisperModel  # heavy import; only workers need it

        self._model = WhisperModel(model, device=device, compute_type=compute_type)

    def _transcribe(self, audio) -> list[tuple[float, str]]:
        # vad_filter skips silence, which otherwise makes Whisper invent text on the quiet channel.
        segments, _info = self._model.transcribe(audio, vad_filter=True, word_timestamps=True)
        # Whisper words carry their own leading space, so joining with "" keeps the spacing.
        return split_on_pauses((w.start, w.end, w.word) for s in segments for w in s.words or ())

    def transcribe(self, source: bytes | str, call_id: str) -> Transcript:
        """source: path to an audio file (bytes are not supported: recordings can be large)."""
        if not isinstance(source, str):
            raise TypeError("WhisperASR.transcribe expects a file path")
        import av
        from faster_whisper import decode_audio

        with av.open(source) as container:
            stereo = container.streams.audio[0].channels >= 2
        if stereo:
            left, right = decode_audio(source, split_stereo=True)
            channels = [(SPEAKERS[0], self._transcribe(left)), (SPEAKERS[1], self._transcribe(right))]
        else:
            channels = [(SPEAKERS[0], self._transcribe(decode_audio(source)))]
        segments = merge_channels(channels)
        if not segments:
            raise ValueError("no speech found in the recording")
        return Transcript(call_id=call_id, segments=segments, stereo=stereo)
