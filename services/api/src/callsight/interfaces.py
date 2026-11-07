"""Plugin interfaces. Anything implementing these can be registered and swapped in."""

from __future__ import annotations

from typing import Protocol

from .models import Insight, Transcript


class ASR(Protocol):
    """Turns a recording or text into a Transcript."""

    name: str

    def transcribe(self, source: bytes | str, call_id: str) -> Transcript: ...


class LLM(Protocol):
    """Minimal text-completion interface so any local or hosted model can be plugged in."""

    name: str

    def complete(self, prompt: str, *, json_mode: bool = False) -> str:
        """json_mode asks the model for a single JSON object (callers still validate it)."""
        ...


class Analyzer(Protocol):
    """Reads a Transcript and returns insights, each backed by transcript evidence."""

    name: str

    def analyze(self, transcript: Transcript) -> list[Insight]: ...
