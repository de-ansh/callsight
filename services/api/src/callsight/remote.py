"""Fetch a recording from a URL supplied by a webhook, without letting the URL reach internal hosts."""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from urllib.parse import urlsplit

MAX_BYTES = 500 * 1024 * 1024
TIMEOUT_S = 60


class UnsafeURL(ValueError):
    pass


def check_url(url: str) -> str:
    """Allow only https URLs whose host resolves exclusively to public addresses (SSRF guard).
    Returns one of the checked addresses, so the caller can connect to exactly that one."""
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
    return infos[0][4][0]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to an already-checked IP, while TLS still verifies the certificate for the hostname.
    Resolving the name again at connect time would let DNS rebinding swap in an internal address."""

    def __init__(self, host: str, ip: str, port: int, timeout: float):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._ip = ip

    def connect(self) -> None:
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def fetch(url: str, path: str, max_bytes: int = MAX_BYTES) -> None:
    ip = check_url(url)
    parts = urlsplit(url)
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    conn = _PinnedHTTPSConnection(parts.hostname, ip, parts.port or 443, TIMEOUT_S)
    try:
        conn.request("GET", target)
        response = conn.getresponse()
        if response.status != 200:  # redirects are not followed: they could point at an internal host
            raise ValueError(f"recording_url returned HTTP {response.status}")
        with open(path, "wb") as out:
            size = 0
            while chunk := response.read(1 << 20):
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError(f"recording larger than {max_bytes // 2**20} MB")
                out.write(chunk)
    finally:
        conn.close()
