"""Inbound connectors: turn a webhook from another system into a call to analyze.

A connector verifies the request and returns an InboundCall with either a transcript or a
recording URL. Add one by registering a class under kind "connector"; it is served at
POST /v1/connectors/<name>/webhook.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Mapping

from .registry import register


class Unauthorized(Exception):
    pass


@dataclass(frozen=True)
class InboundCall:
    external_id: str
    transcript: str | None = None
    recording_url: str | None = None


@register("connector", "generic")
class GenericWebhook:
    """JSON body {"call_id": "...", "transcript": "..."} or {"call_id": "...", "recording_url": "https://..."},
    signed with header X-Callsight-Signature: sha256=<hex HMAC-SHA256 of the raw body with the shared secret>."""

    name = "generic"

    def __init__(self, *, secret: str, **_: object) -> None:
        self.secret = secret.encode()

    def parse(self, body: bytes, headers: Mapping[str, str]) -> InboundCall:
        expected = "sha256=" + hmac.new(self.secret, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, headers.get("x-callsight-signature", "")):
            raise Unauthorized("bad or missing X-Callsight-Signature")
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise ValueError("body must be JSON") from exc
        if not isinstance(data, dict):
            raise ValueError("body must be a JSON object")
        transcript, url = data.get("transcript"), data.get("recording_url")
        if (transcript is None) == (url is None):
            raise ValueError("send exactly one of transcript or recording_url")
        if not all(isinstance(v, str) for v in (transcript or "", url or "", data.get("call_id", ""))):
            raise ValueError("call_id, transcript and recording_url must be strings")
        return InboundCall(str(data.get("call_id") or "webhook"), transcript, url)
