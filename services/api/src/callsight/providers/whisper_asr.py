"""Local speech-to-text with faster-whisper (https://github.com/SYSTRAN/faster-whisper, MIT).

Speakers are separated by channel: on a stereo recording (the usual telephony layout, one
party per channel) each channel is transcribed on its own and the turns are merged by time.
A mono recording (a Zoom/Meet export) is one speaker, unless speaker diarization is enabled
(CALLSIGHT_DIARIZATION=pyannote): pyannote then finds who spoke when, and each Whisper word is
given to the speaker talking at that moment.
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


Word = tuple[float, float, str]  # start_s, end_s, text (Whisper words carry a leading space)


def assign_speakers(words: Iterable[Word], turns: list[tuple[float, float, str]], gap_s: float = TURN_GAP_S
                    ) -> list[tuple[float, str, str]]:
    """Give each word to the diarized speaker talking at its midpoint (or the nearest turn), name
    speakers "Speaker 1", "Speaker 2", ... in order of first appearance, and start a new turn on a
    change of speaker or a pause of at least gap_s. Returns (start_s, speaker, text) turns."""
    if not turns:
        return [(s, SPEAKERS[0], t) for s, t in split_on_pauses(words, gap_s)]
    names: dict[str, str] = {}
    out: list[tuple[float, str, list[str]]] = []
    last_end, last_speaker = None, None
    for start, end, text in words:
        mid = (start + end) / 2
        label = min(turns, key=lambda t: 0 if t[0] <= mid <= t[1] else min(abs(mid - t[0]), abs(mid - t[1])))[2]
        speaker = names.setdefault(label, f"Speaker {len(names) + 1}")
        if speaker != last_speaker or last_end is None or start - last_end >= gap_s:
            out.append((start, speaker, []))
        out[-1][2].append(text)
        last_end, last_speaker = end, speaker
    return [(start, speaker, "".join(parts).strip()) for start, speaker, parts in out]


@register("asr", "whisper")
class WhisperASR:
    name = "whisper"

    def __init__(
        self, *, model: str = "small", device: str = "auto", compute_type: str = "default",
        diarization: str = "", diarization_model: str = "pyannote/speaker-diarization-community-1",
        hf_token: str = "", **_: object,
    ):
        from faster_whisper import WhisperModel  # heavy import; only workers need it

        self._model = WhisperModel(model, device=device, compute_type=compute_type)
        self._diarizer = None
        if diarization == "pyannote":
            self._diarizer = self._load_pyannote(diarization_model, hf_token)
        elif diarization:
            raise ValueError(f"unknown CALLSIGHT_DIARIZATION {diarization!r}; expected 'pyannote' or empty")

    @staticmethod
    def _load_pyannote(model: str, token: str):
        try:
            from pyannote.audio import Pipeline
        except ImportError as exc:
            raise RuntimeError("CALLSIGHT_DIARIZATION=pyannote needs the 'diarize' extra (pip install '.[diarize]')") from exc
        if not token:
            from huggingface_hub import get_token

            token = get_token()  # a token saved by `hf auth login` works too
        if not token:
            raise RuntimeError(f"CALLSIGHT_DIARIZATION=pyannote needs HF_TOKEN (or `hf auth login`), "
                               f"after accepting the terms of {model} on Hugging Face")
        pipeline = Pipeline.from_pretrained(model, token=token)
        if pipeline is None:  # pyannote returns None when the model is gated and terms weren't accepted
            raise RuntimeError(f"could not load {model}: accept its terms on huggingface.co with the HF_TOKEN account")
        return pipeline

    def _words(self, audio) -> list[Word]:
        # vad_filter skips silence, which otherwise makes Whisper invent text on the quiet channel.
        segments, _info = self._model.transcribe(audio, vad_filter=True, word_timestamps=True)
        return [(w.start, w.end, w.word) for s in segments for w in s.words or ()]

    def _transcribe(self, audio) -> list[tuple[float, str]]:
        # Whisper words carry their own leading space, so joining with "" keeps the spacing.
        return split_on_pauses(self._words(audio))

    def _diarize(self, audio) -> list[tuple[float, float, str]]:
        """(start_s, end_s, label) speaker turns for 16 kHz mono float audio."""
        import torch

        output = self._diarizer({"waveform": torch.from_numpy(audio).unsqueeze(0), "sample_rate": 16000})
        annotation = getattr(output, "speaker_diarization", output)  # pyannote 4 wraps it; 3.x returns it
        return [(seg.start, seg.end, label) for seg, _track, label in annotation.itertracks(yield_label=True)]

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
        elif self._diarizer is not None:
            audio = decode_audio(source)
            turns = assign_speakers(self._words(audio), self._diarize(audio))
            segments = [Segment(i, round(s, 2), spk, text) for i, (s, spk, text) in enumerate(turns) if text]
            if not segments:
                raise ValueError("no speech found in the recording")
            return Transcript(call_id=call_id, segments=segments)
        else:
            channels = [(SPEAKERS[0], self._transcribe(decode_audio(source)))]
        segments = merge_channels(channels)
        if not segments:
            raise ValueError("no speech found in the recording")
        return Transcript(call_id=call_id, segments=segments, stereo=stereo)
