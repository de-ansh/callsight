"""Fetch a recording from a URL supplied by a webhook, without letting the URL reach internal hosts."""

from __future__ import annotations

import ipaddress
import socket
import urllib.request
from urllib.parse import urlsplit

MAX_BYTES = 500 * 1024 * 1024
TIMEOUT_S = 60


class UnsafeURL(ValueError):
    pass


def check_url(url: str) -> None:
    """Allow only https URLs whose host resolves exclusively to public addresses (SSRF guard)."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise UnsafeURL("recording_url must be an https URL")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeURL(f"cannot resolve {parts.hostname}") from exc
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise UnsafeURL(f"{parts.hostname} resolves to a non-public address")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # a redirect could point at an internal host
        raise UnsafeURL("redirects are not followed")


def fetch(url: str, path: str, max_bytes: int = MAX_BYTES) -> None:
    # shortcut: the host is resolved twice (check, then connect), so DNS rebinding could slip past;
    # pin the checked IP in the connection, or fetch through an egress proxy, before untrusted senders.
    check_url(url)
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(url, timeout=TIMEOUT_S) as response, open(path, "wb") as out:
        size = 0
        while chunk := response.read(1 << 20):
            size += len(chunk)
            if size > max_bytes:
                raise ValueError(f"recording larger than {max_bytes // 2**20} MB")
            out.write(chunk)
