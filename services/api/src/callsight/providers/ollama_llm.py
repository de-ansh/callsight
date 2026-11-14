"""LLM provider for a local Ollama server (https://ollama.com). Uses only the stdlib."""

from __future__ import annotations

import json
import urllib.request

from ..registry import register


@register("llm", "ollama")
class OllamaLLM:
    name = "ollama"

    def __init__(
        self,
        *,
        base_url: str = "http://localhost:11434",
        model: str = "llama3.1",
        timeout: float = 120.0,
        **_: object,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def complete(self, prompt: str, *, json_mode: bool = False) -> str:
        payload = {"model": self.model, "prompt": prompt, "stream": False, "options": {"temperature": 0}}
        if json_mode:
            payload["format"] = "json"  # Ollama constrains decoding to valid JSON
        body = json.dumps(payload).encode()
        request = urllib.request.Request(
            f"{self.base_url}/api/generate",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode())["response"]


def ollama_embed(base_url: str, model: str, texts: list[str], timeout: float = 120.0) -> list[list[float]]:
    """One vector per text, from Ollama's /api/embed (e.g. model "mxbai-embed-large")."""
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/embed",
        data=json.dumps({"model": model, "input": texts}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        vectors = json.loads(response.read().decode())["embeddings"]
    if len(vectors) != len(texts):
        raise ValueError(f"Ollama returned {len(vectors)} embeddings for {len(texts)} texts")
    return vectors
