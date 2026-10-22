"""Fyers HTTP over IPv4 only (07 §9, HAT-BRK-093).

Fyers accepts orders only from the account's whitelisted static IP, which is an IPv4 address. A
machine with IPv6 reaches `api-t1.fyers.in` over IPv6 by default, and Fyers then refuses every
write with code -50 ("only whitelisted IP"), as the live write test of 2026-10-22 found.

This module is the one place that sets it: it is the only importer of the SDK's HTTP client
(`fyers_apiv3.fyersModel`, whose `FyersModel` sends orders, GTTs, history and quotes and whose
`SessionModel` exchanges the login code), and it installs the hook below before it hands the
SDK out. The hook wraps urllib3's `create_connection`, which every `requests` call uses: a
connection to a Fyers host (`fyers.in` or a subdomain) is opened only to the host's IPv4
addresses, and with none it fails rather than fall back to IPv6. Any other host is untouched.
TLS still checks the Fyers host name: only the TCP connect gets the address literal.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Callable
from types import ModuleType

import urllib3.util.connection as urllib3_connection

__all__ = ["FYERS_DOMAIN", "connect_ipv4", "install", "is_fyers_host", "sdk"]

FYERS_DOMAIN = "fyers.in"

_lock = threading.Lock()
_original: Callable[..., socket.socket] | None = None


class FyersIPv4Error(OSError):
    """A Fyers host has no IPv4 address: the connection is refused, never made over IPv6."""


def is_fyers_host(host: object) -> bool:
    """Whether `host` is Fyers' domain or one of its subdomains (case and a trailing dot
    ignored); a look-alike such as `notfyers.in` or `fyers.in.example` is not."""
    if not isinstance(host, str):
        return False
    name = host.strip("[]").rstrip(".").lower()
    return name == FYERS_DOMAIN or name.endswith("." + FYERS_DOMAIN)


def connect_ipv4(
    connect: Callable[..., socket.socket], address: tuple[str, int], *args: object, **kwargs: object
) -> socket.socket:
    """Open `address` with `connect` (urllib3's `create_connection`), a Fyers host through its
    IPv4 addresses only, in the resolver's order; the last failure is raised."""
    host, port = address
    if not is_fyers_host(host):
        return connect(address, *args, **kwargs)
    found = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    addresses = list(dict.fromkeys(str(sockaddr[0]) for *_, sockaddr in found))
    if not addresses:
        raise FyersIPv4Error("no IPv4 address for the Fyers host")
    error: OSError | None = None
    for ip in addresses:
        try:
            return connect((ip, port), *args, **kwargs)
        except OSError as exc:
            error = exc
    assert error is not None
    raise error


def install() -> None:
    """Route urllib3's connections through `connect_ipv4` (once per process; idempotent)."""
    global _original
    with _lock:
        if _original is not None:
            return
        original = urllib3_connection.create_connection
        _original = original

        def create_connection(
            address: tuple[str, int], *args: object, **kwargs: object
        ) -> socket.socket:
            return connect_ipv4(original, address, *args, **kwargs)

        urllib3_connection.create_connection = create_connection  # type: ignore[assignment]


def sdk() -> ModuleType:
    """The SDK's `fyersModel` module, with the IPv4 hook installed first."""
    install()
    from fyers_apiv3 import fyersModel

    return fyersModel
